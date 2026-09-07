"""Tests for the Typer CLI (no network: client, discovery and cloner are patched)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from gitlab.exceptions import GitlabAuthenticationError, GitlabGetError
from typer.testing import CliRunner

from freki import cli
from freki.cloner import CloneResult
from freki.models import ProjectInfo

runner = CliRunner()

TOKEN = "glpat-test-token"


def info(path: str, pid: int = 1, wiki: bool = False, archived: bool = False) -> ProjectInfo:
    return ProjectInfo(
        id=pid,
        path_with_namespace=path,
        http_url_to_repo=f"https://git.example.org/{path}.git",
        archived=archived,
        wiki_enabled=wiki,
    )


class FakeDiscovery:
    """Records how it was called and returns canned projects."""

    projects: list[ProjectInfo] = []
    instances: list[FakeDiscovery] = []

    def __init__(self, gl, include_archived: bool = False) -> None:
        self.gl = gl
        self.include_archived = include_archived
        self.calls: list[tuple] = []
        FakeDiscovery.instances.append(self)

    def get_project(self, id_or_path):
        self.calls.append(("project", id_or_path))
        return self.projects[0] if self.projects else None

    def get_group_projects(self, id_or_path, with_shared=False):
        self.calls.append(("group", id_or_path, with_shared))
        return list(self.projects)

    def get_user_projects(self, with_shared=False):
        self.calls.append(("user", with_shared))
        return list(self.projects)


class FakeCloner:
    """Returns one CloneResult per project with a configurable status."""

    statuses: dict[str, str] = {}
    wiki_statuses: dict[str, str] = {}
    lfs_statuses: dict[str, str] = {}
    messages: dict[str, str] = {}
    instances: list[FakeCloner] = []

    def __init__(
        self,
        output_dir,
        token,
        clone_wiki=True,
        clone_lfs=True,
        dry_run=False,
        protocol="ssh",
        checkout=True,
        exporter=None,
    ):
        self.checkout = checkout
        self.exporter = exporter
        self.output_dir = Path(output_dir)
        self.token = token
        self.protocol = protocol
        self.clone_wiki = clone_wiki
        self.clone_lfs = clone_lfs
        self.dry_run = dry_run
        self.concurrency: int | None = None
        self.projects: list[ProjectInfo] = []
        FakeCloner.instances.append(self)

    def clone_all(self, projects, concurrency=4):
        self.projects = list(projects)
        self.concurrency = concurrency
        results = []
        for p in self.projects:
            if self.dry_run:
                results.append(CloneResult(p.path_with_namespace, "skipped", "dry-run"))
                continue
            name = p.path_with_namespace
            status = self.statuses.get(name, "cloned")
            wiki = self.wiki_statuses.get(
                name, "cloned" if (self.clone_wiki and p.wiki_enabled) else None
            )
            lfs = self.lfs_statuses.get(name, "fetched" if self.clone_lfs else None)
            results.append(
                CloneResult(
                    name,
                    status,  # type: ignore[arg-type]
                    message=self.messages.get(name, "boom" if status == "failed" else ""),
                    wiki_status=wiki,
                    lfs_status=lfs,
                )
            )
        return results


@pytest.fixture(autouse=True)
def patched(monkeypatch, tmp_path):
    """Isolate from env/.env and swap out every network/disk-touching collaborator."""
    monkeypatch.chdir(tmp_path)  # no .env file here
    for var in (
        "GITLAB_URL",
        "GITLAB_TOKEN",
        "GITLAB_OUTPUT_DIR",
        "GITLAB_INCLUDE_ARCHIVED",
        "GITLAB_CLONE_WIKI",
        "GITLAB_CLONE_LFS",
        "GITLAB_CONCURRENCY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITLAB_TOKEN", TOKEN)
    monkeypatch.setenv("GITLAB_URL", "https://git.example.org")

    client = SimpleNamespace(user=SimpleNamespace(username="alice"))
    seen_settings = []

    def fake_get_client(settings):
        seen_settings.append(settings)
        return client

    FakeDiscovery.projects = [info("grp/one", 1, wiki=True), info("grp/sub/two", 2)]
    FakeDiscovery.instances = []
    FakeCloner.statuses = {}
    FakeCloner.wiki_statuses = {}
    FakeCloner.lfs_statuses = {}
    FakeCloner.messages = {}
    FakeCloner.instances = []
    monkeypatch.setattr(cli, "get_client", fake_get_client)
    monkeypatch.setattr(cli, "ProjectDiscovery", FakeDiscovery)
    monkeypatch.setattr(cli, "GitCloner", FakeCloner)
    return SimpleNamespace(settings=seen_settings, client=client)


def test_help_lists_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("group", "project", "user"):
        assert cmd in result.output


def test_no_args_shows_help():
    result = runner.invoke(cli.app, [])
    assert "Usage" in result.output


def test_group_command_numeric_id_and_defaults(patched):
    result = runner.invoke(cli.app, ["group", "42"])
    assert result.exit_code == 0, result.output
    assert "Authenticated as alice" in result.output

    disc = FakeDiscovery.instances[0]
    assert disc.gl is patched.client
    assert disc.include_archived is False
    assert disc.calls == [("group", 42, False)]

    cloner = FakeCloner.instances[0]
    assert cloner.token == TOKEN
    assert cloner.output_dir == Path("gitlab-export")
    assert cloner.clone_wiki is True and cloner.clone_lfs is True
    assert cloner.dry_run is False
    assert cloner.concurrency == 4
    assert [p.path_with_namespace for p in cloner.projects] == ["grp/one", "grp/sub/two"]

    assert "grp/one" in result.output and "grp/sub/two" in result.output
    assert "2 cloned" in result.output and "0 failed" in result.output
    assert TOKEN not in result.output


def test_group_command_path_and_overrides(patched, tmp_path):
    out = tmp_path / "mirrors"
    result = runner.invoke(
        cli.app,
        [
            "group",
            "faculty/course",
            "--url",
            "https://gl.example.org/",
            "--token",
            "other-token",
            "-o",
            str(out),
            "--include-archived",
            "--no-wiki",
            "--no-lfs",
            "-j",
            "7",
            "--with-shared",
        ],
    )
    assert result.exit_code == 0, result.output
    settings = patched.settings[0]
    assert settings.gitlab_url == "https://gl.example.org/"
    assert settings.gitlab_token.get_secret_value() == "other-token"

    disc = FakeDiscovery.instances[0]
    assert disc.include_archived is True
    assert disc.calls == [("group", "faculty/course", True)]

    cloner = FakeCloner.instances[0]
    assert cloner.token == "other-token"
    assert cloner.output_dir == out
    assert cloner.clone_wiki is False and cloner.clone_lfs is False
    assert cloner.concurrency == 7
    assert "other-token" not in result.output


def test_env_vars_are_used_as_defaults(patched, monkeypatch, tmp_path):
    monkeypatch.setenv("GITLAB_CONCURRENCY", "9")
    monkeypatch.setenv("GITLAB_CLONE_WIKI", "false")
    monkeypatch.setenv("GITLAB_OUTPUT_DIR", str(tmp_path / "from-env"))
    result = runner.invoke(cli.app, ["user"])
    assert result.exit_code == 0, result.output
    cloner = FakeCloner.instances[0]
    assert cloner.concurrency == 9
    assert cloner.clone_wiki is False
    assert cloner.output_dir == tmp_path / "from-env"


def test_project_command(patched):
    FakeDiscovery.projects = [info("grp/single", 5, wiki=True)]
    result = runner.invoke(cli.app, ["project", "grp/single"])
    assert result.exit_code == 0, result.output
    assert FakeDiscovery.instances[0].calls == [("project", "grp/single")]
    assert [p.id for p in FakeCloner.instances[0].projects] == [5]
    assert "1 cloned" in result.output


def test_project_numeric_id_is_int(patched):
    result = runner.invoke(cli.app, ["project", "123"])
    assert result.exit_code == 0, result.output
    assert FakeDiscovery.instances[0].calls == [("project", 123)]


def test_project_archived_skipped_gives_no_projects(patched):
    FakeDiscovery.projects = []  # get_project returns None -> nothing to do
    result = runner.invoke(cli.app, ["project", "grp/archived"])
    assert result.exit_code == 0, result.output
    assert "No projects" in result.output
    assert FakeCloner.instances == []


def test_user_command(patched):
    result = runner.invoke(cli.app, ["user", "--with-shared"])
    assert result.exit_code == 0, result.output
    assert FakeDiscovery.instances[0].calls == [("user", True)]
    assert len(FakeCloner.instances[0].projects) == 2


def test_dry_run(patched):
    result = runner.invoke(cli.app, ["group", "grp", "--dry-run"])
    assert result.exit_code == 0, result.output
    cloner = FakeCloner.instances[0]
    assert cloner.dry_run is True
    assert "Dry run" in result.output
    assert "2 skipped" in result.output and "0 failed" in result.output


def test_failure_exit_code(patched):
    FakeCloner.statuses = {"grp/sub/two": "failed"}
    result = runner.invoke(cli.app, ["group", "grp"])
    assert result.exit_code == 1, result.output
    assert "1 cloned" in result.output and "1 failed" in result.output
    assert "boom" in result.output


def test_wiki_failure_exit_code(patched):
    FakeCloner.wiki_statuses = {"grp/one": "failed: SSL error"}
    result = runner.invoke(cli.app, ["group", "grp"])
    assert result.exit_code == 1, result.output
    assert "2 cloned" in result.output and "0 failed" in result.output
    assert "1 partial" in result.output


def test_lfs_failure_exit_code(patched):
    FakeCloner.lfs_statuses = {"grp/sub/two": "failed: 401"}
    result = runner.invoke(cli.app, ["user"])
    assert result.exit_code == 1, result.output
    assert "1 partial" in result.output


def test_empty_wiki_is_not_a_failure(patched):
    FakeCloner.wiki_statuses = {"grp/one": "empty"}
    FakeCloner.lfs_statuses = {"grp/one": "skipped: git-lfs not installed"}
    result = runner.invoke(cli.app, ["group", "grp"])
    assert result.exit_code == 0, result.output
    assert "partial" not in result.output


def test_output_with_markup_like_text_does_not_crash(patched):
    FakeCloner.statuses = {"grp/one": "failed"}
    FakeCloner.messages = {"grp/one": "fatal: [/b] bad [foo] ref"}
    FakeCloner.wiki_statuses = {"grp/sub/two": "failed: [bold]nope[/bold]"}
    # wide terminal so the summary table does not fold the cells we look for
    result = runner.invoke(cli.app, ["group", "grp"], env={"COLUMNS": "250"})
    assert result.exit_code == 1, result.output
    assert "[/b] bad [foo] ref" in result.output
    assert "[bold]nope[/bold]" in result.output


def test_discovery_error_message_is_escaped(patched, monkeypatch):
    def missing(self, id_or_path, with_shared=False):
        raise GitlabGetError("404 [/bold] Group Not Found [x]")

    monkeypatch.setattr(FakeDiscovery, "get_group_projects", missing)
    result = runner.invoke(cli.app, ["group", "gr[/b]oup"])
    assert result.exit_code == 2, result.output
    assert "gr[/b]oup" in result.output
    assert "[x]" in result.output


def test_auth_error_exit_2(patched, monkeypatch):
    def bad_client(settings):
        raise GitlabAuthenticationError("401 Unauthorized")

    monkeypatch.setattr(cli, "get_client", bad_client)
    result = runner.invoke(cli.app, ["user"])
    assert result.exit_code == 2
    assert "authentication" in result.output.lower()
    assert FakeCloner.instances == []


def test_missing_token_exit_2(patched, monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN")
    result = runner.invoke(cli.app, ["user"])
    assert result.exit_code == 2
    assert "token" in result.output.lower()


def test_group_not_found_exit_2(patched, monkeypatch):
    def missing(self, id_or_path, with_shared=False):
        raise GitlabGetError("404 Group Not Found")

    monkeypatch.setattr(FakeDiscovery, "get_group_projects", missing)
    result = runner.invoke(cli.app, ["group", "nope"])
    assert result.exit_code == 2
    assert "not found" in result.output.lower()


def test_coerce_id():
    assert cli.coerce_id("42") == 42
    assert cli.coerce_id(" 7 ") == 7
    assert cli.coerce_id("grp/sub") == "grp/sub"
    assert cli.coerce_id("4a") == "4a"


def test_protocol_default_and_override(patched, monkeypatch) -> None:
    monkeypatch.delenv("GITLAB_GIT_PROTOCOL", raising=False)
    result = runner.invoke(cli.app, ["user"])
    assert result.exit_code == 0, result.output
    assert FakeCloner.instances[-1].protocol == "ssh"

    result = runner.invoke(cli.app, ["user", "--protocol", "https"])
    assert result.exit_code == 0, result.output
    assert FakeCloner.instances[-1].protocol == "https"

    monkeypatch.setenv("GITLAB_GIT_PROTOCOL", "https")
    result = runner.invoke(cli.app, ["user"])
    assert result.exit_code == 0, result.output
    assert FakeCloner.instances[-1].protocol == "https"

    result = runner.invoke(cli.app, ["user", "--protocol", "ftp"])
    assert result.exit_code != 0


def test_checkout_flag(patched, monkeypatch) -> None:
    monkeypatch.delenv("GITLAB_CHECKOUT", raising=False)
    assert runner.invoke(cli.app, ["user"]).exit_code == 0
    assert FakeCloner.instances[-1].checkout is True
    assert runner.invoke(cli.app, ["user", "--no-checkout"]).exit_code == 0
    assert FakeCloner.instances[-1].checkout is False
    monkeypatch.setenv("GITLAB_CHECKOUT", "false")
    assert runner.invoke(cli.app, ["user"]).exit_code == 0
    assert FakeCloner.instances[-1].checkout is False


def test_export_flags(patched, monkeypatch) -> None:
    monkeypatch.delenv("GITLAB_EXPORT_ISSUES", raising=False)
    monkeypatch.delenv("GITLAB_EXPORT_MRS", raising=False)
    assert runner.invoke(cli.app, ["user"]).exit_code == 0
    exporter = FakeCloner.instances[-1].exporter
    assert exporter is not None and exporter.issues and exporter.merge_requests
    assert runner.invoke(cli.app, ["user", "--no-issues"]).exit_code == 0
    exporter = FakeCloner.instances[-1].exporter
    assert exporter is not None and not exporter.issues and exporter.merge_requests
    assert runner.invoke(cli.app, ["user", "--no-issues", "--no-mrs"]).exit_code == 0
    assert FakeCloner.instances[-1].exporter is None


def test_missing_url_is_friendly_error(patched, monkeypatch) -> None:
    monkeypatch.delenv("GITLAB_URL", raising=False)
    result = runner.invoke(cli.app, ["user"])
    assert result.exit_code == 2
    assert "GITLAB_URL" in result.output
