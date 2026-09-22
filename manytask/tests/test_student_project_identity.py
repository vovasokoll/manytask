"""Offline regression checks for case-only names and safe repository reuse."""

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask, session
from gitlab.exceptions import GitlabCreateError, GitlabGetError

from manytask.abstract import RmsApiException
from manytask.glab import GitLabApi, GitLabConfig
from manytask.web import create_project


class StudentProjectIdentityTests(unittest.TestCase):
    def setUp(self):
        with patch("manytask.glab.gitlab.Gitlab"):
            self.api = GitLabApi(GitLabConfig(base_url="https://gitlab.invalid", admin_token="test-only"))
        self.group = "course/students"
        self.user = SimpleNamespace(id="123", username="TestStudent")
        self.project = MagicMock()
        self.project.id = 42
        self.project.namespace = {"id": 7}
        self.project.path_with_namespace = self.group + "/teststudent"
        self.project.members_all.get.return_value = SimpleNamespace(id=123, access_level=30)
        self.public = MagicMock()
        self.public.members_all.get.return_value = SimpleNamespace(id=123, access_level=20)
        self.api._gitlab.projects.get.return_value = self.project
        self.api._get_group_by_name = MagicMock(return_value=SimpleNamespace(id=7))
        self.api._get_project_by_name = MagicMock(return_value=self.public)
        self.api._configure_student_project = MagicMock()

    def create(self):
        self.api.create_project(self.user, self.group, "course/public")

    def test_case_only_project_is_recognized(self):
        self.assertTrue(self.api.check_project_exists(self.user.username, self.group))

    def test_different_namespaces_and_logins_are_not_recognized(self):
        for path in ("other/students/teststudent", "Course/students/teststudent", "course/students/other"):
            self.project.path_with_namespace = path
            self.assertFalse(self.api.check_project_exists(self.user.username, self.group))

    def test_existing_case_variant_reuses_project_without_fork(self):
        self.create()
        self.public.forks.create.assert_not_called()
        self.api._gitlab.projects.list.assert_not_called()
        self.api._configure_student_project.assert_called_once_with(self.project, self.public, self.group)
        self.project.members.create.assert_called_once_with({"user_id": 123, "access_level": 30})

    def test_namespace_id_must_match_even_if_path_matches(self):
        self.project.namespace = {"id": 99}
        with self.assertRaises(RmsApiException):
            self.create()
        self.project.members.create.assert_not_called()

    def test_orphaned_repository_is_not_claimed_by_matching_username(self):
        self.project.members_all.get.side_effect = GitlabGetError("missing", 404)
        with self.assertRaises(RmsApiException):
            self.create()
        self.project.members.create.assert_not_called()
        self.public.forks.create.assert_not_called()
        self.api._configure_student_project.assert_not_called()

    def test_wrong_member_id_or_insufficient_role_is_rejected(self):
        for member in (SimpleNamespace(id=999, access_level=30), SimpleNamespace(id=123, access_level=20)):
            self.project.members_all.get.return_value = member
            with self.assertRaises(RmsApiException):
                self.create()
        self.project.members.create.assert_not_called()

    def test_permission_error_is_not_treated_as_missing_project(self):
        self.api._gitlab.projects.get.side_effect = GitlabGetError("forbidden", 403)
        with self.assertRaises(GitlabGetError):
            self.create()
        self.public.forks.create.assert_not_called()

    def test_new_project_uses_normalized_slug_but_original_display_name(self):
        self.api._gitlab.projects.get.side_effect = [GitlabGetError("missing", 404), self.project]
        self.public.forks.create.return_value = SimpleNamespace(id=42)
        self.create()
        payload = self.public.forks.create.call_args.args[0]
        self.assertEqual(payload["path"], "teststudent")
        self.assertEqual(payload["name"], "TestStudent")

    def test_membership_conflict_requires_positive_access_readback(self):
        self.project.members.create.side_effect = GitlabCreateError("exists", 409)
        self.create()
        self.project.members_all.get.assert_called_with(123)

    def test_non_conflict_membership_failure_is_not_swallowed(self):
        self.project.members.create.side_effect = GitlabCreateError("forbidden", 403)
        with self.assertRaises(GitlabCreateError):
            self.create()
        self.public.members.create.assert_not_called()

    def test_missing_access_after_successful_grant_is_rejected(self):
        self.public.members_all.get.side_effect = GitlabGetError("missing", 404)
        with self.assertRaises(RmsApiException):
            self.create()


class ProjectFailurePageTests(unittest.TestCase):
    def test_project_errors_stay_on_join_page_and_do_not_enroll(self):
        app = Flask(__name__)
        app.secret_key = "test-only"
        app.favicon = "favicon.ico"
        app.rms_api = MagicMock()
        app.storage_api = MagicMock()
        app.storage_api.get_course.return_value = SimpleNamespace(
            course_name="course",
            token="test-admin",
            registration_secret="test-join",
            gitlab_course_students_group="course/students",
            gitlab_course_public_repo="course/public",
        )
        for error in (GitlabCreateError("PRIVATE_RESPONSE_SENTINEL", 400), RmsApiException("Contact an administrator")):
            app.rms_api.create_project.side_effect = error
            with app.test_request_context(method="POST", data={"secret": "test-join"}):
                session.update(rms={"rms_id": "123"}, manytask={"username": "TestStudent"})
                with patch("manytask.web.validate_csrf"), patch("manytask.web.render_template") as render:
                    inspect.unwrap(create_project)("course")
            self.assertEqual(render.call_args.args[0], "create_project.html")
            self.assertNotIn("PRIVATE_RESPONSE_SENTINEL", render.call_args.kwargs["error_message"])
            app.storage_api.sync_user_on_course.assert_not_called()


if __name__ == "__main__":
    unittest.main()
