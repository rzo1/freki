"""Mirror cloning: 'git clone --mirror' / 'git remote update --prune', optional
'git lfs fetch --all' and wiki mirroring, using an env-based credential helper so
the token never lands in .git/config or logs."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rich import get_console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from freki.models import ProjectInfo, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from freki.exporter import IssueExporter

log = logging.getLogger(__name__)

Status = Literal["cloned", "updated", "unchanged", "skipped", "failed"]
_REF_FORMAT = "%(objectname) %(refname)"

# Credential helper reading the token from the environment at runtime. The token
# itself is never part of the command line or of any git config file.
_CREDENTIAL_HELPER = '!f() { echo username=oauth2; echo password="$GITLAB_TOKEN"; }; f'

# stderr fragments that indicate a wiki repository that is empty or does not exist.
_EMPTY_WIKI_MARKERS = (
    "not found",
    "does not appear to be a git repository",
    "could not be found",
    "repository does not exist",
    "could not read from remote repository",
    "empty repository",
)


class MirrorError(RuntimeError):
    """The mirror directory is in a state git cannot clone into."""


class GitError(RuntimeError):
    """A git subprocess exited with a non-zero status."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"git {' '.join(args)} failed with exit code {returncode}: {stderr.strip()}"
        )


@dataclass
class CloneResult:
    """Outcome of processing a single project."""

    project: str
    status: Status
    message: str = ""
    wiki_status: str | None = None
    lfs_status: str | None = None
    checkout_status: str | None = None
    issues_status: str | None = None
    mrs_status: str | None = None
    changes: str = ""
    """Human readable ref delta for an update, e.g. ``+2 ~1 -0`` (added/changed/removed)."""


