"""Configuration (pydantic-settings) and GitLab client construction."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import gitlab
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Values are read from (highest precedence first): explicit constructor
    kwargs (CLI flags), environment variables, a ``.env`` file in the current
    working directory. Environment variable names are exactly ``GITLAB_URL``,
    ``GITLAB_TOKEN``, ``GITLAB_OUTPUT_DIR``, ``GITLAB_INCLUDE_ARCHIVED``,
    ``GITLAB_CLONE_WIKI``, ``GITLAB_CLONE_LFS``, ``GITLAB_CHECKOUT``,
    ``GITLAB_EXPORT_ISSUES``, ``GITLAB_EXPORT_MRS``, ``GITLAB_CONCURRENCY`` and
    ``GITLAB_GIT_PROTOCOL``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    gitlab_url: str = Field(
        validation_alias="GITLAB_URL",
        description="Base URL of the GitLab instance, e.g. https://gitlab.example.com.",
    )
    gitlab_token: SecretStr = Field(
        validation_alias="GITLAB_TOKEN",
        description=(
            "Personal access token (scopes: read_api; plus read_repository for --protocol https)."
        ),
    )
    output_dir: Path = Field(
        default=Path("./gitlab-export"),
        validation_alias="GITLAB_OUTPUT_DIR",
        description="Directory into which mirrors are cloned.",
    )
    include_archived: bool = Field(
        default=False,
        validation_alias="GITLAB_INCLUDE_ARCHIVED",
        description="Also clone archived projects.",
    )
    clone_wiki: bool = Field(
        default=True,
        validation_alias="GITLAB_CLONE_WIKI",
        description="Mirror the project wiki repository when the wiki is enabled.",
    )
    clone_lfs: bool = Field(
        default=True,
        validation_alias="GITLAB_CLONE_LFS",
        description="Run 'git lfs fetch --all' in each mirror.",
    )
    checkout: bool = Field(
        default=True,
        validation_alias="GITLAB_CHECKOUT",
        description=(
            "Also keep a working-tree checkout of the default branch next to each mirror "
            "(<project>/ beside <project>.git)."
        ),
    )
    export_issues: bool = Field(
        default=True,
        validation_alias="GITLAB_EXPORT_ISSUES",
        description="Export issues with comments and attachments to <project>-issues/.",
    )
    export_mrs: bool = Field(
        default=True,
        validation_alias="GITLAB_EXPORT_MRS",
        description=(
            "Export merge requests with comments and attachments to <project>-merge-requests/."
        ),
    )
    git_protocol: Literal["ssh", "https"] = Field(
        default="ssh",
        validation_alias="GITLAB_GIT_PROTOCOL",
        description=(
            "Transport for git: 'ssh' uses the project's SSH URL and your ssh agent/keys, "
            "'https' authenticates with the token."
        ),
    )
    concurrency: int = Field(
        default=4,
        ge=1,
        validation_alias="GITLAB_CONCURRENCY",
        description="Number of projects processed in parallel.",
    )


def get_client(settings: Settings) -> gitlab.Gitlab:
    """Build an authenticated python-gitlab client for ``settings``.

    Calls ``.auth()`` so that an invalid URL or token fails fast with a
    ``gitlab.exceptions.GitlabAuthenticationError``.
    """
    client = gitlab.Gitlab(
        url=settings.gitlab_url.rstrip("/"),
        private_token=settings.gitlab_token.get_secret_value(),
        per_page=100,
    )
    client.auth()
    return client
