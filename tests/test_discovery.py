"""Tests for freki.discovery using fake gitlab client objects (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import gitlab
import pytest
from gitlab.exceptions import GitlabGetError, GitlabListError
from gitlab.v4.objects import GroupDescendantGroup

from freki.discovery import ProjectDiscovery
from freki.models import ProjectInfo

# --------------------------------------------------------------------------- fakes


def full_project(pid: int, path: str, *, archived: bool = False, wiki: bool = True):
    return SimpleNamespace(
        id=pid,
        path_with_namespace=path,
        http_url_to_repo=f"https://git.example.org/{path}.git",
        ssh_url_to_repo=f"git@git.example.org:{path}.git",
        archived=archived,
        wiki_enabled=wiki,
        default_branch="main",
    )


def light_project(pid: int, path: str, *, archived: bool = False):
    """GroupProject-like object without ``wiki_enabled``."""
    return SimpleNamespace(
        id=pid,
        path_with_namespace=path,
        http_url_to_repo=f"https://git.example.org/{path}.git",
        archived=archived,
    )


class FakeListManager:
    """Records list() calls and returns items filtered by ``archived`` kwarg."""

    def __init__(self, items, *, error: Exception | None = None, honour_archived=True):
        self.items = list(items)
        self.calls: list[dict] = []
        self.error = error
        self.honour_archived = honour_archived

    def list(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        items = self.items
        if self.honour_archived and kwargs.get("archived") is False:
            items = [p for p in items if not getattr(p, "archived", False)]
        return iter(items)


class FakeGetManager:
    def __init__(self, by_key: dict, *, error: Exception | None = None):
        self.by_key = by_key
        self.error = error

    def get(self, key, **kwargs):
        if self.error is not None:
            raise self.error
        try:
            return self.by_key[key]
        except KeyError:
            raise GitlabGetError("404 Not Found", response_code=404) from None


class FakeProjectsManager(FakeGetManager, FakeListManager):
    """gl.projects: supports both get(id) and list(**kwargs)."""

    def __init__(self, full_projects, *, list_items=None):
        FakeGetManager.__init__(self, {p.id: p for p in full_projects})
        FakeListManager.__init__(self, list_items or [])
        self.list_by_kwargs: dict[str, list] = {}

    def list(self, **kwargs):
        self.calls.append(kwargs)
        items = []
        if kwargs.get("membership"):
            items = self.list_by_kwargs.get("membership", [])
        elif kwargs.get("owned"):
            items = self.list_by_kwargs.get("owned", [])
        if kwargs.get("archived") is False:
            items = [p for p in items if not getattr(p, "archived", False)]
        return iter(items)


def fake_group(gid, full_path, projects, *, parent_id=None, error=None):
    """``Group``-like object as returned by ``gl.groups.list()`` (has ``.projects``)."""
    return SimpleNamespace(
        id=gid,
        full_path=full_path,
        parent_id=parent_id,
        projects=FakeListManager(projects, error=error),
    )


def real_descendant(gid, full_path, parent_id):
    """A genuine python-gitlab ``GroupDescendantGroup``: no ``.projects`` manager."""
    gl = gitlab.Gitlab("http://127.0.0.1:9", private_token="x")
    manager = gl.groups.get(parent_id, lazy=True).descendant_groups
    obj = GroupDescendantGroup(manager, {"id": gid, "full_path": full_path, "parent_id": parent_id})
    assert not hasattr(obj, "projects")
    return obj


def make_gl(*, full_projects=(), groups=(), group_list=None):
    gl = SimpleNamespace()
    gl.projects = FakeProjectsManager(list(full_projects))
    gl.groups = FakeGetManager({g.full_path: g for g in groups} | {g.id: g for g in groups})
    gl.groups.list = FakeListManager(group_list if group_list is not None else groups).list
    return gl


# --------------------------------------------------------------------------- get_project


def test_get_project_returns_info():
    gl = make_gl(full_projects=[full_project(1, "grp/a")])
    info = ProjectDiscovery(gl, include_archived=False).get_project(1)
    assert isinstance(info, ProjectInfo)
    assert info.path_with_namespace == "grp/a"
    assert info.wiki_enabled is True
    assert info.wiki_url == "https://git.example.org/grp/a.wiki.git"
    assert info.wiki_url_for("https") == info.wiki_url
    assert info.repo_url("ssh") == "git@git.example.org:grp/a.git"
    assert info.wiki_url_for("ssh") == "git@git.example.org:grp/a.wiki.git"


def test_get_project_skips_archived_unless_included():
    gl = make_gl(full_projects=[full_project(2, "grp/old", archived=True)])
    assert ProjectDiscovery(gl, include_archived=False).get_project(2) is None
    info = ProjectDiscovery(gl, include_archived=True).get_project(2)
    assert info is not None and info.archived is True


def test_get_project_raises_for_missing():
    gl = make_gl()
    with pytest.raises(GitlabGetError):
        ProjectDiscovery(gl).get_project("nope/nope")


# --------------------------------------------------------------------------- groups


def _group_fixture():
    # A group whose projects.list(include_subgroups=True) yields projects from
    # the group and its subgroup; one of them archived. The group endpoint
    # returns lightweight objects (no wiki_enabled) so the full project is fetched.
    p1 = light_project(10, "top/a")
    p2 = light_project(11, "top/sub/b")
    p3 = light_project(12, "top/sub/archived", archived=True)
    full = [
        full_project(10, "top/a", wiki=True),
        full_project(11, "top/sub/b", wiki=False),
        full_project(12, "top/sub/archived", archived=True),
    ]
    group = fake_group(100, "top", [p1, p2, p3])
    return group, full


def test_group_recursion_filters_archived_and_resolves_wiki():
    group, full = _group_fixture()
    gl = make_gl(full_projects=full, groups=[group])
    result = ProjectDiscovery(gl, include_archived=False).get_group_projects("top")

    assert [p.path_with_namespace for p in result] == ["top/a", "top/sub/b"]
    assert {p.id: p.wiki_enabled for p in result} == {10: True, 11: False}

    call = group.projects.calls[0]
    assert call["include_subgroups"] is True
    assert call["iterator"] is True
    assert call["with_shared"] is False
    assert call["archived"] is False


def test_group_include_archived_passes_no_archived_filter():
    group, full = _group_fixture()
    gl = make_gl(full_projects=full, groups=[group])
    result = ProjectDiscovery(gl, include_archived=True).get_group_projects(100, with_shared=True)

    assert [p.path_with_namespace for p in result] == ["top/a", "top/sub/archived", "top/sub/b"]
    call = group.projects.calls[0]
    assert "archived" not in call
    assert call["with_shared"] is True


def test_group_client_side_archived_guard_when_server_ignores_filter():
    group, full = _group_fixture()
    group.projects.honour_archived = False  # server returns archived anyway
    gl = make_gl(full_projects=full, groups=[group])
    result = ProjectDiscovery(gl, include_archived=False).get_group_projects("top")
    assert [p.id for p in result] == [10, 11]


def test_group_list_error_is_tolerated():
    group = fake_group(5, "broken", [], error=GitlabListError("boom", response_code=500))
    gl = make_gl(groups=[group])
    assert ProjectDiscovery(gl).get_group_projects("broken") == []


def test_group_missing_raises():
    gl = make_gl()
    with pytest.raises(GitlabGetError):
        ProjectDiscovery(gl).get_group_projects("missing")


# --------------------------------------------------------------------------- user mode


def test_user_mode_dedupes_and_sorts():
    a = full_project(1, "me/personal")
    b = full_project(2, "team/app")
    c = full_project(3, "team/sub/lib")
    old = full_project(4, "team/legacy", archived=True)

    team = fake_group(20, "team", [b, c, old])
    # groups.list returns team *and* its subgroup (GitLab lists the whole accessible
    # hierarchy); the subgroup must not be listed again as team already covers it.
    sub = fake_group(21, "team/sub", [c], parent_id=20)

    gl = make_gl(full_projects=[a, b, c, old], groups=[team, sub], group_list=[team, sub, sub])
    gl.projects.list_by_kwargs = {"membership": [a, b, old], "owned": [a]}

    result = ProjectDiscovery(gl, include_archived=False).get_user_projects()

    assert [p.path_with_namespace for p in result] == ["me/personal", "team/app", "team/sub/lib"]
    assert len({p.id for p in result}) == 3
    # only the root group is walked (with include_subgroups=True), exactly once
    assert len(team.projects.calls) == 1
    assert team.projects.calls[0]["include_subgroups"] is True
    assert len(sub.projects.calls) == 0
    assert len(team.projects.calls) + len(sub.projects.calls) == 1
    # membership/owned both queried, with archived filter
    kinds = [("membership" in c, "owned" in c) for c in gl.projects.calls]
    assert (True, False) in kinds and (False, True) in kinds
    assert all(c["archived"] is False and c["iterator"] is True for c in gl.projects.calls)


def test_user_mode_walks_only_roots_of_nested_hierarchy():
    p_top = full_project(1, "faculty/readme")
    p_course = full_project(2, "faculty/course/slides")
    p_team = full_project(3, "faculty/course/team/app")
    p_other = full_project(4, "other/sub/tool")

    faculty = fake_group(1, "faculty", [p_top, p_course, p_team])
    course = fake_group(2, "faculty/course", [p_course, p_team], parent_id=1)
    team = fake_group(3, "faculty/course/team", [p_team], parent_id=2)
    # member of a subgroup only: its parent (id 99) is not accessible/listed -> a root
    other_sub = fake_group(4, "other/sub", [p_other], parent_id=99)

    groups = [faculty, course, team, other_sub]
    gl = make_gl(full_projects=[p_top, p_course, p_team, p_other], groups=groups)
    result = ProjectDiscovery(gl).get_user_projects()

    assert [p.path_with_namespace for p in result] == [
        "faculty/course/slides",
        "faculty/course/team/app",
        "faculty/readme",
        "other/sub/tool",
    ]
    calls = {g.full_path: len(g.projects.calls) for g in groups}
    assert calls == {"faculty": 1, "faculty/course": 0, "faculty/course/team": 0, "other/sub": 1}
    assert sum(calls.values()) == 2


def test_user_mode_survives_group_object_without_projects_manager():
    """A real ``GroupDescendantGroup`` has no ``.projects``; it must never crash the run."""
    good = full_project(1, "team/app")
    team = fake_group(20, "team", [good])
    orphan = real_descendant(30, "elsewhere/sub", parent_id=99)  # parent not listed -> root

    gl = make_gl(full_projects=[good], groups=[team], group_list=[team, orphan])
    result = ProjectDiscovery(gl).get_user_projects()

    assert [p.id for p in result] == [1]
    assert len(team.projects.calls) == 1


def test_user_mode_includes_archived_when_requested():
    old = full_project(4, "team/legacy", archived=True)
    team = fake_group(20, "team", [old])
    gl = make_gl(full_projects=[old], groups=[team])
    gl.projects.list_by_kwargs = {"membership": [old]}
    result = ProjectDiscovery(gl, include_archived=True).get_user_projects()
    assert [p.id for p in result] == [4]


def test_user_mode_tolerates_group_errors():
    good = full_project(1, "ok/proj")
    ok = fake_group(1, "ok", [good])
    broken = fake_group(2, "broken", [], error=GitlabListError("nope", response_code=403))
    gl = make_gl(full_projects=[good], groups=[ok, broken])
    result = ProjectDiscovery(gl).get_user_projects()
    assert [p.id for p in result] == [1]