class GitCloner:
    """Mirror-clone (or update) GitLab projects into ``output_dir``."""

    def __init__(
        self,
        output_dir: Path,
        token: str,
        clone_wiki: bool = True,
        clone_lfs: bool = True,
        dry_run: bool = False,
        protocol: Protocol = "ssh",
        checkout: bool = True,
        exporter: IssueExporter | None = None,
    ) -> None:
        # Absolute: git subprocesses run with cwd inside the mirrors, so relative
        # paths passed to them would otherwise be resolved from there.
        self.output_dir = Path(output_dir).resolve()
        self._token = token
        self.protocol: Protocol = protocol
        self.checkout = checkout
        self.exporter = exporter
        self.clone_wiki = clone_wiki
        self.clone_lfs = clone_lfs
        self.dry_run = dry_run
        self._lfs_available: bool | None = None
        self._lfs_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _git_env(self) -> dict[str, str]:
        """Environment for git subprocesses with an env-based credential helper.

        The helper chain is *reset* first (an empty ``credential.helper`` value
        clears all previously configured helpers) so that system/global helpers
        such as ``osxkeychain`` or ``store`` neither supply a stale credential nor
        get the token handed to them via ``approve`` and persist it. Any
        ``GIT_CONFIG_*`` entries already present in the environment are kept and
        our entries are appended after them.
        """
        env = os.environ.copy()
        env["GITLAB_TOKEN"] = self._token
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
        # SSH: rely on the user's ssh agent / keys but never block on a prompt.
        # A user-provided GIT_SSH_COMMAND is respected as-is.
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")

        try:
            count = max(0, int(env.get("GIT_CONFIG_COUNT", "0") or 0))
        except ValueError:
            count = 0
        for key, value in (
            ("credential.helper", ""),
            ("credential.helper", _CREDENTIAL_HELPER),
        ):
            env[f"GIT_CONFIG_KEY_{count}"] = key
            env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
        return env

    def _sanitize(self, text: str) -> str:
        """Replace any occurrence of the token in ``text`` with ``***``."""
        if self._token:
            text = text.replace(self._token, "***")
        return text

    def _run(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Run ``git <args>`` capturing output; raise :class:`GitError` on failure."""
        cmd = ["git", *args]
        log.debug("running: %s (cwd=%s)", " ".join(self._sanitize(a) for a in cmd), cwd)
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            env=self._git_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        proc.stdout = self._sanitize(proc.stdout or "")
        proc.stderr = self._sanitize(proc.stderr or "")
        if proc.returncode != 0:
            raise GitError([self._sanitize(a) for a in args], proc.returncode, proc.stderr)
        return proc

    def _lfs_installed(self) -> bool:
        """Check once (cached, thread-safe) whether the git-lfs extension is available."""
        with self._lfs_lock:
            if self._lfs_available is None:
                try:
                    self._run(["lfs", "version"])
                    self._lfs_available = True
                except (GitError, OSError) as exc:
                    log.warning("git-lfs not available, skipping LFS fetch: %s", exc)
                    self._lfs_available = False
            return self._lfs_available

    def _mirror(self, url: str, target: Path) -> Status:
        """Clone ``url`` as a mirror into ``target`` or update an existing mirror.

        A leftover from an interrupted clone (directory without ``HEAD``) is
        removed when it is clearly not a git directory (no ``objects/``); an
        empty directory is simply reused. Anything else raises
        :class:`MirrorError` with a hint instead of failing on every re-run with
        git's "already exists and is not an empty directory".
        """
        if (target / "HEAD").exists():
            return self._update(target)[0]
        if target.exists():
            self._clear_stale(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._run(["clone", "--mirror", url, str(target)])
        return "cloned"

    def _refs(self, target: Path) -> dict[str, str]:
        """Map ``refname -> objectname`` of every ref in the mirror."""
        out = self._run(["for-each-ref", f"--format={_REF_FORMAT}"], cwd=target).stdout
        refs: dict[str, str] = {}
        for line in out.splitlines():
            sha, _, name = line.strip().partition(" ")
            if name:
                refs[name] = sha
        return refs

    def _update(self, target: Path) -> tuple[Status, str]:
        """Fetch into an existing mirror; report whether any ref actually changed.

        Returns ``("updated", "+a ~c -r")`` when refs were added/changed/removed
        (branches, tags, ...), ``("unchanged", "")`` when the mirror was already
        up to date.
        """
        before = self._refs(target)
        self._run(["remote", "update", "--prune"], cwd=target)
        after = self._refs(target)
        if before == after:
            return "unchanged", ""
        added = sum(1 for name in after if name not in before)
        removed = sum(1 for name in before if name not in after)
        changed = sum(1 for name, sha in after.items() if name in before and before[name] != sha)
        return "updated", f"+{added} ~{changed} -{removed}"

    @staticmethod
    def _clear_stale(target: Path) -> None:
        """Make room for a fresh clone at ``target`` (no ``HEAD`` present) or raise."""
        if not target.is_dir():
            raise MirrorError(f"{target} exists but is not a directory; remove it to re-clone")
        if not any(target.iterdir()):
            return
        if not (target / "objects").exists():
            log.warning("removing stale/partial mirror directory %s", target)
            shutil.rmtree(target)
            return
        raise MirrorError(
            f"stale/partial mirror at {target} (no HEAD but objects/ present); "
            "delete it to re-clone"
        )

    # -------------------------------------------------------------------- steps

    def _fetch_lfs(self, target: Path) -> str:
        if not self._lfs_installed():
            return "skipped: git-lfs not installed"
        try:
            self._run(["lfs", "fetch", "--all"], cwd=target)
        except GitError as exc:
            log.warning("LFS fetch failed for %s: %s", target, exc.stderr.strip())
            return f"failed: {exc.stderr.strip()}"
        return "fetched"

    def _mirror_wiki(self, project: ProjectInfo) -> str:
        target = self.output_dir / f"{project.path_with_namespace}.wiki.git"
        existed = (target / "HEAD").exists()
        try:
            return self._mirror(project.wiki_url_for(self.protocol), target)
        except MirrorError as exc:
            log.warning("wiki mirror failed for %s: %s", project.path_with_namespace, exc)
            return f"failed: {exc}"
        except GitError as exc:
            stderr = exc.stderr.lower()
            if not existed and target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if any(marker in stderr for marker in _EMPTY_WIKI_MARKERS):
                log.info("wiki of %s is empty or does not exist", project.path_with_namespace)
                return "empty"
            log.warning("wiki mirror failed for %s: %s", project.path_with_namespace, exc)
            return f"failed: {exc.stderr.strip()}"

    def _default_ref(self, mirror: Path, preferred: str | None) -> str | None:
        """Name of the branch to check out.

        Tries, in order: ``preferred`` (the project's default branch), the
        mirror's HEAD, ``main``/``master``, and finally the first branch that
        exists — a mirror can have a dangling HEAD (e.g. the remote's default
        branch was deleted) and should still get a checkout.
        """
        candidates = [f"refs/heads/{preferred}"] if preferred else []
        try:
            head = self._run(["symbolic-ref", "-q", "HEAD"], cwd=mirror).stdout.strip()
            if head:
                candidates.append(head)
        except GitError:
            pass
        candidates += ["refs/heads/main", "refs/heads/master"]
        for ref in candidates:
            try:
                self._run(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd=mirror)
                return ref
            except GitError:
                continue
        branches = self._run(
            ["for-each-ref", "--format=%(refname)", "--count=1", "refs/heads/"], cwd=mirror
        ).stdout.strip()
        return branches or None

    def _checkout(self, mirror: Path, target: Path, branch: str | None) -> str:
        """Maintain a detached worktree of ``branch`` (default branch) at ``target``.

        The worktree shares the mirror's object and LFS store, so no network
        access is needed. The checkout is an *export*: on every run it is
        forced to the branch tip, local modifications in it are discarded.
        Returns ``checked out`` / ``updated`` / ``unchanged`` / ``empty`` /
        ``failed: ...``.
        """
        try:
            ref = self._default_ref(mirror, branch)
            if ref is None:
                return "empty"
            sha = self._run(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=mirror).stdout
            sha = sha.strip()

            if (target / ".git").exists():
                try:
                    current = self._run(["rev-parse", "--verify", "HEAD"], cwd=target)
                    current = current.stdout.strip()
                except GitError:
                    # Worktree link is broken (e.g. mirror was re-cloned) -> recreate.
                    log.warning("recreating broken checkout at %s", target)
                    shutil.rmtree(target)
                    current = None
                if current is not None:
                    if current == sha:
                        status = "unchanged"
                    else:
                        self._run(["checkout", "--detach", "--force", sha], cwd=target)
                        status = "updated"
                    self._lfs_checkout(target)
                    return status

            if target.exists():
                if any(target.iterdir()):
                    return f"failed: {target} exists and is not a git checkout; remove it"
                target.rmdir()
            target.parent.mkdir(parents=True, exist_ok=True)
            self._run(["worktree", "prune"], cwd=mirror)
            self._remove_misplaced_worktrees(mirror, target)
            self._run(["worktree", "add", "--detach", str(target), sha], cwd=mirror)
            self._lfs_checkout(target)
            return "checked out"
        except (GitError, OSError) as exc:
            log.warning("checkout failed for %s: %s", target, self._sanitize(str(exc)))
            return f"failed: {self._sanitize(str(exc))}"

    def _remove_misplaced_worktrees(self, mirror: Path, target: Path) -> None:
        """Remove worktrees registered in ``mirror`` that live *inside* the mirror.

        Earlier versions resolved a relative output directory against the
        mirror and created the checkout nested inside ``<project>.git``. Such
        leftovers are removed (registration and files) before the proper
        checkout at ``target`` is created.
        """
        try:
            listing = self._run(["worktree", "list", "--porcelain"], cwd=mirror).stdout
        except GitError:
            return
        mirror_abs = mirror.resolve()
        for line in listing.splitlines():
            if not line.startswith("worktree "):
                continue
            path = Path(line[len("worktree ") :]).resolve()
            if path == mirror_abs or path == target.resolve():
                continue
            if mirror_abs in path.parents:
                log.warning("removing misplaced checkout %s", path)
                try:
                    self._run(["worktree", "remove", "--force", str(path)], cwd=mirror)
                except GitError:
                    shutil.rmtree(path, ignore_errors=True)
                    self._run(["worktree", "prune"], cwd=mirror)
                # drop now-empty intermediate directories inside the mirror
                parent = path.parent
                while parent != mirror_abs and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent

    def _lfs_checkout(self, target: Path) -> None:
        """Replace LFS pointers with content from the mirror's local LFS store."""
        if not self.clone_lfs or not self._lfs_installed():
            return
        try:
            self._run(["lfs", "checkout"], cwd=target)
        except GitError as exc:
            log.warning("LFS checkout in %s incomplete: %s", target, exc.stderr.strip())

    # ---------------------------------------------------------------- public API

    def clone_or_update(self, project: ProjectInfo) -> CloneResult:
        """Mirror-clone or update one project including LFS objects and wiki."""
        name = project.path_with_namespace
        target = self.output_dir / f"{name}.git"

        if self.dry_run:
            action = "update" if (target / "HEAD").exists() else "clone"
            extras = []
            if self.clone_lfs:
                extras.append("lfs")
            if self.clone_wiki and project.wiki_enabled:
                extras.append("wiki")
            if self.checkout:
                extras.append("checkout")
            if self.exporter is not None:
                if self.exporter.issues:
                    extras.append("issues")
                if self.exporter.merge_requests:
                    extras.append("merge requests")
            suffix = f" (+{', '.join(extras)})" if extras else ""
            log.info("[dry-run] would %s %s -> %s%s", action, name, target, suffix)
            return CloneResult(project=name, status="skipped", message="dry-run")

        changes = ""
        try:
            if (target / "HEAD").exists():
                status, changes = self._update(target)
            else:
                url = project.repo_url(self.protocol)
                if self.protocol == "ssh" and not project.ssh_url_to_repo:
                    log.warning("%s: no SSH URL known, falling back to %s", name, url)
                status = self._mirror(url, target)
        except Exception as exc:  # report every failure per project, never abort the run
            message = self._sanitize(str(exc))
            log.error("failed to mirror %s: %s", name, message)
            return CloneResult(project=name, status="failed", message=message)

        log.info("%s: %s%s", name, status, f" ({changes})" if changes else "")
        result = CloneResult(project=name, status=status, changes=changes)

        if self.clone_lfs:
            result.lfs_status = self._fetch_lfs(target)

        if self.clone_wiki and project.wiki_enabled:
            result.wiki_status = self._mirror_wiki(project)

        if self.checkout:
            result.checkout_status = self._checkout(
                target, self.output_dir / name, project.default_branch
            )
            wiki_mirror = self.output_dir / f"{name}.wiki.git"
            if (
                result.wiki_status in ("cloned", "updated", "unchanged")
                and (wiki_mirror / "HEAD").exists()
            ):
                wiki_co = self._checkout(wiki_mirror, self.output_dir / f"{name}.wiki", None)
                if wiki_co.startswith("failed"):
                    result.wiki_status = f"{result.wiki_status}; checkout {wiki_co}"

        if self.exporter is not None:
            result.issues_status, result.mrs_status = self.exporter.export(project)

        notes = []
        if result.lfs_status and result.lfs_status.startswith("failed"):
            notes.append(f"lfs {result.lfs_status}")
        if result.wiki_status and result.wiki_status.startswith("failed"):
            notes.append(f"wiki {result.wiki_status}")
        if result.checkout_status and result.checkout_status.startswith("failed"):
            notes.append(f"checkout {result.checkout_status}")
        if result.issues_status and result.issues_status.startswith("failed"):
            notes.append(f"issues {result.issues_status}")
        if result.mrs_status and result.mrs_status.startswith("failed"):
            notes.append(f"merge requests {result.mrs_status}")
        result.message = "; ".join(notes)
        return result

    def clone_all(self, projects: Iterable[ProjectInfo], concurrency: int = 4) -> list[CloneResult]:
        """Process all projects concurrently; results are returned in input order."""
        projects = list(projects)
        if not projects:
            return []
        concurrency = max(1, concurrency)

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TextColumn("[dim]{task.fields[current]}"),
            console=get_console(),  # same console as the log handler -> no garbling
            transient=False,
        ) as progress:
            task_id = progress.add_task("Mirroring", total=len(projects), current="")

            def work(project: ProjectInfo) -> CloneResult:
                result = self.clone_or_update(project)
                progress.update(task_id, advance=1, current=project.path_with_namespace)
                return result

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                return list(pool.map(work, projects))
