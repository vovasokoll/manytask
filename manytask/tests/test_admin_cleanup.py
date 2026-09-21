"""Offline cleanup regression tests: SQLite and fake GitLab, no live credentials."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from manytask.admin_cleanup import (
    CleanupError,
    GitLab,
    Target,
    apply_cleanup,
    build_plan,
    fingerprint,
    resolve_target,
)
from manytask.models import Base, Course, Grade, Task, TaskGroup, User, UserOnCourse


class FakeGitLab:
    def __init__(self):
        self.url = "https://gitlab.invalid/api/v4/"
        self.data = {
            "groups/course%2Fstudents": {"id": 10, "full_path": "course/students"},
            "users/20": {"id": 20, "username": "student", "is_admin": False},
            "projects/30": {"id": 30, "path_with_namespace": "course/students/student", "namespace": {"id": 10}},
        }
        self.lists = {}
        self.calls = []
        self.fail_delete = False
        self.keep_pending = False

    def get(self, path):
        if path == "projects/course%2Fstudents%2Fstudent":
            path = "projects/30"
        return copy.deepcopy(self.data.get(path))

    def listing(self, path, **params):
        return self.lists.get((path, params.get("scope")), [])

    def export(self, project_id, destination):
        self.calls.append(("EXPORT", project_id))
        destination.write_bytes(b"fake-export")

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if self.fail_delete:
            raise CleanupError("API deletion failed")
        if self.keep_pending and path == "projects/30":
            self.data[path]["marked_for_deletion_on"] = "2026-10-01"
        else:
            self.data.pop(path, None)
            if path == "groups/10/members/20":
                self.data.pop("groups/10/members/all/20", None)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.api = FakeGitLab()
        self.target = Target("course", "student", 1, 20, 30)
        self.temp = tempfile.TemporaryDirectory()
        self.backup = Path(self.temp.name) / "operation"
        self.session.add_all(
            [
                User(id=1, username="student", first_name="A", last_name="B", rms_id="20", auth_id=20),
                User(id=2, username="other", first_name="C", last_name="D", rms_id="21", auth_id=21),
                self.course(1, "course", "course/students"),
                self.course(2, "other-course", "other/students"),
            ]
        )
        self.session.flush()
        self.session.add_all(
            [
                UserOnCourse(id=1, user_id=1, course_id=1),
                UserOnCourse(id=2, user_id=2, course_id=1),
                TaskGroup(id=1, name="group", course_id=1),
            ]
        )
        self.session.flush()
        self.session.add(Task(id=1, name="sum", group_id=1, score=10))
        self.session.flush()
        self.session.add_all(
            [
                Grade(id=1, user_on_course_id=1, task_id=1, score=10),
                Grade(id=2, user_on_course_id=2, task_id=1, score=7),
            ]
        )
        self.session.commit()

    @staticmethod
    def course(identifier, name, group):
        return Course(
            id=identifier,
            name=name,
            registration_secret="test-only",
            token=name,
            gitlab_course_group=group.split("/")[0],
            gitlab_course_public_repo="course/public",
            gitlab_course_students_group=group,
            gitlab_default_branch="main",
            task_url_template="test",
        )

    def tearDown(self):
        self.session.close()
        self.engine.dispose()
        self.temp.cleanup()

    def plan(self):
        return build_plan(self.session, self.api, self.target)[0]

    def apply(self):
        return apply_cleanup(self.session, self.api, self.target, fingerprint(self.plan()), self.backup)

    def test_dry_run_does_not_write(self):
        plan = self.plan()
        self.assertEqual(plan["grade_count"], 1)
        self.assertEqual(self.api.calls, [])
        self.assertIsNotNone(self.session.get(User, 1))
        self.assertFalse(self.backup.exists())

    def test_discovery_needs_only_course_and_username(self):
        args = SimpleNamespace(
            course="course",
            username="student",
            apply=False,
            user_id=None,
            gitlab_user_id=None,
            project_id=None,
            permanent=False,
        )
        self.assertEqual(resolve_target(self.session, self.api, args), self.target)
        args.apply = True
        with self.assertRaises(CleanupError):
            resolve_target(self.session, self.api, args)

    def test_direct_group_membership_removed_without_parent_changes(self):
        self.api.data["groups/10/members/20"] = {"access_level": 30}
        self.api.data["groups/10/members/all/20"] = {"access_level": 30}
        self.apply()
        self.assertIsNone(self.api.get("groups/10/members/all/20"))
        self.assertIn(("DELETE", "groups/10/members/20"), self.api.calls)

    def test_deletes_target_only_and_writes_private_backup(self):
        self.assertEqual(self.apply()["project"], "absent")
        self.assertIsNone(self.session.get(User, 1))
        self.assertIsNone(self.session.get(UserOnCourse, 1))
        self.assertIsNone(self.session.get(Grade, 1))
        self.assertEqual(self.session.get(Grade, 2).score, 7)
        self.assertIsNotNone(self.session.get(User, 2))
        self.assertEqual((self.backup / "database.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.api.calls, [("EXPORT", 30), ("DELETE", "projects/30")])

    def test_other_course_and_global_gitlab_account_preserved(self):
        self.session.add(UserOnCourse(id=3, user_id=1, course_id=2))
        self.session.commit()
        self.apply()
        self.assertIsNotNone(self.session.get(User, 1))
        self.assertIsNotNone(self.session.get(UserOnCourse, 3))
        self.assertIsNotNone(self.api.get("users/20"))

    def test_missing_gitlab_user_is_supported(self):
        del self.api.data["users/20"]
        self.assertEqual(self.apply()["global_gitlab_account"], "already_absent")

    def test_missing_project_is_supported(self):
        del self.api.data["projects/30"]
        self.apply()
        self.assertEqual(self.api.calls, [])

    def test_repeat_is_idempotent(self):
        self.apply()
        self.backup = Path(self.temp.name) / "second"
        self.apply()
        self.assertEqual(len(self.session.scalars(select(User)).all()), 1)

    def test_pending_deletion_is_not_claimed_as_absent(self):
        self.api.keep_pending = True
        result = self.apply()
        self.assertEqual(result["project"], "deletion_requested_check_again")

    def test_scheduled_rename_and_permanent_removal(self):
        project = self.api.data["projects/30"]
        project["path_with_namespace"] += "-deletion_scheduled-30"
        project["marked_for_deletion_on"] = "2026-09-21"
        self.target = Target("course", "student", 1, 20, 30, permanent=True)
        self.assertTrue(self.plan()["permanently_remove_project"])
        self.assertEqual(self.apply()["project"], "absent")

    def test_permanent_requires_previous_soft_deletion(self):
        self.target = Target("course", "student", 1, 20, 30, permanent=True)
        with self.assertRaises(CleanupError):
            self.plan()

    def test_arbitrary_renamed_project_is_not_accepted(self):
        self.api.data["projects/30"]["path_with_namespace"] += "-someone-else"
        self.api.data["projects/30"]["marked_for_deletion_on"] = "2026-09-21"
        with self.assertRaises(CleanupError):
            self.plan()

    def test_safe_numeric_project_redirect(self):
        api = GitLab("https://gitlab.invalid", "test-only")
        moved = requests.Response()
        moved.status_code = 301
        moved.headers["Location"] = "https://gitlab.invalid/api/v4/projects/30"
        found = requests.Response()
        found.status_code = 200
        found._content = json.dumps({"id": 30}).encode()
        with patch.object(api.http, "request", side_effect=[moved, found]) as request:
            self.assertEqual(api.get("projects/old-path"), {"id": 30})
            self.assertEqual(request.call_count, 2)
        moved.headers["Location"] = "https://attacker.invalid/api/v4/projects/30"
        with patch.object(api.http, "request", return_value=moved) as request:
            with self.assertRaises(CleanupError):
                api.get("projects/old-path")
            self.assertEqual(request.call_count, 1)

    def test_wrong_confirmation_does_nothing(self):
        with self.assertRaises(CleanupError):
            apply_cleanup(self.session, self.api, self.target, "wrong", self.backup)
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.backup.exists())

    def test_changed_grade_invalidates_confirmation(self):
        confirmation = fingerprint(self.plan())
        self.session.get(Grade, 1).score = 9
        self.session.flush()
        with self.assertRaises(CleanupError):
            apply_cleanup(self.session, self.api, self.target, confirmation, self.backup)

    def test_gitlab_failure_keeps_database_rows(self):
        self.api.fail_delete = True
        with self.assertRaises(CleanupError):
            self.apply()
        self.assertIsNotNone(self.session.get(User, 1))
        self.assertEqual(self.session.get(Grade, 1).score, 10)
        self.assertTrue((self.backup / "database.json").exists())

    def test_export_failure_prevents_deletion(self):
        with patch.object(self.api, "export", side_effect=CleanupError("failed")):
            with self.assertRaises(CleanupError):
                self.apply()
        self.assertEqual(self.api.calls, [])
        self.assertIsNotNone(self.session.get(User, 1))

    def test_admin_is_protected(self):
        self.session.get(User, 1).is_instance_admin = True
        with self.assertRaises(CleanupError):
            self.plan()

    def test_course_admin_is_protected(self):
        self.session.get(UserOnCourse, 1).is_course_admin = True
        with self.assertRaises(CleanupError):
            self.plan()

    def test_renamed_or_mismatched_identity_is_protected(self):
        self.api.data["users/20"]["username"] = "renamed"
        with self.assertRaises(CleanupError):
            self.plan()

    def test_database_identity_is_pinned(self):
        self.target = Target("course", "student", 2, 20, 30)
        with self.assertRaises(CleanupError):
            self.plan()

    def test_unrelated_project_is_protected(self):
        self.api.data["projects/30"]["path_with_namespace"] = "another/students/student"
        with self.assertRaises(CleanupError):
            self.plan()

    def test_shared_project_is_protected(self):
        self.api.lists[("projects/30/members", None)] = [{"id": 99, "access_level": 30}]
        with self.assertRaises(CleanupError):
            self.plan()

    def test_running_pipeline_is_protected(self):
        self.api.lists[("projects/30/pipelines", "running")] = [{"id": 999}]
        with self.assertRaises(CleanupError):
            self.plan()

    def test_inherited_access_is_protected(self):
        self.api.data["groups/10/members/all/20"] = {"access_level": 30}
        with self.assertRaises(CleanupError):
            self.plan()

    def test_same_namespace_other_course_is_protected(self):
        self.session.get(Course, 2).gitlab_course_students_group = "course/students"
        self.session.add(UserOnCourse(id=3, user_id=1, course_id=2))
        with self.assertRaises(CleanupError):
            self.plan()


if __name__ == "__main__":
    unittest.main()
