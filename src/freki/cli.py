"""Typer CLI entry point (``freki group|project|user``, alias ``bms``)."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from gitlab.exceptions import GitlabAuthenticationError, GitlabError, GitlabGetError
from pydantic import ValidationError
from rich import get_console
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table

from freki import __version__
from freki.cloner import CloneResult, GitCloner
from freki.config import Settings, get_client
from freki.discovery import ProjectDiscovery
from freki.exporter import IssueExporter
from freki.models import ProjectInfo

EXIT_OK = 0
EXIT_FAILED = 1  # at least one project failed
EXIT_USAGE = 2  # configuration / authentication / not-found errors

console = get_console()  # shared with the progress bar in cloner.py
err_console = Console(stderr=True)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    help=(
        "Bulk-clone GitLab projects as full git mirrors (all branches and tags).\n\n"
        "Every project ends up in [bold]OUTPUT/<path_with_namespace>.git[/bold]; re-running "
        "updates existing mirrors instead of re-cloning. Settings are read from environment "
        "variables or a [bold].env[/bold] file (GITLAB_URL, GITLAB_TOKEN, GITLAB_OUTPUT_DIR, ...); "
        "command-line options take precedence."
    ),
)

# --------------------------------------------------------------------------- options

UrlOpt = Annotated[
    str | None,
    typer.Option(
        "--url",
        help="Base URL of the GitLab instance.",
        rich_help_panel="Connection",
        show_default="$GITLAB_URL",
    ),
]
TokenOpt = Annotated[
    str | None,
    typer.Option(
        "--token",
        envvar="GITLAB_TOKEN",
        prompt=False,
        hide_input=True,
        show_default=False,
        help="Personal access token (scopes: read_api; plus read_repository for --protocol https).",
        rich_help_panel="Connection",
    ),
]
OutputOpt = Annotated[
    Path | None,
    typer.Option(
        "--output",
        "-o",
        file_okay=False,
        help="Directory the mirrors are written to.",
        show_default="$GITLAB_OUTPUT_DIR or ./gitlab-export",
    ),
]
IncludeArchivedOpt = Annotated[
    bool | None,
    typer.Option(
        "--include-archived/--no-include-archived",
        help="Also clone archived projects.",
        show_default="$GITLAB_INCLUDE_ARCHIVED or off",
    ),
]
WikiOpt = Annotated[
    bool | None,
    typer.Option(
        "--wiki/--no-wiki",
        help="Mirror the project wiki when the wiki is enabled.",
        show_default="$GITLAB_CLONE_WIKI or on",
    ),
]
LfsOpt = Annotated[
    bool | None,
    typer.Option(
        "--lfs/--no-lfs",
        help="Run 'git lfs fetch --all' in each mirror.",
        show_default="$GITLAB_CLONE_LFS or on",
    ),
]
CheckoutOpt = Annotated[
    bool | None,
    typer.Option(
        "--checkout/--no-checkout",
        help="Keep a working-tree checkout of the default branch next to each mirror.",
        show_default="$GITLAB_CHECKOUT or on",
    ),
]
IssuesOpt = Annotated[
    bool | None,
    typer.Option(
        "--issues/--no-issues",
        help="Export issues (+comments, attachments) to <project>-issues/.",
        show_default="$GITLAB_EXPORT_ISSUES or on",
    ),
]
MrsOpt = Annotated[
    bool | None,
    typer.Option(
        "--mrs/--no-mrs",
        help="Export merge requests (+comments, attachments) to <project>-merge-requests/.",
        show_default="$GITLAB_EXPORT_MRS or on",
    ),
]
DryRunOpt = Annotated[
    bool,
    typer.Option("--dry-run", help="Only list what would be cloned/updated; touch nothing."),
]
ConcurrencyOpt = Annotated[
    int | None,
    typer.Option(
        "--concurrency",
        "-j",
        min=1,
        help="Number of projects processed in parallel.",
        show_default="$GITLAB_CONCURRENCY or 4",
    ),
]


class GitProtocol(StrEnum):
    ssh = "ssh"
    https = "https"


ProtocolOpt = Annotated[
    GitProtocol | None,
    typer.Option(
        "--protocol",
        help="Git transport: 'ssh' uses your ssh agent/keys, 'https' uses the token.",
        show_default="$GITLAB_GIT_PROTOCOL or ssh",
        rich_help_panel="Connection",
    ),
]
WithSharedOpt = Annotated[
    bool,
    typer.Option(
        "--with-shared",
        help="Also include projects that are merely shared with a group.",
    ),
]


@dataclass(frozen=True)
class CommonOptions:
    """Command-line values shared by all commands (``None`` = not given)."""

    url: str | None = None
    token: str | None = None
    output: Path | None = None
    include_archived: bool | None = None
    wiki: bool | None = None
    lfs: bool | None = None
    checkout: bool | None = None
    issues: bool | None = None
    mrs: bool | None = None
    dry_run: bool = False
    concurrency: int | None = None
    with_shared: bool = False
    protocol: GitProtocol | None = None

    def to_settings(self) -> Settings:
        """Build ``Settings`` from env/.env, overridden by the given CLI values."""
        overrides = {
            "gitlab_url": self.url,
            "gitlab_token": self.token,
            "output_dir": self.output,
            "include_archived": self.include_archived,
            "clone_wiki": self.wiki,
            "clone_lfs": self.lfs,
            "checkout": self.checkout,
            "export_issues": self.issues,
            "export_mrs": self.mrs,
            "concurrency": self.concurrency,
            "git_protocol": self.protocol.value if self.protocol else None,
        }
        return Settings(**{k: v for k, v in overrides.items() if v is not None})


# --------------------------------------------------------------------------- helpers


def coerce_id(value: str) -> int | str:
    """Numeric ids become ``int`` so python-gitlab does not URL-encode them as paths."""
    value = value.strip()
    return int(value) if value.isdigit() else value


def _setup_logging() -> None:
    logger = logging.getLogger("freki")
    if any(isinstance(h, RichHandler) for h in logger.handlers):
        return
    handler = RichHandler(
        console=console, show_path=False, show_time=False, rich_tracebacks=False, markup=False
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _fail(message: str, code: int = EXIT_USAGE) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {escape(message)}")
    raise typer.Exit(code)


def _yes_no(value: bool) -> str:
    return "[green]yes[/green]" if value else "[dim]no[/dim]"


def _plan_table(projects: list[ProjectInfo], settings: Settings) -> Table:
    table = Table(
        title=f"{len(projects)} project(s) to mirror into {escape(str(settings.output_dir))}"
        f" via {settings.git_protocol}"
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("Project", overflow="fold")
    table.add_column("Archived", justify="center")
    table.add_column("Wiki", justify="center")
    for idx, p in enumerate(projects, start=1):
        table.add_row(
            str(idx), escape(p.path_with_namespace), _yes_no(p.archived), _yes_no(p.wiki_enabled)
        )
    return table


_STATUS_STYLE = {
    "cloned": "green",
    "checked out": "green",
    "updated": "cyan",
    "unchanged": "dim",
    "skipped": "yellow",
    "failed": "bold red",
}


def _style_status(status: str | None) -> str:
    if status is None:
        return "[dim]-[/dim]"
    text = escape(status)
    for key, style in _STATUS_STYLE.items():
        if status.startswith(key):
            return f"[{style}]{text}[/{style}]"
    if status.startswith("empty"):
        return f"[dim]{text}[/dim]"
    return text


def _is_partial(result: CloneResult) -> bool:
    """Mirror succeeded but the wiki or LFS step failed (an 'empty' wiki is fine)."""
    return result.status != "failed" and any(
        (step or "").startswith("failed")
        for step in (
            result.wiki_status,
            result.lfs_status,
            result.checkout_status,
            result.issues_status,
            result.mrs_status,
        )
    )


def _summary_table(results: list[CloneResult]) -> Table:
    table = Table(title="Summary")
    table.add_column("Project", overflow="fold")
    table.add_column("Status")
    table.add_column("Refs", justify="right")
    table.add_column("Wiki")
    table.add_column("LFS")
    table.add_column("Checkout")
    table.add_column("Issues")
    table.add_column("MRs")
    table.add_column("Message", overflow="fold")
    for r in results:
        table.add_row(
            escape(r.project),
            _style_status(r.status),
            escape(r.changes or ""),
            _style_status(r.wiki_status),
            _style_status(r.lfs_status),
            _style_status(r.checkout_status),
            _style_status(r.issues_status),
            _style_status(r.mrs_status),
            escape(r.message or ""),
        )
    return table


def _print_counts(results: list[CloneResult]) -> int:
    """Print the totals line; return the number of results that must fail the run.

    A project counts as failed if its mirror failed *or* if its wiki/LFS step
    failed ('partial'). Partial results keep their main status so the status
    column stays honest, but they still make the exit code non-zero.
    """
    counts = Counter(r.status for r in results)
    partial = sum(1 for r in results if _is_partial(r))
    console.print(f"Total: {len(results)} project(s)", soft_wrap=True)
    for s in ("cloned", "updated", "unchanged", "skipped", "failed"):
        n = counts.get(s, 0)
        style = _STATUS_STYLE[s] if n else "dim"
        console.print(f"  [{style}]{n:>5} {s}[/{style}]", soft_wrap=True)
    if partial:
        console.print(
            f"  [bold red]{partial:>5} partial (wiki/LFS/checkout/export failed)[/bold red]",
            soft_wrap=True,
        )
    return counts.get("failed", 0) + partial


def _discover(mode: str, target: str | None, discovery: ProjectDiscovery, with_shared: bool):
    if mode == "group":
        assert target is not None
        console.print(
            f"Discovering projects of group [bold]{escape(target)}[/bold] (incl. subgroups)..."
        )
        return discovery.get_group_projects(coerce_id(target), with_shared=with_shared)
    if mode == "project":
        assert target is not None
        console.print(f"Looking up project [bold]{escape(target)}[/bold]...")
        info = discovery.get_project(coerce_id(target))
        return [info] if info is not None else []
    console.print("Discovering all projects accessible to the authenticated user...")
    return discovery.get_user_projects(with_shared=with_shared)


def _execute(mode: str, target: str | None, opts: CommonOptions) -> None:
    """Shared workflow: settings -> auth -> discover -> plan -> clone -> summary."""
    _setup_logging()

    try:
        settings = opts.to_settings()
    except ValidationError as exc:
        missing = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
        if any("GITLAB_URL" in m or "gitlab_url" in m for m in missing):
            _fail("no GitLab URL given. Use --url, set GITLAB_URL, or add it to .env.")
        if any("GITLAB_TOKEN" in m or "gitlab_token" in m for m in missing):
            _fail("no token given. Use --token, set GITLAB_TOKEN, or add it to .env.")
        _fail(f"invalid configuration: {exc}")

    try:
        client = get_client(settings)
    except GitlabAuthenticationError as exc:
        _fail(f"authentication against {settings.gitlab_url} failed: {exc}")
    except GitlabError as exc:
        _fail(f"could not talk to {settings.gitlab_url}: {exc}")

    username = getattr(getattr(client, "user", None), "username", None) or "<unknown>"
    console.print(
        f"Authenticated as [bold]{escape(str(username))}[/bold] at {escape(settings.gitlab_url)}"
    )

    discovery = ProjectDiscovery(client, include_archived=settings.include_archived)
    try:
        projects = list(_discover(mode, target, discovery, opts.with_shared))
    except GitlabGetError as exc:
        _fail(f"{mode} '{target}' not found or not accessible: {exc}")
    except GitlabError as exc:
        _fail(f"discovery failed: {exc}")

    if not projects:
        console.print("[yellow]No projects to mirror.[/yellow]")
        raise typer.Exit(EXIT_OK)

    console.print(_plan_table(projects, settings))
    if opts.dry_run:
        console.print("[yellow]Dry run:[/yellow] nothing will be cloned or updated.")

    exporter = None
    if settings.export_issues or settings.export_mrs:
        exporter = IssueExporter(
            client,
            settings.output_dir,
            settings.gitlab_token.get_secret_value(),
            issues=settings.export_issues,
            merge_requests=settings.export_mrs,
        )
    cloner = GitCloner(
        output_dir=settings.output_dir,
        token=settings.gitlab_token.get_secret_value(),
        clone_wiki=settings.clone_wiki,
        clone_lfs=settings.clone_lfs,
        dry_run=opts.dry_run,
        protocol=settings.git_protocol,
        checkout=settings.checkout,
        exporter=exporter,
    )
    results = cloner.clone_all(projects, concurrency=settings.concurrency)

    console.print(_summary_table(results))
    failed = _print_counts(results)
    if failed:
        raise typer.Exit(EXIT_FAILED)


# --------------------------------------------------------------------------- commands


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"freki {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Bulk-clone GitLab projects as git mirrors."""


