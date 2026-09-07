"""Project discovery: enumerate projects for a group, a single project, or everything
the authenticated user can access (owned/private projects + all member groups,
recursively including subgroups), deduplicated by project id."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import gitlab
from gitlab.exceptions import GitlabError, GitlabGetError, GitlabListError

from freki.models import ProjectInfo

log = logging.getLogger(__name__)

# Access levels as defined by GitLab (``min_access_level`` filter).
GUEST_ACCESS = 10


class ProjectDiscovery:
    """Discover projects through an authenticated python-gitlab client.

    Archived projects are excluded unless ``include_archived`` is true. The
    archived filter is applied both server-side (where the API supports it)
    and client-side, because some endpoints ignore the flag.
    """

    def __init__(self, gl: gitlab.Gitlab, include_archived: bool = False) -> None:
        self.gl = gl
        self.include_archived = include_archived

    # ------------------------------------------------------------------ helpers

    def _archived_filter(self) -> dict[str, Any]:
        """Server-side ``archived`` filter kwargs.

        ``archived=False`` returns only active projects; omitting the parameter
        returns both archived and active ones.
        """
        return {} if self.include_archived else {"archived": False}

    def _accept(self, info: ProjectInfo) -> bool:
        """Client-side archived guard."""
        if info.archived and not self.include_archived:
            log.debug("Skipping archived project %s", info.path_with_namespace)
            return False
        return True

    def _to_info(self, project: Any) -> ProjectInfo:
        """Convert a (possibly lightweight) python-gitlab object to ``ProjectInfo``.

        ``GroupProject`` objects returned by ``group.projects.list()`` may lack
        attributes such as ``wiki_enabled``; in that case the full project is
        fetched. If that fails, the lightweight data is used as-is.
        """
        if not all(
            hasattr(project, attr)
            for attr in ("wiki_enabled", "http_url_to_repo", "ssh_url_to_repo")
        ):
            try:
                project = self.gl.projects.get(project.id)
            except GitlabError as exc:
                log.warning(
                    "Could not fetch full project %s (%s); using lightweight data",
                    getattr(project, "path_with_namespace", project.id),
                    exc,
                )
        return ProjectInfo.from_gl(project)

    def _collect(self, projects: Iterable[Any], into: dict[int, ProjectInfo]) -> int:
        """Convert and add ``projects`` to ``into`` (keyed by id); return #added."""
        added = 0
        for project in projects:
            info = self._to_info(project)
            if not self._accept(info):
                continue
            if info.id not in into:
                into[info.id] = info
                added += 1
        return added

    @staticmethod
    def _sorted(projects: dict[int, ProjectInfo]) -> list[ProjectInfo]:
        return sorted(projects.values(), key=lambda p: p.path_with_namespace.lower())

    # ------------------------------------------------------------------ single project

    def get_project(self, id_or_path: str | int) -> ProjectInfo | None:
        """Return the project ``id_or_path`` or ``None`` if it is archived and
        archived projects are excluded.

        Raises ``gitlab.exceptions.GitlabGetError`` if the project does not exist
        or is not accessible.
        """
        project = self.gl.projects.get(id_or_path)
        info = ProjectInfo.from_gl(project)
        if not self._accept(info):
            log.info(
                "Project %s is archived; skipped (use --include-archived to clone it)",
                info.path_with_namespace,
            )
            return None
        return info

    # ------------------------------------------------------------------ group

    def get_group_projects(
        self, id_or_path: str | int, with_shared: bool = False
    ) -> list[ProjectInfo]:
        """Return all projects of group ``id_or_path``, recursively including all
        subgroups, sorted by ``path_with_namespace``.

        ``with_shared`` controls whether projects merely *shared* with the group
        are included (default: no).

        Raises ``gitlab.exceptions.GitlabGetError`` if the group cannot be fetched.
        """
        group = self.gl.groups.get(id_or_path)
        found: dict[int, ProjectInfo] = {}
        self._collect_group(group, found, with_shared=with_shared)
        result = self._sorted(found)
        log.info(
            "Group %s: %d project(s) discovered (including subgroups)",
            getattr(group, "full_path", id_or_path),
            len(result),
        )
        return result

    def _collect_group(
        self, group: Any, into: dict[int, ProjectInfo], with_shared: bool = False
    ) -> int:
        """Add all projects of ``group`` (incl. subgroups) to ``into``; return #added."""
        name = getattr(group, "full_path", getattr(group, "id", "?"))
        try:
            projects = group.projects.list(
                include_subgroups=True,
                with_shared=with_shared,
                iterator=True,
                **self._archived_filter(),
            )
            added = self._collect(projects, into)
        except (GitlabError, AttributeError) as exc:
            # AttributeError: lightweight group objects (e.g. GroupDescendantGroup)
            # carry no ``projects`` manager; never let one bad group abort the run.
            log.warning("Could not list projects of group %s: %s", name, exc)
            return 0
        log.debug("Group %s: %d new project(s)", name, added)
        return added

    # ------------------------------------------------------------------ user

    def get_user_projects(self, with_shared: bool = False) -> list[ProjectInfo]:
        """Return everything the token's user can access:

        1. projects the user is a direct member of,
        2. projects the user owns (personal/private projects),
        3. all projects of every group the user is a member of, recursively
           including subgroups.

        ``GET /groups?min_access_level=`` already returns the complete accessible
        hierarchy (top-level groups *and* their subgroups), and listing a group
        with ``include_subgroups=True`` already covers its whole subtree. To avoid
        fetching every project once per nesting level, only the *roots* of the
        accessible forest are walked: groups whose parent is not itself listed.

        Deduplicated by project id and sorted by ``path_with_namespace``.
        """
        found: dict[int, ProjectInfo] = {}

        for label, kwargs in (("membership", {"membership": True}), ("owned", {"owned": True})):
            try:
                projects = self.gl.projects.list(iterator=True, **kwargs, **self._archived_filter())
                added = self._collect(projects, found)
                log.info("User projects (%s): %d new project(s)", label, added)
            except (GitlabListError, GitlabGetError) as exc:
                log.warning("Could not list %s projects: %s", label, exc)

        try:
            groups = list(self.gl.groups.list(iterator=True, min_access_level=GUEST_ACCESS))
        except (GitlabListError, GitlabGetError) as exc:
            log.warning("Could not list groups: %s", exc)
            groups = []

        roots = self._root_groups(groups)
        for group in roots:
            added = self._collect_group(group, found, with_shared=with_shared)
            log.info(
                "Group %s: %d new project(s) (incl. subgroups)",
                getattr(group, "full_path", getattr(group, "id", "?")),
                added,
            )

        result = self._sorted(found)
        log.info(
            "User mode: %d unique project(s) across %d group(s) (%d root group(s) walked)",
            len(result),
            len(groups),
            len(roots),
        )
        return result

    @staticmethod
    def _root_groups(groups: list[Any]) -> list[Any]:
        """Deduplicated groups whose parent is not part of ``groups`` itself."""
        listed: dict[int, Any] = {}
        for group in groups:
            listed.setdefault(int(group.id), group)
        roots = []
        for gid, group in listed.items():
            parent_id = getattr(group, "parent_id", None)
            if parent_id is not None and int(parent_id) in listed and int(parent_id) != gid:
                continue
            roots.append(group)
        return roots
