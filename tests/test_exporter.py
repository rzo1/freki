"""Tests for freki.exporter (no network; fake python-gitlab objects)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gitlab.exceptions import GitlabListError

from freki.exporter import IssueExporter, slugify
from freki.models import ProjectInfo

TOKEN = "glpat-secret"
SECRET = "0123456789abcdef0123456789abcdef"


class FakeNotes:
    def __init__(self, notes):
        self._notes = notes

    def list(self, **kwargs):
        return [SimpleNamespace(attributes=n) for n in self._notes]


class FakeItem:
    def __init__(self, data, notes=()):
        self.attributes = data
        self.iid = data["iid"]
        self.updated_at = data.get("updated_at")
        self.notes = FakeNotes(list(notes))


class FakeManager:
    def __init__(self, items=(), error=None):
        self.items = list(items)
        self.error = error
        self.calls = 0

    def list(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        assert kwargs.get("state") == "all" and kwargs.get("iterator") is True
        return iter(self.items)


class FakeGl:
    def __init__(self, issues, mrs):
        self.api_url = "https://git.example.org/api/v4"
        self.session = None
        self._project = SimpleNamespace(
            issues=issues, mergerequests=mrs, web_url="https://git.example.org/grp/proj"
        )
        self.projects = SimpleNamespace(get=lambda pid, lazy=False: self._project)


def user(name):
    return {"username": name, "name": name.title()}


def issue(iid, title, assignees=(), updated="2024-01-02T10:00:00Z", description="", notes=()):
    return FakeItem(
        {
            "iid": iid,
            "title": title,
            "state": "opened",
            "author": user("alice"),
            "assignees": [user(a) for a in assignees],
            "labels": ["bug"],
            "milestone": {"title": "v1"},
            "created_at": "2024-01-01T09:00:00Z",
            "updated_at": updated,
            "description": description,
            "web_url": f"https://git.example.org/grp/proj/-/issues/{iid}",
        },
        notes,
    )


PROJECT = ProjectInfo(
    1,
    "grp/proj",
    "https://git.example.org/grp/proj.git",
    web_url="https://git.example.org/grp/proj",
)


def test_slugify() -> None:
    assert slugify("Fix Login Bug!  ") == "fix-login-bug"
    assert slugify("Ünïcode ümlauts") == "n-code-mlauts"
    assert slugify("") == "untitled"
    assert len(slugify("x" * 200)) == 50


def test_issue_export_layout_and_content(tmp_path: Path) -> None:
    notes = [
        {
            "author": user("bob"),
            "created_at": "2024-01-01T10:00:00Z",
            "body": "Looks fine",
            "system": False,
        },
        {
            "author": user("alice"),
            "created_at": "2024-01-01T11:00:00Z",
            "body": "closed",
            "system": True,
        },
    ]
    issues = FakeManager(
        [issue(42, "Fix login bug", ["jdoe", "bob"], notes=notes), issue(7, "Other")]
    )
    gl = FakeGl(issues, FakeManager([]))
    exporter = IssueExporter(gl, tmp_path, TOKEN)  # type: ignore[arg-type]

    issues_status, mrs_status = exporter.export(PROJECT)
    assert issues_status == "2 (2 updated)"
    assert mrs_status == "0"

    folder = tmp_path / "grp/proj-issues"
    md = folder / "0042-fix-login-bug--jdoe+bob.md"
    assert md.exists()
    assert (folder / "0042-fix-login-bug--jdoe+bob.json").exists()
    assert (folder / "0007-other.md").exists()
    text = md.read_text()
    assert text.startswith("# #42 Fix login bug")
    assert "| Assignees | Jdoe (@jdoe), Bob (@bob) |" in text
    assert "| Labels | bug |" in text and "| Milestone | v1 |" in text
    assert "## Comments (1)" in text and "### Bob (@bob) — 2024-01-01 10:00:00" in text
    assert "## Activity (1)" in text and "@alice: closed" in text
    raw = json.loads((folder / "0042-fix-login-bug--jdoe+bob.json").read_text())
    assert raw["issue"]["iid"] == 42 and len(raw["notes"]) == 2
    index = json.loads((folder / ".index.json").read_text())
    assert index["42"]["file"] == "0042-fix-login-bug--jdoe+bob.md"


def test_rerun_skips_unchanged_and_renames_on_change(tmp_path: Path) -> None:
    first = issue(1, "Old title", ["jdoe"])
    gl = FakeGl(FakeManager([first]), FakeManager([]))
    exporter = IssueExporter(gl, tmp_path, TOKEN, merge_requests=False)  # type: ignore[arg-type]
    assert exporter.export(PROJECT) == ("1 (1 updated)", None)

    # unchanged updated_at -> notes are not fetched again, nothing rewritten
    with patch.object(FakeNotes, "list", side_effect=AssertionError("should not fetch")):
        assert exporter.export(PROJECT) == ("1 (unchanged)", None)

    # title + assignee changed -> new file name, old files removed
    gl._project.issues.items = [issue(1, "New title", ["bob"], updated="2024-02-01T00:00:00Z")]
    assert exporter.export(PROJECT) == ("1 (1 updated)", None)
    names = sorted(p.name for p in (tmp_path / "grp/proj-issues").glob("0001-*"))
    assert names == ["0001-new-title--bob.json", "0001-new-title--bob.md"]


def test_attachments_are_downloaded_and_links_rewritten(tmp_path: Path) -> None:
    body = (
        f"See ![shot](/uploads/{SECRET}/shot.png) and "
        f"[doc](https://git.example.org/grp/proj/uploads/{SECRET}/spec.pdf)"
    )
    it = issue(3, "With files", description=body)
    gl = FakeGl(FakeManager([it]), FakeManager([]))
    exporter = IssueExporter(gl, tmp_path, TOKEN, merge_requests=False)  # type: ignore[arg-type]
    downloaded: list[str] = []

    def fake_download(project, gl_project, secret, filename, dest: Path) -> bool:
        downloaded.append(filename)
        if filename == "spec.pdf":
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"png")
        return True

    with patch.object(IssueExporter, "_download", side_effect=fake_download):
        status, _ = exporter.export(PROJECT)
    assert status == "1 (1 updated, 1 attachment(s) missing)"
    assert downloaded == ["shot.png", "spec.pdf"]
    folder = tmp_path / "grp/proj-issues"
    assert (folder / "attachments" / SECRET / "shot.png").read_bytes() == b"png"
    text = (folder / "0003-with-files.md").read_text()
    assert f"![shot](attachments/{SECRET}/shot.png)" in text
    assert f"https://git.example.org/grp/proj/uploads/{SECRET}/spec.pdf" in text  # kept


def test_merge_request_specific_fields(tmp_path: Path) -> None:
    mr = FakeItem(
        {
            "iid": 5,
            "title": "Add caching",
            "state": "merged",
            "author": user("alice"),
            "assignees": [],
            "reviewers": [user("bob")],
            "source_branch": "feature/cache",
            "target_branch": "main",
            "merged_at": "2024-03-01T12:00:00Z",
            "merged_by": user("bob"),
            "sha": "abc123",
            "labels": [],
            "created_at": "2024-02-01T09:00:00Z",
            "updated_at": "2024-03-01T12:00:00Z",
            "description": "",
            "web_url": "https://git.example.org/grp/proj/-/merge_requests/5",
        },
        [
            {
                "author": user("bob"),
                "created_at": "2024-02-02T00:00:00Z",
                "body": "nit",
                "system": False,
                "position": {"new_path": "src/a.py", "new_line": 12},
                "resolved": True,
            }
        ],
    )
    gl = FakeGl(FakeManager([]), FakeManager([mr]))
    exporter = IssueExporter(gl, tmp_path, TOKEN, issues=False)  # type: ignore[arg-type]
    assert exporter.export(PROJECT) == (None, "1 (1 updated)")
    text = (tmp_path / "grp/proj-merge-requests/0005-add-caching.md").read_text()
    assert text.startswith("# !5 Add caching")
    assert "| Branches | `feature/cache` → `main` |" in text
    assert "| Merged | 2024-03-01 12:00:00 by Bob (@bob) |" in text
    assert "| Reviewers | Bob (@bob) |" in text
    assert "(on `src/a.py:12`) [resolved]" in text
    raw = json.loads((tmp_path / "grp/proj-merge-requests/0005-add-caching.json").read_text())
    assert raw["merge_request"]["iid"] == 5


def test_disabled_feature_and_errors(tmp_path: Path) -> None:
    disabled = GitlabListError(response_code=403, error_message="403 Forbidden")
    broken = GitlabListError(response_code=500, error_message=f"boom {TOKEN}")
    gl = FakeGl(FakeManager(error=disabled), FakeManager(error=broken))
    exporter = IssueExporter(gl, tmp_path, TOKEN)  # type: ignore[arg-type]
    issues_status, mrs_status = exporter.export(PROJECT)
    assert issues_status == "0"
    assert mrs_status is not None and mrs_status.startswith("failed:")
    assert TOKEN not in mrs_status
    assert not (tmp_path / "grp/proj-issues").exists()
