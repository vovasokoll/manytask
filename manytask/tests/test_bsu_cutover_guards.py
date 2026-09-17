"""Offline regression checks: no real GitLab, database or credentials."""

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from gitlab.exceptions import GitlabGetError

from manytask.api import create_course_api
from manytask.config import CreateCourseRequest
from manytask.glab import GitLabApi, GitLabConfig
from manytask.local_config import LocalConfig


class CutoverGuardTests(unittest.TestCase):
    def api(self, dedicated=True):
        with patch("manytask.glab.gitlab.Gitlab"):
            return GitLabApi(
                GitLabConfig(
                    base_url="https://gitlab.invalid",
                    admin_token="test-only",
                    student_runner_id=117 if dedicated else None,
                    student_runner_namespace="bsu-cpp/students-2026-fall" if dedicated else "",
                )
            )

    def project(self):
        project = MagicMock()
        project.path_with_namespace = "bsu-cpp/students-2026-fall/student"
        project.variables.get.side_effect = GitlabGetError("not found", 404)
        project.runners.list.return_value = []
        return project

    def public(self):
        return SimpleNamespace(path_with_namespace="bsu-cpp/public-2026-fall", default_branch="main")

    def test_fork_settings_use_explicit_update_and_readback(self):
        project = self.project()
        self.api()._configure_student_project(project, self.public(), "bsu-cpp/students-2026-fall")
        self.assertEqual(project.ci_config_path, ".gitlab-ci.yml@bsu-cpp/public-2026-fall:main")
        self.assertFalse(project.shared_runners_enabled)
        self.assertFalse(project.group_runners_enabled)
        self.assertFalse(project.auto_devops_enabled)
        project.save.assert_called_once()
        project.refresh.assert_called_once()
        project.runners.create.assert_called_once_with({"runner_id": 117})
        self.assertEqual(project.variables.create.call_count, 4)
        for call in project.variables.create.call_args_list:
            self.assertEqual(call.args[0]["value"], "unavailable-in-student-jobs")

    def test_unretained_ci_settings_fail_before_runner_assignment(self):
        project = self.project()
        project.refresh.side_effect = lambda: setattr(project, "ci_config_path", "")
        with self.assertRaises(RuntimeError):
            self.api()._configure_student_project(project, self.public(), "bsu-cpp/students-2026-fall")
        project.runners.create.assert_not_called()
        project.variables.create.assert_not_called()

    def test_other_semester_is_not_modified(self):
        project = self.project()
        project.path_with_namespace = "bsu-cpp/students-2025-fall/student"
        with self.assertRaises(RuntimeError):
            self.api()._configure_student_project(project, self.public(), "bsu-cpp/students-2025-fall")
        project.save.assert_not_called()
        project.runners.create.assert_not_called()

    def test_unconfigured_instances_keep_runner_policy(self):
        project = self.project()
        project.shared_runners_enabled = True
        project.group_runners_enabled = True
        self.api(False)._configure_student_project(project, self.public(), "other/students")
        self.assertTrue(project.shared_runners_enabled)
        self.assertTrue(project.group_runners_enabled)
        project.runners.create.assert_not_called()
        project.variables.create.assert_not_called()

    def test_course_creation_guard_makes_no_external_calls(self):
        app = Flask(__name__)
        app.app_config = SimpleNamespace(disable_course_creation=True)
        app.storage_api = MagicMock()
        app.rms_api = MagicMock()
        with app.test_request_context():
            response, status = inspect.unwrap(create_course_api)(
                CreateCourseRequest(namespace_id=0, course_name="Test", slug="test")
            )
        self.assertEqual(status, 503)
        self.assertIn("disabled", response.get_json()["error"])
        self.assertEqual(app.storage_api.mock_calls, [])
        self.assertEqual(app.rms_api.mock_calls, [])

    def test_guard_and_runner_configuration(self):
        with patch.dict(
            "os.environ",
            {
                "MANYTASK_DISABLE_COURSE_CREATION": "true",
                "GITLAB_STUDENT_RUNNER_ID": "117",
                "GITLAB_STUDENT_RUNNER_NAMESPACE": "bsu-cpp/students-2026-fall",
            },
        ):
            config = LocalConfig.from_env()
        self.assertTrue(config.disable_course_creation)
        self.assertEqual(config.gitlab_student_runner_id, 117)


if __name__ == "__main__":
    unittest.main()