@app.command()
def group(
    group_id_or_path: Annotated[
        str, typer.Argument(help="Numeric group id or full path, e.g. 'faculty/course'.")
    ],
    url: UrlOpt = None,
    token: TokenOpt = None,
    output: OutputOpt = None,
    include_archived: IncludeArchivedOpt = None,
    wiki: WikiOpt = None,
    lfs: LfsOpt = None,
    checkout: CheckoutOpt = None,
    issues: IssuesOpt = None,
    mrs: MrsOpt = None,
    dry_run: DryRunOpt = False,
    concurrency: ConcurrencyOpt = None,
    with_shared: WithSharedOpt = False,
    protocol: ProtocolOpt = None,
) -> None:
    """Mirror all projects of a group, recursively including all subgroups."""
    _execute(
        "group",
        group_id_or_path,
        CommonOptions(
            url=url,
            token=token,
            output=output,
            include_archived=include_archived,
            wiki=wiki,
            lfs=lfs,
            checkout=checkout,
            issues=issues,
            mrs=mrs,
            dry_run=dry_run,
            concurrency=concurrency,
            with_shared=with_shared,
            protocol=protocol,
        ),
    )


@app.command()
def project(
    project_id_or_path: Annotated[
        str, typer.Argument(help="Numeric project id or full path, e.g. 'group/sub/project'.")
    ],
    url: UrlOpt = None,
    token: TokenOpt = None,
    output: OutputOpt = None,
    include_archived: IncludeArchivedOpt = None,
    wiki: WikiOpt = None,
    lfs: LfsOpt = None,
    checkout: CheckoutOpt = None,
    issues: IssuesOpt = None,
    mrs: MrsOpt = None,
    dry_run: DryRunOpt = False,
    concurrency: ConcurrencyOpt = None,
    protocol: ProtocolOpt = None,
) -> None:
    """Mirror a single project."""
    _execute(
        "project",
        project_id_or_path,
        CommonOptions(
            url=url,
            token=token,
            output=output,
            include_archived=include_archived,
            wiki=wiki,
            lfs=lfs,
            checkout=checkout,
            issues=issues,
            mrs=mrs,
            dry_run=dry_run,
            concurrency=concurrency,
            protocol=protocol,
        ),
    )


@app.command()
def user(
    url: UrlOpt = None,
    token: TokenOpt = None,
    output: OutputOpt = None,
    include_archived: IncludeArchivedOpt = None,
    wiki: WikiOpt = None,
    lfs: LfsOpt = None,
    checkout: CheckoutOpt = None,
    issues: IssuesOpt = None,
    mrs: MrsOpt = None,
    dry_run: DryRunOpt = False,
    concurrency: ConcurrencyOpt = None,
    with_shared: WithSharedOpt = False,
    protocol: ProtocolOpt = None,
) -> None:
    """Mirror everything the token's user can access.

    Owned/private projects plus all projects of every group (and subgroup) the
    user is a member of, deduplicated by project id.
    """
    _execute(
        "user",
        None,
        CommonOptions(
            url=url,
            token=token,
            output=output,
            include_archived=include_archived,
            wiki=wiki,
            lfs=lfs,
            checkout=checkout,
            issues=issues,
            mrs=mrs,
            dry_run=dry_run,
            concurrency=concurrency,
            with_shared=with_shared,
            protocol=protocol,
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    app()
