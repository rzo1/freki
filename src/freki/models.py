"""Plain data models shared between discovery, cloning and reporting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Protocol = Literal["ssh", "https"]


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    """Minimal, library-independent description of a GitLab project."""

    id: int
    path_with_namespace: str
    http_url_to_repo: str
    ssh_url_to_repo: str | None = None
    web_url: str | None = None
    archived: bool = False
    wiki_enabled: bool = False
    default_branch: str | None = None

    @classmethod
    def from_gl(cls, project: Any) -> ProjectInfo:
        """Create a ``ProjectInfo`` from a python-gitlab ``Project`` object.

        Works with any object exposing the same attributes (e.g. simple
        namespaces in tests); missing optional attributes fall back to
        sensible defaults.
        """
        return cls(
            id=int(project.id),
            path_with_namespace=str(project.path_with_namespace),
            http_url_to_repo=str(project.http_url_to_repo),
            ssh_url_to_repo=getattr(project, "ssh_url_to_repo", None) or None,
            web_url=getattr(project, "web_url", None) or None,
            archived=bool(getattr(project, "archived", False)),
            wiki_enabled=bool(getattr(project, "wiki_enabled", False)),
            default_branch=getattr(project, "default_branch", None),
        )

    def repo_url(self, protocol: Protocol = "ssh") -> str:
        """Clone URL for ``protocol``; falls back to HTTPS if no SSH URL is known."""
        if protocol == "ssh" and self.ssh_url_to_repo:
            return self.ssh_url_to_repo
        return self.http_url_to_repo

    def wiki_url_for(self, protocol: Protocol = "ssh") -> str:
        """URL of the wiki repository (``<repo minus .git>.wiki.git``) for ``protocol``."""
        base = self.repo_url(protocol)
        if base.endswith(".git"):
            base = base[: -len(".git")]
        return f"{base}.wiki.git"

    @property
    def wiki_url(self) -> str:
        """HTTPS URL of the wiki repository."""
        return self.wiki_url_for("https")
