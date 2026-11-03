"""GitHub discovery, against a pretend GitHub CLI. Invented accounts, repositories and files only."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import make_tree
from portfolioforge import data, paths
from portfolioforge.batch import cli as bcli
from portfolioforge.batch import workflow as bw
from portfolioforge.drive import hydrate
from portfolioforge.drive.auth import DriveError
from portfolioforge.github import cli as ghcli
from portfolioforge.github import discovery as gd
from portfolioforge.github.client import GhResult, GitHubClient, GitHubError
from portfolioforge.grouping import cli as gcli
from portfolioforge.grouping.engine import propose_groups
from portfolioforge.inventory import JsonlSink, plan_targeted, run_scan
from portfolioforge.inventory.sinks import GITHUB_REPOS_NAME, load_inventory
from portfolioforge.understanding import cli as ucli
from test_publishing import real_github_cli_is_forbidden  # noqa: F401 (autouse fixture)

ME = "synth-me"
LOCAL_KEY = "20260301T000000Z"


def blob(path, size=100, sha=None, mode="100644"):
    return {"path": path, "mode": mode, "type": "blob", "size": size, "sha": sha or f"sha-{path}"}


def repo(name, owner=ME, **extra):
    return {"name": name, "full_name": f"{owner}/{name}", "owner": {"login": owner}, "private": False,
            "default_branch": "main", "fork": False, **extra}


def person(login, count):
    return {"login": login, "type": "User", "contributions": count}


def anonymous(count):
    return {"type": "Anonymous", "name": "Private Name", "email": "private@example.invalid", "contributions": count}


class FakeGh:
    """Anything but a read of a listed endpoint fails the test."""

    def __init__(self):
        self.repos, self.trees, self.people, self.details, self.commits = {}, {}, {}, {}, {}
        self.calls: list[list[str]] = []

    def add(self, info, files, people=(), **detail):
        self.repos[info["full_name"]] = info
        self.trees[info["full_name"]] = files
        self.people[info["full_name"]] = list(people)
        self.details[info["full_name"]] = {**info, **detail}
        return info

    def __call__(self, argv):
        argv = list(argv)
        assert argv[:4] == ["gh", "api", "-X", "GET"], f"not a read: {argv}"
        self.calls.append(argv)
        endpoint = argv[4]
        assert "/contents" not in endpoint and "/blobs" not in endpoint and "/compare" not in endpoint, "file contents were requested"
        query = dict(a.split("=", 1) for a in argv[5:] if a != "-f")
        if endpoint == "user":
            return _ok({"login": ME})
        if endpoint == "user/repos":
            return _ok(list(self.repos.values()) if query.get("page") == "1" else [])
        match = re.fullmatch(r"repos/([^/]+/[^/]+)(?:/(contributors|commits|git/trees/.+))?", endpoint)
        assert match, f"unexpected endpoint {endpoint}"
        full, rest = match.group(1), match.group(2)
        if full not in self.repos:
            return GhResult(1, "", "gh: Not Found (HTTP 404)")
        if rest is None:
            return _ok(self.details[full])
        if rest == "contributors":
            return _ok(self.people[full] if query.get("page") == "1" else [])
        if rest == "commits":
            return _ok(self.commits.get((full, query["path"]), []))
        files = self.trees[full]
        if files is None:
            return GhResult(1, "", "gh: Git Repository is empty. (HTTP 409)")
        return _ok({"tree": files, "truncated": False})


def _ok(body):
    return GhResult(0, json.dumps(body), "")


def commit(login=None, date="2024-03-01T10:00:00Z"):
    return {"author": {"login": login} if login else None, "commit": {"author": {"date": date, "name": "Private Name"}}}


def info_of(**values):
    return gd.RepoInfo(full_name="x/y", folder="y (x)", owner=values.pop("owner", ME), private=False, fork=values.pop("fork", False),
                       contributors_known=values.pop("known", True), **values)


# --- attribution ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("values", "expected"), [
    ({"mine": 5}, gd.LIKELY_ORIGINAL),
    ({"unattributed": 5}, gd.LIKELY_ORIGINAL),
    ({"mine": 5, "others": 1}, gd.SHARED),
    ({"mine": 5, "template": "t/t"}, gd.SHARED),
    ({"mine": 5, "submodules": 1}, gd.SHARED),
    ({}, gd.UNKNOWN),
    ({"mine": 5, "known": False}, gd.UNKNOWN),
    ({"owner": "someone", "mine": 3, "others": 9}, gd.SHARED),
    ({"owner": "someone", "others": 9}, gd.THIRD_PARTY),
    ({"owner": "someone", "unattributed": 9}, gd.UNKNOWN),
    ({"fork": True, "others": 9}, gd.THIRD_PARTY),
    ({"fork": True, "mine": 1, "others": 9}, gd.SHARED),
])
def test_labels_are_cautious(values, expected):
    assert gd.classify(info_of(**values), ME)[0] == expected


def test_only_your_own_unshared_repository_is_ever_likely_original():
    for values in ({"fork": True, "mine": 100}, {"owner": "someone", "mine": 100}, {"mine": 100, "others": 1}):
        assert gd.classify(info_of(**values), ME)[0] != gd.LIKELY_ORIGINAL


# --- the client can only read metadata --------------------------------------------------------------------------


def test_requests_off_the_read_only_list_are_refused_before_running():
    fake = FakeGh()
    client = GitHubClient(fake)
    for endpoint in ("repos/a/b/contents/x.py", "repos/a/b/git/blobs/abc", "repos/a/b/compare/x...y", "repos/a/b/forks", "graphql",
                     "-X", "repos/a/b/git/trees/x/../../../blobs/1", "repos/../../user/keys", "repos/a/../git/blobs"):
        with pytest.raises(GitHubError):
            client._get(endpoint)
    assert not fake.calls


def test_a_bad_repository_name_is_refused():
    with pytest.raises(GitHubError):
        GitHubClient(FakeGh()).tree("../evil", "main")


def test_errors_never_repeat_what_github_said():
    client = GitHubClient(lambda argv: GhResult(1, "", "secret-repo-name failed (HTTP 500)"))
    with pytest.raises(GitHubError) as caught:
        client.login()
    assert "secret-repo-name" not in str(caught.value) and "500" in str(caught.value)
    with pytest.raises(GitHubError, match="gh auth login"):
        GitHubClient(lambda argv: GhResult(127, "", "FileNotFoundError")).login()


def test_a_branch_with_a_slash_stays_one_path_part():
    fake = FakeGh()
    fake.add(repo("a"), [], [person(ME, 1)])
    GitHubClient(fake).tree(f"{ME}/a", "feature/x")
    assert fake.calls[-1][4].endswith("/git/trees/feature%2Fx")


# --- a scan ---------------------------------------------------------------------------------------------------


def _world() -> FakeGh:
    fake = FakeGh()
    fake.add(repo("solo-project"), [blob("main.py"), blob("README.md"), blob("analysis.ipynb", 500)], [person(ME, 12)])
    team = repo("team-project", owner="classmate")
    fake.add(team, [blob("CNN.ipynb", 700), blob("Models.ipynb", 800), blob("util.py", 50)], [person(ME, 4), person("classmate", 20), anonymous(2)])
    fake.commits[(team["full_name"], "CNN.ipynb")] = [commit(ME)]
    fake.commits[(team["full_name"], "Models.ipynb")] = [commit("classmate"), commit(None)]
    fake.add(repo("fork-of-course", private=True), [blob("CNN.ipynb", 600), blob("lab.py", 60)], [person("teacher", 40)],
             fork=True, parent={"full_name": "teacher/course"})
    fake.add(repo("empty-one"), None)
    fake.add(repo("Taxes"), [blob("return.py")], [person(ME, 1)])
    fake.add(repo("PortfolioForge"), [blob("pyproject.toml"), blob("src/portfolioforge/inventory", 0), blob("x.py")], [person(ME, 1)])
    return fake


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "pf-settings"))
    return tmp_path


@pytest.fixture
def cli(homes, monkeypatch, capsys):
    fake = _world()
    monkeypatch.setattr(ghcli, "make_client", lambda: GitHubClient(fake))

    def run(*args, answer=None):
        def asked(prompt):
            assert answer is not None, f"unexpected question: {prompt}"
            return answer
        code = ghcli.main(["scan", *args], input_fn=asked)
        output = capsys.readouterr()
        return code, output.out + output.err

    run.fake = fake
    return run


def _scanned():
    key, inventory = gcli.resolve_inventory("latest-github")
    return key, inventory, json.loads((paths.inventories_dir() / key / GITHUB_REPOS_NAME).read_text(encoding="utf-8"))


def _labels(sidecar):
    return {r["full_name"]: r["label"] for r in sidecar["repos"]}


def test_scanning_every_repository_asks_first(cli):
    code, out = cli(answer="no")
    assert code == 1 and "Nothing was scanned" in out
    assert not data.stored_inventories()
    assert not any("/git/trees/" in c[4] for c in cli.fake.calls)


def test_a_scan_labels_each_repository_and_never_requests_contents(cli):
    code, out = cli("--yes", "--compare-with", "none", "--compare-drive", "none")
    assert code == 0, out
    _, inventory, sidecar = _scanned()
    assert _labels(sidecar) == {f"{ME}/solo-project": gd.LIKELY_ORIGINAL, "classmate/team-project": gd.SHARED,
                                f"{ME}/fork-of-course": gd.THIRD_PARTY}
    assert {f.category.value for f in inventory.files} >= {"code", "notebook"}
    assert all(f.absolute_path.startswith("github://") for f in inventory.files)
    assert all(c[:4] == ["gh", "api", "-X", "GET"] for c in cli.fake.calls)
    assert "Left out: 1 empty, 1 by your private or excluded names, 1 PortfolioForge's own source" in out
    assert "return.py" not in inventory.to_json(), "a repository with a private name stays out"


def test_each_repository_becomes_one_candidate_project(cli):
    cli("--yes", "--compare-with", "none", "--compare-drive", "none")
    _, inventory, _ = _scanned()
    run = propose_groups(inventory)
    projects = {g.working_title.text: len(g.members) for g in run.groups}
    assert projects == {"solo-project (synth-me)": 3, "team-project (classmate)": 3, "fork-of-course (synth-me)": 2}
    assert not run.unassigned


def test_no_private_contributor_detail_is_kept(cli):
    cli("--yes", "--compare-with", "none", "--compare-drive", "none")
    key, inventory, _ = _scanned()
    text = (paths.inventories_dir() / key / GITHUB_REPOS_NAME).read_text(encoding="utf-8") + inventory.to_json()
    assert "Private Name" not in text and "private@example.invalid" not in text


def test_notebooks_get_commit_counts_within_a_budget(cli):
    cli("--yes", "--compare-with", "none", "--compare-drive", "none")
    _, _, sidecar = _scanned()
    team = next(r for r in sidecar["repos"] if r["full_name"] == "classmate/team-project")
    assert team["file_commits"]["CNN.ipynb"] == {"mine": 1, "others": 0, "unattributed": 0}
    assert team["file_commits"]["Models.ipynb"] == {"mine": 0, "others": 1, "unattributed": 1}
    cli.fake.calls.clear()
    cli("--yes", "--max-commit-lookups", "1", "--compare-with", "none", "--compare-drive", "none")
    assert sum(c[4].endswith("/commits") for c in cli.fake.calls) == 1


def test_one_repository_can_be_chosen_without_a_question(cli):
    code, out = cli("--repo", "classmate/team-project", "--compare-with", "none", "--compare-drive", "none")
    assert code == 0, out
    assert list(_labels(_scanned()[2])) == ["classmate/team-project"]
    code, out = cli("--repo", "nobody/nothing")
    assert code == 1 and "wasn't found" in out
    code, out = cli("--repo", "not a repo")
    assert code == 1 and "OWNER/NAME" in out


def test_notebooks_only_keeps_just_notebooks(cli):
    cli("--yes", "--notebooks-only", "--compare-with", "none", "--compare-drive", "none")
    _, inventory, _ = _scanned()
    assert {f.category.value for f in inventory.files} == {"notebook"}


def test_links_submodules_and_sensitive_names_are_not_recorded(homes, cli):
    fake = cli.fake
    fake.repos.clear()
    fake.add(repo("mixed"), [blob("a.py"), blob("link.py", mode="120000"), blob(".env"), blob("keys/server.pem"), blob("Medical/record.py"),
                             {"path": "vendor/lib", "mode": "160000", "type": "commit", "sha": "x"}], [person(ME, 2)])
    assert cli("--yes", "--compare-with", "none", "--compare-drive", "none")[0] == 0
    _, inventory, sidecar = _scanned()
    assert {f.name for f in inventory.files} == {"a.py"}
    for hidden in ("server.pem", "record.py", "Medical"):
        assert hidden not in inventory.to_json()
    assert sidecar["repos"][0]["label"] == gd.SHARED, "a submodule means other people's code"


def test_hostile_paths_stay_safe(homes, cli):
    fake = cli.fake
    fake.repos.clear()
    fake.add(repo("evil"), [blob("../../outside.py"), blob("a/../../b.py"), blob("CON/x.py")], [person(ME, 1)])
    assert cli("--yes", "--compare-with", "none", "--compare-drive", "none")[0] == 0
    _, inventory, _ = _scanned()
    assert inventory.files
    assert all(".." not in f.path_parts and ".." != f.name for f in inventory.files)


def test_a_cut_off_listing_is_reported_as_incomplete(cli, monkeypatch):
    real = GitHubClient.tree
    monkeypatch.setattr(GitHubClient, "tree", lambda self, name, branch: (lambda t: t and {**t, "truncated": True})(real(self, name, branch)))
    code, out = cli("--yes", "--compare-with", "none", "--compare-drive", "none")
    assert code == 3 and "INCOMPLETE" in out


def test_a_failed_scan_leaves_no_inventory(cli, monkeypatch):
    monkeypatch.setattr(ghcli, "make_client", lambda: GitHubClient(lambda argv: GhResult(1, "", "boom (HTTP 500)")))
    code, out = cli("--yes")
    assert code == 1 and "HTTP 500" in out and not data.stored_inventories()


def test_redacted_output_names_nothing(cli):
    code, out = cli("--yes", "--redact", "--compare-with", "none", "--compare-drive", "none")
    assert code == 0
    for name in ("team-project", "classmate", "solo-project", "CNN"):
        assert name not in out


def test_github_never_becomes_the_latest_of_anything_else(cli, tmp_path, fake_system):
    root = make_tree(tmp_path / "Local", {"Work/app.py": "x"})
    with JsonlSink(paths.inventories_dir() / LOCAL_KEY) as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)
    cli("--yes", "--compare-with", "none", "--compare-drive", "none")
    assert gcli.resolve_inventory("latest")[0] == LOCAL_KEY
    assert gcli.resolve_inventory("latest-github")[0].startswith("github-")
    with pytest.raises(gcli.UsageError):
        gcli.resolve_inventory("latest-drive")


def test_purge_removes_a_github_inventory(cli):
    cli("--yes", "--compare-with", "none", "--compare-drive", "none")
    key = _scanned()[0]
    assert data.main(["purge", key, "--yes"]) == 0
    assert not (paths.inventories_dir() / key).exists()


# --- duplicates ------------------------------------------------------------------------------------------------


def _local(tmp_path, fake_system, files, dirs=()):
    root = make_tree(tmp_path / "Local", files, dirs)
    with JsonlSink(paths.inventories_dir() / LOCAL_KEY) as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)
    return root


def _concerns(key):
    from portfolioforge.drive.duplicates import load_concerns
    return [c for cs in load_concerns(key).values() for c in cs]


def test_a_local_copy_is_a_possible_duplicate_on_both_sides(cli, tmp_path, fake_system):
    fake = cli.fake
    fake.repos.clear()
    files = {"Sorter/main.py": "x" * 40, "Sorter/util.py": "y" * 50, "Sorter/README.md": "z" * 60}
    fake.add(repo("sorter"), [blob(p.split("/")[1], len(c)) for p, c in files.items()], [person(ME, 1)])
    _local(tmp_path, fake_system, files)
    code, out = cli("--yes", "--compare-drive", "none")
    assert code == 0 and "possible_duplicate" in out
    key = _scanned()[0]
    assert any("local project" in c for c in _concerns(key))
    assert any("GitHub project" in c for c in _concerns(LOCAL_KEY))
    assert not (paths.data_dir() / "groupings" / LOCAL_KEY).exists(), "comparing must not write the local inventory's records"


def test_comparing_with_two_sources_keeps_both_results(homes):
    from portfolioforge.drive import duplicates as d
    match = d.CrossSourceMatch("g1", "g2", "possible_variant", "x")
    d.save_matches("github-1", "local-1", [match], cloud_label="GitHub", other_label="local")
    d.save_matches("github-1", "drive-1", [match], cloud_label="GitHub", other_label="Google Drive")
    d.save_concerns("github-1", "attribution", {"g1": ["attribution: shared"]})
    found = d.load_concerns("github-1")["g1"]
    assert len(found) == 3 and any("Google Drive" in c for c in found) and any("local project" in c for c in found)


def test_a_broken_concerns_file_is_ignored(homes):
    from portfolioforge.drive import duplicates as d
    d.save_concerns("k", "good", {"g": ["kept"]})
    (d._concerns_folder("k") / "bad.json").write_text("not json")
    assert d.load_concerns("k") == {"g": ["kept"]}


def _git_config(folder: Path, url: str) -> None:
    (folder / ".git").mkdir(parents=True, exist_ok=True)
    (folder / ".git" / "config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n', encoding="utf-8")


@pytest.mark.parametrize(("url", "expected"), [
    ("https://github.com/owner/name.git", ("owner", "name")),
    ("git@github.com:owner/name.git", ("owner", "name")),
    ("https://synthetic-user:synthetic-pass@github.com/owner/name", ("owner", "name")),
    ("https://gitlab.com/owner/name.git", None),
])
def test_a_remote_is_read_without_keeping_credentials(tmp_path, url, expected):
    _git_config(tmp_path, url)
    assert gd.read_remote(str(tmp_path)) == expected


def test_no_config_means_no_remote(tmp_path):
    assert gd.read_remote(str(tmp_path)) is None


def _clone_world(cli, tmp_path, fake_system, url):
    fake = cli.fake
    fake.repos.clear()
    fake.add(repo("team", owner="classmate"), [blob("different.py", 5), blob("other.py", 6)], [person(ME, 1), person("classmate", 5)])
    root = make_tree(tmp_path / "Local", {"Clone/renamed.py": "x", "Clone/more.py": "y"})
    _git_config(root / "Clone", url)
    with JsonlSink(paths.inventories_dir() / LOCAL_KEY) as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)


def test_remotes_are_read_only_when_asked(cli, tmp_path, fake_system, monkeypatch):
    _clone_world(cli, tmp_path, fake_system, "https://github.com/classmate/team.git")
    monkeypatch.setattr(gd, "read_remote", lambda path: pytest.fail("a local file was opened without being asked"))
    code, out = cli("--yes", "--compare-drive", "none")
    assert code == 0 and "with local inventory" in out and ": 0." in out


def test_a_clone_matches_its_repository_even_with_different_files(cli, tmp_path, fake_system):
    _clone_world(cli, tmp_path, fake_system, "https://github.com/classmate/team.git")
    code, out = cli("--yes", "--compare-drive", "none", "--read-local-remotes")
    assert code == 0, out
    assert "this local clone's Git remote is the repository (shared)" in out


def test_a_clone_of_an_unlisted_repository_gets_an_attribution_concern(cli, tmp_path, fake_system):
    _clone_world(cli, tmp_path, fake_system, "https://github.com/famous/framework.git")
    assert cli("--yes", "--compare-drive", "none", "--read-local-remotes")[0] == 0
    assert any(c.startswith("attribution: unknown") for c in _concerns(LOCAL_KEY))


def test_your_own_unlisted_repository_raises_no_concern(cli, tmp_path, fake_system):
    _clone_world(cli, tmp_path, fake_system, f"https://github.com/{ME}/left-out.git")
    assert cli("--yes", "--compare-drive", "none", "--read-local-remotes")[0] == 0
    assert not any(c.startswith("attribution") for c in _concerns(LOCAL_KEY))


# --- into the batch: discovery only --------------------------------------------------------------------------


@pytest.fixture
def batch(cli, capsys):
    assert cli("--yes", "--compare-with", "none", "--compare-drive", "none")[0] == 0

    def run(*args, answer=None):
        code = bcli.main(["--inventory", "latest-github", *args], input_fn=lambda prompt: answer or "")
        output = capsys.readouterr()
        return code, output.out + output.err

    return run


def _group(title_part: str) -> str:
    ctx = bw.load_context("latest-github")
    return next(g.id for g in ctx.run.groups if title_part in g.working_title.text)


def test_the_batch_lists_candidates_with_their_attribution(batch):
    assert batch("run")[0] == 0
    code, review = batch("review")
    assert code == 0
    assert "needs_attribution_review" in review
    assert "Attribution:" in review
    assert "attribution: shared" in review and "attribution: third_party" in review and "attribution: likely_original" in review
    assert "don't prove who wrote the code" in review


def test_attribution_concerns_are_safe_under_redaction(batch):
    batch("run")
    for concerns in bw.load_queue(_scanned()[0]).jobs.values():
        for concern in concerns.concerns:
            for name in ("classmate", "team-project", "teacher", "CNN"):
                assert name not in concern


def test_a_shared_project_cannot_be_approved_for_reading(batch):
    batch("run")
    group = _group("team-project")
    code, out = batch("approve-read", group, "--acknowledge-review", answer="read 1")
    assert code == 1 and "Not at this step" in out
    assert not (paths.data_dir() / "understanding").exists() or not list((paths.data_dir() / "understanding").rglob("*approval*"))


def test_the_single_project_commands_refuse_too(batch, capsys):
    batch("run")
    group = _group("solo-project")
    assert gcli.main(["--inventory", "latest-github", "accept", group]) == 0
    capsys.readouterr()
    code = ucli.main(["--inventory", "latest-github", "approve-files", group, "--yes", "--acknowledge-review"],
                     input_fn=lambda prompt: pytest.fail("asked before refusing"))
    assert code == 1
    assert "clone it and run the local scan" in capsys.readouterr().err


def test_a_github_record_cannot_be_read_or_staged_even_with_an_approval_record(batch):
    from portfolioforge.privacy import PrivacySettings
    from portfolioforge.publishing import workflow as pw
    from portfolioforge.understanding.eligibility import assess_run
    from portfolioforge.understanding.reader import ApprovedFileReader, ReadRefused
    from portfolioforge.understanding.selection import approve_files, propose_selection

    key, inventory = gcli.resolve_inventory("latest-github")
    run = propose_groups(inventory)
    group = next(g for g in run.groups if "solo-project" in g.working_title.text)
    privacy = PrivacySettings()
    eligibility = next(e for e in assess_run(run, inventory) if e.group_id == group.id)
    record = approve_files(inventory_key=key, group=_accepted(group),
                           eligibility=eligibility, proposal=propose_selection(group, inventory, privacy), inventory=inventory,
                           acknowledge_review=True)
    reader = ApprovedFileReader(record, privacy)
    with pytest.raises(ReadRefused):
        reader.read_bytes(record.files[0].file_id)
    candidates, _ = pw.list_candidates(group, inventory, privacy, key)
    assert all(c.absolute_path.startswith("github://") for c in candidates)


def _accepted(group):
    from dataclasses import replace

    from portfolioforge.grouping.models import GroupStatus
    return replace(group, status=GroupStatus.ACCEPTED)


def test_a_github_inventory_can_never_hydrate_or_be_opened(batch):
    _, inventory = gcli.resolve_inventory("latest-github")
    with pytest.raises(DriveError, match="doesn't read files"):
        hydrate.require_download_permission(inventory)
    assert hydrate.download_approved(inventory, "k", [inventory.files[0].id]) == 0
    assert not Path(inventory.files[0].absolute_path).exists()


def test_nothing_reaches_a_provider_or_github_writes_from_a_batch_run(batch, monkeypatch):
    monkeypatch.setattr(ucli, "make_provider", lambda *a: pytest.fail("an AI provider was contacted"))
    assert batch("run", "--provider", "anthropic")[0] == 0
    _, status = batch("status")
    assert "needs_disclosure_approval" not in status and "needs_publish_approval" not in status


def test_setting_a_project_aside_still_works(batch):
    batch("run")
    group = _group("fork-of-course")
    code, _ = batch("exclude", group, "--reason", "someone else's course material")
    assert code == 0
    _, status = batch("status")
    assert "excluded" in status


def test_the_package_cannot_fetch_contents_or_publish():
    package = Path(gd.__file__).parent
    source = "".join(p.read_text(encoding="utf-8") for p in package.glob("*.py"))
    imports = "\n".join(re.findall(r"(?m)^\s*(?:from|import) .*$", source))
    for forbidden in ("publishing", "anthropic", "validation", "understanding", "hydrate"):
        assert forbidden not in imports, forbidden
    for word in ("/contents", "/blobs", "/compare", "git/blobs", "--method", '"POST"', '"PUT"', '"PATCH"', '"DELETE"', "clone("):
        assert word not in source, word


def test_saved_inventory_loads_like_any_other(cli):
    cli("--yes", "--compare-with", "none", "--compare-drive", "none")
    key = _scanned()[0]
    assert load_inventory(paths.inventories_dir() / key).report.mode.value == "github"
