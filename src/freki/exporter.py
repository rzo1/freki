"""Export issues and merge requests (with comments and attachments) to Markdown.

Layout per project (next to the git mirror)::

    <ns>/<project>-issues/0042-fix-login--jdoe.md      human readable
    <ns>/<project>-issues/0042-fix-login--jdoe.json    raw API data (issue + notes)
    <ns>/<project>-issues/attachments/<secret>/<file>  downloaded uploads
    <ns>/<project>-issues/.index.json                  iid -> updated_at (skip unchanged)
    <ns>/<project>-merge-requests/...                  same for merge requests

Attachment links (``/uploads/<secret>/<file>``) inside descriptions and
comments are rewritten to the local ``attachments/`` copies.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import gitlab
from gitlab.exceptions import GitlabError

from freki.models import ProjectInfo

log = logging.getLogger(__name__)

Kind = Literal["issues", "merge-requests"]

# ``/uploads/<32 hex>/<filename>`` in any of the forms GitLab renders them:
#   /uploads/<secret>/<file>
#   /<group>/<project>/uploads/<secret>/<file>
#   /-/project/<id>/uploads/<secret>/<file>
#   https://host/<any of the above>
_UPLOAD_RE = re.compile(
    r"(?:https?://[^\s()<>\"']+?)?(?:/[^\s()<>\"']*?)?/uploads/([0-9a-f]{32})/([^\s()<>\"']+)"
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_INDEX_FILE = ".index.json"


def slugify(text: str, max_len: int = 50) -> str:
    """Lower-case, ASCII-ish slug of ``text`` (``Fix login bug!`` -> ``fix-login-bug``)."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "untitled"


def _username(user: Any) -> str | None:
    if not user:
        return None
    if isinstance(user, dict):
        return user.get("username") or user.get("name")
    return getattr(user, "username", None) or getattr(user, "name", None)


def _display(user: Any) -> str:
    """``Jane Doe (@jdoe)`` for a user dict, ``-`` when absent."""
    if not user:
        return "-"
    if not isinstance(user, dict):
        user = getattr(user, "attributes", None) or {"username": str(user)}
    name, username = user.get("name"), user.get("username")
    if name and username:
        return f"{name} (@{username})"
    return f"@{username}" if username else str(name or "-")


def _date(value: str | None) -> str:
    return value.replace("T", " ")[:19] if value else "-"


