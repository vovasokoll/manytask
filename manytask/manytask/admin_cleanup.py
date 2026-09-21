"""SSH-admin-only course cleanup. Dry-run by default; never delete global GitLab users."""

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import time
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from manytask.models import Course, Grade, User, UserOnCourse

GITLAB_MAINTAINER = 40


class CleanupError(RuntimeError):
    """A safe, credential-free error suitable for an administrative log."""


@dataclass(frozen=True)
class Target:
    course: str
    username: str
    user_id: int
    gitlab_user_id: int
    project_id: int
    permanent: bool = False

    def __post_init__(self):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", self.username):
            raise CleanupError("Expected an exact username, not a path or pattern")
        if min(self.user_id, self.gitlab_user_id, self.project_id) <= 0:
            raise CleanupError("All pinned IDs must be positive")


class GitLab:
    def __init__(self, url, token):
        if not url.startswith("https://") or not token:
            raise CleanupError("HTTPS GitLab URL and administrative token are required")
        self.url = url.rstrip("/") + "/api/v4/"
        self.http = requests.Session()
        self.http.headers["PRIVATE-TOKEN"] = token

    def request(self, method, path, *, missing=False, allow_moved=False, **kwargs):
        try:
            response = self.http.request(method, self.url + path, timeout=30, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise CleanupError("GitLab transport failure; inspect the journal before retrying") from None
        if missing and response.status_code == HTTPStatus.NOT_FOUND:
            return None
        if allow_moved and response.status_code == HTTPStatus.MOVED_PERMANENTLY:
            return response
        if response.status_code not in (200, 201, 202, 204):
            # Response bodies and URLs can contain secrets: deliberately omit them.
            raise CleanupError(f"GitLab {method} failed with HTTP {response.status_code}")
        return response

    def get(self, path, **params):
        result = self.request("GET", path, missing=True, allow_moved=path.startswith("projects/"), params=params)
        if result is not None and result.status_code == HTTPStatus.MOVED_PERMANENTLY:
            destination = urljoin(self.url, result.headers.get("Location", ""))
            relative = destination.removeprefix(self.url)
            if destination == relative or not re.fullmatch(r"projects/[1-9][0-9]*", relative):
                raise CleanupError("Unsafe project redirect refused")
            result = self.request("GET", relative, missing=True, params=params)
        return None if result is None else result.json()

    def listing(self, path, **params):
        result = []
        for page in range(1, 101):
            response = self.request("GET", path, params={**params, "per_page": 100, "page": page})
            result.extend(response.json())
            if not response.headers.get("X-Next-Page"):
                return result
        raise CleanupError("Pagination limit reached; refusing incomplete inspection")

    def export(self, project_id, destination):
        path = f"projects/{project_id}/export"
        self.request("POST", path)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = self.get(path)
            if status and status.get("export_status") == "finished":
                response = self.request("GET", path + "/download", stream=True)
                with destination.open("xb") as out:
                    os.chmod(destination, 0o600)
                    for chunk in response.iter_content(1024 * 1024):
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if destination.stat().st_size == 0:
                    raise CleanupError("Empty project export; nothing has been deleted")
                if not tarfile.is_tarfile(destination):
                    raise CleanupError("Invalid project export; nothing has been deleted")
                return
            time.sleep(2)
        raise CleanupError("Project export timed out; nothing has been deleted")


def row_data(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def database_plan(session, target):
    course = session.scalar(select(Course).where(Course.name == target.course))
    if course is None:
        raise CleanupError("Course not found")
    user = session.scalar(select(User).where(User.id == target.user_id).with_for_update())
    by_name = session.scalar(select(User).where(User.username == target.username))
    if by_name is not None and by_name.id != target.user_id:
        raise CleanupError("Username belongs to another Manytask ID")
    if user is not None and (user.username != target.username or user.rms_id != str(target.gitlab_user_id)):
        raise CleanupError("Pinned Manytask/GitLab identity mismatch")
    if user is not None and (
        user.is_instance_admin
        or user.users_on_namespaces.count()
        or user.created_namespaces.count()
        or user.assigned_users_on_namespaces.count()
    ):
        raise CleanupError("Refusing to remove an administrator or namespace-related account")
    memberships = session.scalars(
        select(UserOnCourse).where(UserOnCourse.user_id == target.user_id).with_for_update()
    ).all()
    membership = next((item for item in memberships if item.course_id == course.id), None)
    if membership is not None and membership.is_course_admin:
        raise CleanupError("Refusing to remove a course administrator")
    others = [item.course_id for item in memberships if item.course_id != course.id]
    if any(
        item.course.gitlab_course_students_group == course.gitlab_course_students_group
        for item in memberships
        if item.course_id != course.id
    ):
        raise CleanupError("Another course uses the same students namespace")
    grades = (
        []
        if membership is None
        else session.scalars(
            select(Grade).where(Grade.user_on_course_id == membership.id).order_by(Grade.id).with_for_update()
        ).all()
    )
    backup = {
        "user": None if user is None else row_data(user),
        "membership": None if membership is None else row_data(membership),
        "grades": [row_data(grade) for grade in grades],
    }
    plan = {
        "course": target.course,
        "course_id": course.id,
        "students_group": course.gitlab_course_students_group,
        "username": target.username,
        "user_id": target.user_id,
        "gitlab_user_id": target.gitlab_user_id,
        "membership_id": None if membership is None else membership.id,
        "grade_count": len(grades),
        "other_course_ids_preserved": sorted(others),
        "delete_manytask_user": user is not None and not others,
        "database_snapshot_sha256": fingerprint(backup),
    }
    return plan, backup


def external_plan(api, target, students_group):
    group = api.get("groups/" + quote(students_group, safe=""))
    if group is None or group["full_path"] != students_group:
        raise CleanupError("Students namespace cannot be verified")
    user = api.get(f"users/{target.gitlab_user_id}")
    if user and (user["username"] != target.username or user.get("is_admin")):
        raise CleanupError("GitLab username changed or account is an administrator")
    names = api.listing("users", username=target.username)
    if any(item["id"] != target.gitlab_user_id for item in names):
        raise CleanupError("GitLab username has been reused")
    group_path = f"groups/{group['id']}/members"
    direct = api.get(f"{group_path}/{target.gitlab_user_id}")
    effective = api.get(f"{group_path}/all/{target.gitlab_user_id}")
    if effective and (not direct or effective["access_level"] >= GITLAB_MAINTAINER):
        raise CleanupError("Inherited or administrative group access needs separate review")
    expected_path = students_group + "/" + target.username
    project = api.get(f"projects/{target.project_id}")
    by_path = api.get("projects/" + quote(expected_path, safe=""))
    if by_path and by_path["id"] != target.project_id:
        raise CleanupError("Project path has been reused by another project")
    pending_deletion = bool(
        project and (project.get("marked_for_deletion_on") or project.get("marked_for_deletion_at"))
    )
    allowed_paths = [expected_path]
    if pending_deletion:
        allowed_paths.append(expected_path + "-deletion_scheduled-" + str(target.project_id))
    if project and (project["path_with_namespace"] not in allowed_paths or project["namespace"]["id"] != group["id"]):
        raise CleanupError("Pinned project is outside the exact course/user namespace")
    if project:
        members = api.listing(f"projects/{target.project_id}/members")
        if any(m["id"] != target.gitlab_user_id and m["access_level"] < GITLAB_MAINTAINER for m in members):
            raise CleanupError("Project has another student as a direct member")
        pipelines = api.listing(f"projects/{target.project_id}/pipelines", scope="running")
        pending = api.listing(f"projects/{target.project_id}/pipelines", scope="pending")
        if pipelines or pending:
            raise CleanupError("Project has running/pending pipelines; retry after they finish")
    return {
        "group_id": group["id"],
        "project_id": target.project_id,
        "project_path": project["path_with_namespace"] if project else expected_path,
        "project_present": project is not None,
        "project_pending_deletion": pending_deletion,
        "remove_direct_group_membership": direct is not None,
        "global_gitlab_account": "preserved" if user else "already_absent",
    }


def build_plan(session, api, target):
    plan, backup = database_plan(session, target)
    plan.update(external_plan(api, target, plan["students_group"]))
    plan["gitlab_api"] = api.url
    plan["permanently_remove_project"] = target.permanent
    if target.permanent and plan["project_present"] and not plan["project_pending_deletion"]:
        raise CleanupError("Permanent removal requires a project already marked for deletion")
    return plan, backup


def private_json(path, data):
    with path.open("x") as out:
        os.chmod(path, 0o600)
        json.dump(data, out, indent=2, sort_keys=True, default=str)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())


def delete_database_rows(session, plan):
    membership = session.get(UserOnCourse, plan["membership_id"]) if plan["membership_id"] else None
    if membership:
        session.delete(membership)  # ORM cascade deletes only this membership's grades.
        session.flush()
    if plan["delete_manytask_user"]:
        user = session.get(User, plan["user_id"])
        if user.users_on_courses.count():
            raise CleanupError("A new course membership appeared; refusing account deletion")
        session.delete(user)
    session.flush()


def apply_cleanup(session, api, target, confirmation, backup_dir):
    plan, backup = build_plan(session, api, target)
    if confirmation != fingerprint(plan):
        raise CleanupError("Confirmation does not match the current dry-run; review a fresh plan")
    if backup_dir is None or not backup_dir.is_absolute():
        raise CleanupError("An absolute new backup directory is required")
    backup_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    private_json(backup_dir / "plan.json", plan)
    private_json(backup_dir / "database.json", backup)
    if plan["project_present"]:
        api.export(target.project_id, backup_dir / "project-export.tar.gz")
    # Revalidate external identity after the potentially slow backup.
    if external_plan(api, target, plan["students_group"]) != {key: plan[key] for key in external_plan_keys()}:
        raise CleanupError("GitLab state changed during export; review a fresh plan")
    if plan["remove_direct_group_membership"]:
        api.request(
            "DELETE",
            f"groups/{plan['group_id']}/members/{target.gitlab_user_id}",
            missing=True,
            params={"skip_subresources": "true"},
        )
        if api.get(f"groups/{plan['group_id']}/members/all/{target.gitlab_user_id}"):
            raise CleanupError("Group access remains; database deletion has not started")
    if plan["project_present"]:
        if target.permanent:
            api.request(
                "DELETE",
                f"projects/{target.project_id}",
                missing=True,
                params={"permanently_remove": "true", "full_path": plan["project_path"]},
            )
        elif not plan["project_pending_deletion"]:
            api.request("DELETE", f"projects/{target.project_id}", missing=True)
    private_json(backup_dir / "gitlab-delete-requested.json", {"project_id": target.project_id})
    delete_database_rows(session, plan)
    session.commit()
    project = api.get(f"projects/{target.project_id}")
    result = {
        "database": "removed",
        "global_gitlab_account": plan["global_gitlab_account"],
        "project": "absent" if project is None else "deletion_requested_check_again",
        "backup_directory": str(backup_dir),
    }
    private_json(backup_dir / "result.json", result)
    return result


def external_plan_keys():
    return (
        "group_id",
        "project_id",
        "project_path",
        "project_present",
        "project_pending_deletion",
        "remove_direct_group_membership",
        "global_gitlab_account",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--course", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--gitlab-user-id", type=int)
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--permanent", action="store_true", help="Purge a project already marked for deletion")
    parser.add_argument("--confirm", help="SHA256 printed by a fresh dry-run")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        api = GitLab(os.environ["GITLAB_URL"], os.environ["GITLAB_ADMIN_TOKEN"])
        with Session(create_engine(os.environ["DATABASE_URL"], echo=False)) as session:
            target = resolve_target(session, api, args)
            if args.apply:
                result = apply_cleanup(session, api, target, args.confirm, args.backup_dir)
            else:
                plan, _ = build_plan(session, api, target)
                result = {"dry_run": True, "plan": plan, "confirmation": fingerprint(plan)}
            print(json.dumps(result, indent=2, sort_keys=True))
    except CleanupError as error:
        print(f"Cleanup stopped: {error}", file=sys.stderr)
        return 1
    except Exception:
        # Never print SQL parameters, environment, tokens or HTTP response bodies.
        print("Cleanup failed; inspect protected backup/journal and run dry-run before retrying", file=sys.stderr)
        return 1
    return 0


def resolve_target(session, api, args):
    if args.apply and not all((args.user_id, args.gitlab_user_id, args.project_id)):
        raise CleanupError("Apply requires all three pinned IDs from the dry-run")
    if not all((args.user_id, args.gitlab_user_id, args.project_id)):
        user = session.scalar(select(User).where(User.username == args.username))
        course = session.scalar(select(Course).where(Course.name == args.course))
        if user is None or course is None:
            raise CleanupError("Cannot discover target; specify IDs from the previous journal")
        path = course.gitlab_course_students_group + "/" + args.username
        project = api.get("projects/" + quote(path, safe=""))
        if project is None and args.project_id is None:
            raise CleanupError("Project already absent; specify its ID from the previous journal")
        return Target(
            args.course,
            args.username,
            args.user_id or user.id,
            args.gitlab_user_id or int(user.rms_id),
            args.project_id or project["id"],
            args.permanent,
        )
    return Target(args.course, args.username, args.user_id, args.gitlab_user_id, args.project_id, args.permanent)


if __name__ == "__main__":
    sys.exit(main())
