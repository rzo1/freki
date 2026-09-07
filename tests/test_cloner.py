"""Tests for freki.cloner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from freki.cloner import CloneResult, GitCloner, GitError, MirrorError
from freki.models import ProjectInfo

TOKEN = "glpat-SECRET-TOKEN-123"


def make_project(wiki: bool = False) -> ProjectInfo:
    return ProjectInfo(
        id=1,
        path_with_namespace="group/sub/proj",
        http_url_to_repo="https://git.example.org/group/sub/proj.git",
        ssh_url_to_repo="git@git.example.org:group/sub/proj.git",
        wiki_enabled=wiki,
    )


def ok(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git", *args], 0, "", "")


class Recorder:
    """Stand-in for GitCloner._run that records calls and can fail on demand."""

    def __init__(self, fail: dict[str, str] | None = None, refs: list[str] | None = None) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.fail = fail or {}
        # successive outputs for 'for-each-ref' (before/after snapshots)
        self.refs = list(refs or [])

    def __call__(self, args: list[str], cwd: Path | None = None):
        self.calls.append((list(args), cwd))
        for key, stderr in self.fail.items():
            if key in " ".join(args):
                raise GitError(args, 128, stderr)
        if args[0] == "for-each-ref" and self.refs:
            return subprocess.CompletedProcess(["git", *args], 0, self.refs.pop(0), "")
        return ok(args)


# ----------------------------------------------------------------- unit tests


def test_fresh_clone_sequence(tmp_path: Path) -> None:
    cloner = GitCloner(
        tmp_path, TOKEN, checkout=False, clone_wiki=True, clone_lfs=True, protocol="https"
    )
    rec = Recorder()
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project(wiki=True))

    target = tmp_path / "group/sub/proj.git"
    assert result.status == "cloned"
    assert result.lfs_status == "fetched"
    assert result.wiki_status == "cloned"
    assert result.message == ""
    assert rec.calls == [
        (["clone", "--mirror", "https://git.example.org/group/sub/proj.git", str(target)], None),
        (["lfs", "version"], None),
        (["lfs", "fetch", "--all"], target),
        (
            [
                "clone",
                "--mirror",
                "https://git.example.org/group/sub/proj.wiki.git",
                str(tmp_path / "group/sub/proj.wiki.git"),
            ],
            None,
        ),
    ]
    assert target.parent.is_dir()


def test_fresh_clone_uses_ssh_by_default(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=True, clone_lfs=False)
    rec = Recorder()
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project(wiki=True))

    assert result.status == "cloned"
    assert result.wiki_status == "cloned"
    urls = [c[0][2] for c in rec.calls if c[0][0] == "clone"]
    assert urls == [
        "git@git.example.org:group/sub/proj.git",
        "git@git.example.org:group/sub/proj.wiki.git",
    ]


def test_ssh_falls_back_to_https_without_ssh_url(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=False, clone_lfs=False)
    project = ProjectInfo(id=1, path_with_namespace="g/p", http_url_to_repo="https://x/g/p.git")
    rec = Recorder()
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(project)
    assert result.status == "cloned"
    assert rec.calls[0][0][2] == "https://x/g/p.git"


def test_git_env_sets_batchmode_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    env = GitCloner(tmp_path, TOKEN, checkout=False)._git_env()
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /my/key")
    assert (
        GitCloner(tmp_path, TOKEN, checkout=False)._git_env()["GIT_SSH_COMMAND"] == "ssh -i /my/key"
    )


def test_update_sequence(tmp_path: Path) -> None:
    target = tmp_path / "group/sub/proj.git"
    target.mkdir(parents=True)
    (target / "HEAD").write_text("ref: refs/heads/main\n")
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=False, clone_lfs=False)
    rec = Recorder()
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project())

    assert result.status == "unchanged"
    assert result.changes == ""
    assert result.lfs_status is None
    assert result.wiki_status is None
    assert rec.calls == [
        (["for-each-ref", "--format=%(objectname) %(refname)"], target),
        (["remote", "update", "--prune"], target),
        (["for-each-ref", "--format=%(objectname) %(refname)"], target),
    ]


def test_update_reports_ref_delta(tmp_path: Path) -> None:
    target = tmp_path / "group/sub/proj.git"
    target.mkdir(parents=True)
    (target / "HEAD").write_text("ref: refs/heads/main\n")
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=False, clone_lfs=False)
    before = "aaa refs/heads/main\nbbb refs/heads/old\nccc refs/tags/v1\n"
    after = "aaa2 refs/heads/main\nccc refs/tags/v1\nddd refs/tags/v2\neee refs/heads/new\n"
    rec = Recorder(refs=[before, after])
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project())

    assert result.status == "updated"
    assert result.changes == "+2 ~1 -1"


def test_wiki_empty_is_tolerated(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_lfs=False)
    rec = Recorder(fail={"proj.wiki.git": "fatal: repository 'x.wiki.git/' not found"})
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project(wiki=True))

    assert result.status == "cloned"
    assert result.wiki_status == "empty"
    assert result.message == ""
    assert not (tmp_path / "group/sub/proj.wiki.git").exists()


def test_wiki_other_failure_keeps_main_result(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_lfs=False)
    rec = Recorder(fail={"proj.wiki.git": "fatal: unable to access: SSL error"})
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project(wiki=True))

    assert result.status == "cloned"
    assert result.wiki_status is not None and result.wiki_status.startswith("failed:")
    assert "wiki failed" in result.message


def test_lfs_failure_keeps_main_result(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=False, clone_lfs=True)
    rec = Recorder(fail={"lfs fetch": "boom"})
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project())

    assert result.status == "cloned"
    assert result.lfs_status == "failed: boom"
    assert "lfs failed" in result.message


def test_lfs_not_installed_checked_once(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=False, clone_lfs=True)
    rec = Recorder(fail={"lfs version": "git: 'lfs' is not a git command"})
    with patch.object(GitCloner, "_run", side_effect=rec):
        r1 = cloner.clone_or_update(make_project())
        r2 = cloner.clone_or_update(make_project())

    assert r1.lfs_status == r2.lfs_status == "skipped: git-lfs not installed"
    assert sum(1 for args, _ in rec.calls if args == ["lfs", "version"]) == 1
    assert not any(args[:2] == ["lfs", "fetch"] for args, _ in rec.calls)


def test_main_clone_failure_is_sanitized(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=True, clone_lfs=True)
    rec = Recorder(fail={"proj.git": f"fatal: auth failed for oauth2:{TOKEN}"})
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project(wiki=True))

    assert result.status == "failed"
    assert TOKEN not in result.message
    assert "***" in result.message
    assert result.wiki_status is None and result.lfs_status is None
    assert len(rec.calls) == 1


def test_dry_run(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, dry_run=True)
    rec = Recorder()
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project(wiki=True))

    assert result == CloneResult(project="group/sub/proj", status="skipped", message="dry-run")
    assert rec.calls == []
    assert not (tmp_path / "group").exists()


def test_token_never_in_argv_but_in_env(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=True, clone_lfs=True)
    seen: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs["env"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("freki.cloner.subprocess.run", side_effect=fake_run):
        result = cloner.clone_or_update(make_project(wiki=True))

    assert result.status == "cloned"
    assert seen
    for cmd, env in seen:
        assert cmd[0] == "git"
        assert all(TOKEN not in part for part in cmd)
        assert env["GITLAB_TOKEN"] == TOKEN
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        # helper chain is reset (empty value) before our env-based helper is added
        assert env["GIT_CONFIG_COUNT"] == "2"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""
        assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
        assert TOKEN not in env["GIT_CONFIG_VALUE_1"]
        assert '"$GITLAB_TOKEN"' in env["GIT_CONFIG_VALUE_1"]


def test_git_env_preserves_existing_git_config_entries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    env = GitCloner(tmp_path, TOKEN, checkout=False)._git_env()
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("core.autocrlf", "false")
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("credential.helper", "")
    assert env["GIT_CONFIG_KEY_2"] == "credential.helper"
    assert "$GITLAB_TOKEN" in env["GIT_CONFIG_VALUE_2"]

    monkeypatch.setenv("GIT_CONFIG_COUNT", "garbage")
    assert GitCloner(tmp_path, TOKEN, checkout=False)._git_env()["GIT_CONFIG_COUNT"] == "2"


def test_stale_empty_dir_is_reused(tmp_path: Path) -> None:
    target = tmp_path / "group/sub/proj.git"
    target.mkdir(parents=True)
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=False, clone_lfs=False)
    rec = Recorder()
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project())
    assert result.status == "cloned"
    assert rec.calls[0][0][:2] == ["clone", "--mirror"]


def test_stale_non_git_dir_is_removed(tmp_path: Path) -> None:
    target = tmp_path / "group/sub/proj.git"
    target.mkdir(parents=True)
    (target / "junk.txt").write_text("partial\n")
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=False, clone_lfs=False)
    rec = Recorder()
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project())
    assert result.status == "cloned"
    assert not (target / "junk.txt").exists()
    assert rec.calls[0][0][:2] == ["clone", "--mirror"]


def test_stale_partial_git_dir_fails_with_hint(tmp_path: Path) -> None:
    target = tmp_path / "group/sub/proj.git"
    (target / "objects").mkdir(parents=True)  # objects/ but no HEAD: interrupted clone
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=True, clone_lfs=False)
    rec = Recorder()
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project(wiki=True))
    assert result.status == "failed"
    assert "delete it to re-clone" in result.message
    assert rec.calls == []
    assert (target / "objects").exists()  # never deletes something that may hold data

    with pytest.raises(MirrorError):
        cloner._mirror("https://x/y.git", target)


def test_stale_wiki_dir_is_reported_as_failed(tmp_path: Path) -> None:
    wiki = tmp_path / "group/sub/proj.wiki.git"
    (wiki / "objects").mkdir(parents=True)
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=True, clone_lfs=False)
    rec = Recorder()
    with patch.object(GitCloner, "_run", side_effect=rec):
        result = cloner.clone_or_update(make_project(wiki=True))
    assert result.status == "cloned"
    assert result.wiki_status is not None and result.wiki_status.startswith("failed:")
    assert "delete it to re-clone" in result.wiki_status


def test_run_raises_git_error_with_sanitized_stderr(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False)
    proc = subprocess.CompletedProcess(["git", "x"], 1, "", f"denied for {TOKEN}")
    with patch("freki.cloner.subprocess.run", return_value=proc):
        with pytest.raises(GitError) as excinfo:
            cloner._run(["x"])
    assert TOKEN not in str(excinfo.value)
    assert "***" in excinfo.value.stderr


def test_clone_all_preserves_order(tmp_path: Path) -> None:
    cloner = GitCloner(tmp_path, TOKEN, checkout=False, clone_wiki=False, clone_lfs=False)
    projects = [
        ProjectInfo(id=i, path_with_namespace=f"g/p{i}", http_url_to_repo=f"https://x/g/p{i}.git")
        for i in range(6)
    ]
    rec = Recorder(fail={"p3.git": "fatal: nope"})
    with patch.object(GitCloner, "_run", side_effect=rec):
        results = cloner.clone_all(projects, concurrency=3)

    assert [r.project for r in results] == [p.path_with_namespace for p in projects]
    assert [r.status for r in results] == ["cloned"] * 3 + ["failed"] + ["cloned"] * 2
    assert cloner.clone_all([], concurrency=2) == []


# ----------------------------------------------------------- integration test


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_real_credential_helper_overrides_store_and_never_persists(
    tmp_path: Path, monkeypatch
) -> None:
    """With a user-level ``credential.helper = store`` holding a stale token, git must
    still use the PAT from the environment, and ``approve`` must not write it anywhere."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    creds = home / ".git-credentials"
    creds.write_text("https://oauth2:STALE-OLD-TOKEN@git.example.org\n")
    gitconfig.write_text(f"[credential]\n\thelper = store --file {creds}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)

    cloner = GitCloner(tmp_path, TOKEN, checkout=False)
    env = cloner._git_env()
    description = "protocol=https\nhost=git.example.org\n\n"

    filled = subprocess.run(
        ["git", "credential", "fill"],
        input=description,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"password={TOKEN}" in filled
    assert "STALE-OLD-TOKEN" not in filled

    subprocess.run(
        ["git", "credential", "approve"],
        input=f"protocol=https\nhost=git.example.org\nusername=oauth2\npassword={TOKEN}\n\n",
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert TOKEN not in creds.read_text()
    assert TOKEN not in gitconfig.read_text()


def test_real_mirror_clone_and_update(tmp_path: Path) -> None:
    # Build a "remote": a work repo pushed into a bare repo.
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    _git("config", "user.email", "t@example.org", cwd=work)
    _git("config", "user.name", "T", cwd=work)
    (work / "a.txt").write_text("one\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "one", cwd=work)
    _git("tag", "v1", cwd=work)
    _git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "-q", "origin", "main", "v1", cwd=work)

    out = tmp_path / "out"
    project = ProjectInfo(
        id=42,
        path_with_namespace="ns/demo",
        http_url_to_repo=remote.as_uri(),
        wiki_enabled=False,
    )
    cloner = GitCloner(out, TOKEN, clone_wiki=False, clone_lfs=False, checkout=True)

    first = cloner.clone_or_update(project)
    assert first.status == "cloned", first
    assert first.checkout_status == "checked out", first
    mirror = out / "ns/demo.git"
    checkout = out / "ns/demo"
    assert (checkout / "a.txt").read_text() == "one\n"
    assert (checkout / ".git").is_file()  # worktree link into the mirror
    assert (mirror / "HEAD").exists()
    assert _git("config", "core.bare", cwd=mirror) == "true"
    assert _git("config", "remote.origin.url", cwd=mirror) == remote.as_uri()
    assert TOKEN not in (mirror / "config").read_text()
    assert "refs/tags/v1" in _git("show-ref", cwd=mirror)

    # Add a branch + commit upstream, then re-run -> update path.
    _git("checkout", "-q", "-b", "feature", cwd=work)
    (work / "b.txt").write_text("two\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "two", cwd=work)
    _git("push", "-q", "origin", "feature", cwd=work)

    second = cloner.clone_or_update(project)
    assert second.status == "updated", second
    assert second.changes == "+1 ~0 -0"
    assert second.checkout_status == "unchanged"  # main did not move
    refs = _git("show-ref", cwd=mirror)
    assert "refs/heads/feature" in refs
    assert "refs/heads/main" in refs

    # Nothing changed upstream -> re-run reports 'unchanged'.
    same = cloner.clone_or_update(project)
    assert same.status == "unchanged", same
    assert same.changes == ""
    assert same.checkout_status == "unchanged"

    # Advance main upstream -> checkout follows; local edits are discarded.
    _git("checkout", "-q", "main", cwd=work)
    (work / "a.txt").write_text("one-v2\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "v2", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    (checkout / "a.txt").write_text("local edit\n")
    moved = cloner.clone_or_update(project)
    assert moved.status == "updated", moved
    assert moved.checkout_status == "updated"
    assert (checkout / "a.txt").read_text() == "one-v2\n"

    # Mirror re-created from scratch -> broken worktree link is repaired.
    shutil.rmtree(mirror)
    again = cloner.clone_or_update(project)
    assert again.status == "cloned", again
    assert again.checkout_status == "checked out"
    assert (checkout / "a.txt").read_text() == "one-v2\n"

    # Delete the branch upstream -> prune removes it from the mirror.
    _git("push", "-q", "origin", "--delete", "feature", cwd=work)
    third = cloner.clone_or_update(project)
    assert third.status == "updated"
    assert third.changes == "+0 ~0 -1"
    assert "refs/heads/feature" not in _git("show-ref", cwd=mirror)


def test_checkout_step_sequence(tmp_path: Path) -> None:
    target = tmp_path / "group/sub/proj.git"
    target.mkdir(parents=True)
    (target / "HEAD").write_text("ref: refs/heads/main\n")
    cloner = GitCloner(tmp_path, TOKEN, clone_wiki=False, clone_lfs=False, checkout=True)
    rec = Recorder(refs=["aaa refs/heads/main\n", "aaa refs/heads/main\n"])

    def run(args, cwd=None):
        if args[0] == "rev-parse" and "refs/heads/main^{commit}" in args:
            rec.calls.append((list(args), cwd))
            return subprocess.CompletedProcess(["git", *args], 0, "aaa\n", "")
        return rec(args, cwd)

    with patch.object(GitCloner, "_run", side_effect=run):
        result = cloner.clone_or_update(
            ProjectInfo(1, "group/sub/proj", "https://x/p.git", default_branch="main")
        )
    assert result.status == "unchanged"
    assert result.checkout_status == "checked out"
    wt = [c for c in rec.calls if c[0][:2] == ["worktree", "add"]]
    assert wt == [
        (["worktree", "add", "--detach", str(tmp_path / "group/sub/proj"), "aaa"], target)
    ]


def test_relative_output_dir_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: relative --output must not nest the checkout inside the mirror."""
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "a.txt").write_text("one\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "one", cwd=work)
    _git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    _git("push", "-q", str(remote), "main", cwd=work)
    project = ProjectInfo(1, "ns/demo", remote.as_uri(), default_branch="main")

    monkeypatch.chdir(tmp_path)
    cloner = GitCloner(Path("gitlab-export"), TOKEN, clone_wiki=False, clone_lfs=False)
    result = cloner.clone_or_update(project)
    assert result.checkout_status == "checked out", result
    assert (tmp_path / "gitlab-export/ns/demo/a.txt").read_text() == "one\n"
    assert not (tmp_path / "gitlab-export/ns/demo.git/gitlab-export").exists()

    # Simulate the old bug: a worktree nested inside the mirror -> cleaned up on next run.
    mirror = tmp_path / "gitlab-export/ns/demo.git"
    shutil.rmtree(tmp_path / "gitlab-export/ns/demo")
    _git("worktree", "prune", cwd=mirror)
    nested = mirror / "gitlab-export/ns/demo"
    _git("worktree", "add", "--detach", str(nested), "main", cwd=mirror)
    assert nested.is_dir()
    again = cloner.clone_or_update(project)
    assert again.checkout_status == "checked out", again
    assert not (mirror / "gitlab-export").exists()
    assert (tmp_path / "gitlab-export/ns/demo/a.txt").read_text() == "one\n"
    assert "gitlab-export/ns/demo.git/gitlab-export" not in _git("worktree", "list", cwd=mirror)


def test_exporter_hook_and_partial_note(tmp_path: Path) -> None:
    class FakeExporter:
        issues = True
        merge_requests = True

        def export(self, project):
            return "3 (1 updated)", "failed: boom"

    cloner = GitCloner(
        tmp_path, TOKEN, clone_wiki=False, clone_lfs=False, checkout=False, exporter=FakeExporter()
    )
    with patch.object(GitCloner, "_run", side_effect=Recorder()):
        result = cloner.clone_or_update(make_project())
    assert result.status == "cloned"
    assert result.issues_status == "3 (1 updated)"
    assert result.mrs_status == "failed: boom"
    assert result.message == "merge requests failed: boom"

    cloner.dry_run = True
    with patch.object(GitCloner, "_run", side_effect=Recorder()):
        assert cloner.clone_or_update(make_project()).status == "skipped"


def test_checkout_falls_back_when_head_dangling(tmp_path: Path) -> None:
    """A mirror whose HEAD points to a deleted/never-born branch still gets a checkout."""
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    work.mkdir()
    _git("init", "-q", "-b", "trunk", cwd=work)
    (work / "a.txt").write_text("x\n")
    _git("add", ".", cwd=work)
    _git("commit", "-q", "-m", "one", cwd=work)
    # remote HEAD points at 'gone', which is never pushed
    _git("init", "-q", "--bare", "-b", "gone", str(remote), cwd=tmp_path)
    _git("push", "-q", str(remote), "trunk", cwd=work)

    project = ProjectInfo(1, "ns/dangling", remote.as_uri())
    cloner = GitCloner(tmp_path / "out", TOKEN, clone_wiki=False, clone_lfs=False)
    result = cloner.clone_or_update(project)
    assert result.checkout_status == "checked out", result
    assert (tmp_path / "out/ns/dangling/a.txt").read_text() == "x\n"