def _safe_filename(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").strip()
    return name or "file"


@dataclass
class ExportOutcome:
    """Result of exporting one kind (issues or merge requests) of one project."""

    total: int = 0
    written: int = 0
    attachments: int = 0
    attachment_failures: int = 0
    error: str | None = None

    def status(self) -> str:
        """Compact status for the summary table."""
        if self.error:
            return f"failed: {self.error}"
        if self.total == 0:
            return "0"
        parts = [f"{self.total}"]
        parts.append(f"{self.written} updated" if self.written else "unchanged")
        if self.attachment_failures:
            parts.append(f"{self.attachment_failures} attachment(s) missing")
        return " ".join(parts[:1]) + " (" + ", ".join(parts[1:]) + ")"


class IssueExporter:
    """Export issues and merge requests of projects via the GitLab API."""

    def __init__(
        self,
        gl: gitlab.Gitlab,
        output_dir: Path,
        token: str,
        issues: bool = True,
        merge_requests: bool = True,
    ) -> None:
        self.gl = gl
        self.output_dir = Path(output_dir).resolve()
        self._token = token
        self.issues = issues
        self.merge_requests = merge_requests
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- public API

    def export(self, project: ProjectInfo) -> tuple[str | None, str | None]:
        """Export issues and merge requests of ``project``; return (issues, mrs) status."""
        issues = mrs = None
        if self.issues:
            issues = self._export_kind(project, "issues").status()
        if self.merge_requests:
            mrs = self._export_kind(project, "merge-requests").status()
        return issues, mrs

    # ----------------------------------------------------------------- internals

    def _export_kind(self, project: ProjectInfo, kind: Kind) -> ExportOutcome:
        outcome = ExportOutcome()
        target = self.output_dir / f"{project.path_with_namespace}-{kind}"
        try:
            gl_project = self.gl.projects.get(project.id, lazy=True)
            manager = gl_project.issues if kind == "issues" else gl_project.mergerequests
            items = manager.list(state="all", iterator=True, order_by="created_at", sort="asc")
            index = self._load_index(target)
            seen: dict[str, dict[str, Any]] = {}
            for item in items:
                outcome.total += 1
                iid = str(item.iid)
                entry = index.get(iid)
                updated = getattr(item, "updated_at", None)
                if entry and entry.get("updated_at") == updated and self._exists(target, entry):
                    seen[iid] = entry
                    continue
                filename = self._write_item(project, gl_project, kind, item, target, outcome)
                seen[iid] = {"updated_at": updated, "file": filename}
                outcome.written += 1
            if outcome.total or target.exists():
                self._save_index(target, seen)
        except GitlabError as exc:
            code = getattr(exc, "response_code", None)
            if code in (403, 404) and outcome.total == 0:
                # feature disabled for this project
                return outcome
            outcome.error = self._sanitize(str(exc))
            log.warning("%s export failed for %s: %s", kind, project.path_with_namespace, exc)
        except Exception as exc:  # never abort the run because of one project
            outcome.error = self._sanitize(str(exc))
            log.warning("%s export failed for %s: %s", kind, project.path_with_namespace, exc)
        if outcome.written:
            log.info(
                "%s: %d %s exported (%d updated, %d attachment(s))",
                project.path_with_namespace,
                outcome.total,
                kind,
                outcome.written,
                outcome.attachments,
            )
        return outcome

    @staticmethod
    def _exists(target: Path, entry: dict[str, Any]) -> bool:
        return bool(entry.get("file")) and (target / entry["file"]).exists()

    @staticmethod
    def _load_index(target: Path) -> dict[str, dict[str, Any]]:
        path = target / _INDEX_FILE
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save_index(target: Path, index: dict[str, dict[str, Any]]) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / _INDEX_FILE).write_text(
            json.dumps(index, indent=1, sort_keys=True), encoding="utf-8"
        )

    def _write_item(
        self,
        project: ProjectInfo,
        gl_project: Any,
        kind: Kind,
        item: Any,
        target: Path,
        outcome: ExportOutcome,
    ) -> str:
        data = dict(item.attributes)
        notes = [
            dict(n.attributes)
            for n in item.notes.list(iterator=True, order_by="created_at", sort="asc")
        ]
        target.mkdir(parents=True, exist_ok=True)

        stem = self._stem(data)
        # A changed title/assignee changes the file name: drop old files of this iid.
        prefix = f"{int(data['iid']):04d}-"
        for old in target.glob(f"{prefix}*"):
            if old.stem != stem and old.suffix in (".md", ".json"):
                old.unlink()

        def rewrite(text: str | None) -> str:
            return self._localize_attachments(text or "", project, gl_project, target, outcome)

        markdown = self._render(kind, data, notes, rewrite)
        (target / f"{stem}.md").write_text(markdown, encoding="utf-8")
        (target / f"{stem}.json").write_text(
            json.dumps(
                {kind[:-1] if kind == "issues" else "merge_request": data, "notes": notes},
                indent=1,
                sort_keys=True,
                default=str,
            ),
            encoding="utf-8",
        )
        return f"{stem}.md"

    @staticmethod
    def _stem(data: dict[str, Any]) -> str:
        iid = int(data["iid"])
        assignees = [_username(a) for a in (data.get("assignees") or []) if _username(a)] or (
            [_username(data["assignee"])] if data.get("assignee") else []
        )
        stem = f"{iid:04d}-{slugify(str(data.get('title') or ''))}"
        if assignees:
            stem += "--" + "+".join(slugify(a, 30) for a in assignees)
        return stem

    # --------------------------------------------------------------- rendering

    def _render(
        self, kind: Kind, data: dict[str, Any], notes: list[dict[str, Any]], rewrite: Any
    ) -> str:
        iid = data["iid"]
        prefix = "#" if kind == "issues" else "!"
        lines = [f"# {prefix}{iid} {data.get('title', '')}", ""]

        rows: list[tuple[str, str]] = [
            ("Type", "Issue" if kind == "issues" else "Merge request"),
            ("State", str(data.get("state", "-"))),
            ("Author", _display(data.get("author"))),
        ]
        assignees = data.get("assignees") or ([data["assignee"]] if data.get("assignee") else [])
        rows.append(("Assignees", ", ".join(_display(a) for a in assignees) or "-"))
        if kind == "merge-requests":
            rows.append(
                ("Reviewers", ", ".join(_display(r) for r in data.get("reviewers") or []) or "-")
            )
            rows.append(
                ("Branches", f"`{data.get('source_branch')}` → `{data.get('target_branch')}`")
            )
            if data.get("merged_at"):
                rows.append(
                    (
                        "Merged",
                        f"{_date(data.get('merged_at'))} by "
                        f"{_display(data.get('merged_by') or data.get('merge_user'))}",
                    )
                )
            if data.get("sha"):
                rows.append(("Head SHA", f"`{data['sha']}`"))
            if data.get("merge_commit_sha"):
                rows.append(("Merge commit", f"`{data['merge_commit_sha']}`"))
            if data.get("draft"):
                rows.append(("Draft", "yes"))
        rows.append(("Labels", ", ".join(str(label) for label in data.get("labels") or []) or "-"))
        milestone = data.get("milestone")
        rows.append(
            (
                "Milestone",
                (milestone or {}).get("title", "-") if isinstance(milestone, dict) else "-",
            )
        )
        rows.append(("Created", _date(data.get("created_at"))))
        rows.append(("Updated", _date(data.get("updated_at"))))
        if data.get("closed_at"):
            rows.append(
                ("Closed", f"{_date(data.get('closed_at'))} by {_display(data.get('closed_by'))}")
            )
        if data.get("due_date"):
            rows.append(("Due", str(data["due_date"])))
        if data.get("weight") is not None:
            rows.append(("Weight", str(data["weight"])))
        rows.append(("URL", str(data.get("web_url", "-"))))

        lines += ["| | |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in rows]
        lines += ["", "## Description", "", rewrite(data.get("description")) or "_(empty)_", ""]

        comments = [n for n in notes if not n.get("system")]
        activity = [n for n in notes if n.get("system")]

        lines += [f"## Comments ({len(comments)})", ""]
        if not comments:
            lines += ["_(none)_", ""]
        for note in comments:
            header = f"### {_display(note.get('author'))} — {_date(note.get('created_at'))}"
            pos = note.get("position") or {}
            if pos.get("new_path"):
                where = pos["new_path"]
                if pos.get("new_line"):
                    where += f":{pos['new_line']}"
                header += f" (on `{where}`)"
            if note.get("resolved"):
                header += " [resolved]"
            lines += [header, "", rewrite(note.get("body")), ""]

        if activity:
            lines += [f"## Activity ({len(activity)})", ""]
            for note in activity:
                body = (note.get("body") or "").strip().replace("\n", " ")
                who = _username(note.get("author")) or "?"
                lines.append(f"- {_date(note.get('created_at'))} @{who}: {body}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------- attachments

    def _localize_attachments(
        self, text: str, project: ProjectInfo, gl_project: Any, target: Path, outcome: ExportOutcome
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            secret, filename = match.group(1), match.group(2)
            local = target / "attachments" / secret / _safe_filename(filename)
            if not local.exists():
                if self._download(project, gl_project, secret, filename, local):
                    outcome.attachments += 1
                else:
                    outcome.attachment_failures += 1
                    return match.group(0)
            return f"attachments/{secret}/{_safe_filename(filename)}"

        return _UPLOAD_RE.sub(replace, text)

    def _download(
        self, project: ProjectInfo, gl_project: Any, secret: str, filename: str, dest: Path
    ) -> bool:
        """Fetch one upload: project uploads API first, web URL with the token second."""
        candidates = [
            f"{self.gl.api_url}/projects/{project.id}/uploads/{secret}/{filename}",
        ]
        web_url = project.web_url or getattr(gl_project, "web_url", None)
        if web_url:
            candidates.append(f"{web_url}/uploads/{secret}/{filename}")
        for url in candidates:
            try:
                response = self.gl.session.get(
                    url,
                    headers={"PRIVATE-TOKEN": self._token},
                    stream=True,
                    timeout=60,
                    allow_redirects=True,
                )
            except Exception as exc:  # network errors: try the next candidate
                log.debug("download %s failed: %s", url, exc)
                continue
            content_type = response.headers.get("content-type", "")
            if response.status_code != 200 or content_type.startswith("text/html"):
                response.close()
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    fh.write(chunk)
            return True
        log.warning(
            "could not download attachment %s/%s of %s",
            secret,
            filename,
            project.path_with_namespace,
        )
        return False

    def _sanitize(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text


def export_all(
    exporter: IssueExporter, projects: Iterable[ProjectInfo]
) -> None:  # pragma: no cover
    """Convenience for scripts: export every project sequentially."""
    for project in projects:
        exporter.export(project)
