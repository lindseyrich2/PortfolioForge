"""Google Drive and Colab discovery, against a pretend Google. Invented names and files only."""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import make_tree
from drive_fixtures import (
    COLAB_MIME,
    DOC_MIME,
    ROOT_ID,
    SHORTCUT_MIME,
    FakeDrive,
    client_for,
    content_client_for,
    notebook,
)
from portfolioforge import data, paths
from portfolioforge.batch import cli as bcli
from portfolioforge.drive import auth, discovery, duplicates, hydrate
from portfolioforge.drive import cli as dcli
from portfolioforge.drive.auth import CONTENT_SCOPE, METADATA_SCOPE, DriveAuthError, DriveError, HttpResponse, OAuthClient
from portfolioforge.drive.client import quote_query
from portfolioforge.grouping import cli as gcli
from portfolioforge.grouping.engine import propose_groups
from portfolioforge.inventory import JsonlSink, MemorySink, plan_targeted, run_scan
from portfolioforge.inventory.config import ResourceLimits
from portfolioforge.inventory.models import ScanMode, SkipReason
from portfolioforge.inventory.sinks import load_inventory
from portfolioforge.publishing import cli as pcli
from portfolioforge.understanding import cli as ucli
from test_batch import FakeProvider
from test_publishing import FakeGitHub, real_github_cli_is_forbidden  # noqa: F401 (autouse fixture)

STARTED = datetime(2026, 3, 1, tzinfo=UTC)
LOCAL_KEY = "20260301T000000Z"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "pf-data"))
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "pf-settings"))
    return tmp_path


def inventory_of(drive: FakeDrive, options: discovery.DriveOptions | None = None, key: str = "drive-test"):
    options = options or discovery.DriveOptions()
    client = client_for(drive)
    targets = discovery.resolve_targets(client, options)
    listing = discovery.collect(client, options, targets)
    sink = MemorySink()
    result = discovery.build_inventory_records(listing, options, key, sink, STARTED)
    sink.close(result.report)
    return sink.inventory(), result


def paths_of(inventory) -> set[str]:
    return {f.relative_path for f in inventory.files}


def skips_of(inventory) -> set[tuple[str, str]]:
    return {(s.relative_path, s.reason.value) for s in inventory.skipped}


# --- permissions ---------------------------------------------------------------------------------------------


def test_only_two_permissions_exist_and_both_are_read_only():
    assert METADATA_SCOPE.endswith("/auth/drive.metadata.readonly")
    assert CONTENT_SCOPE.endswith("/auth/drive.readonly")
    source = "".join(p.read_text(encoding="utf-8") for p in Path(auth.__file__).parent.glob("*.py"))
    scopes = set(re.findall(r"auth/[\w.]+", source))
    assert scopes == {"auth/drive.metadata.readonly", "auth/drive.readonly"}


def _browser(token_status=200, *, scope=METADATA_SCOPE, sent=None, state_override=None):
    """Plays the user's browser and Google. A wrong-state request arrives first, and must be ignored."""
    def open_browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if sent is not None:
            sent["auth"] = query

        def respond():
            redirect = query["redirect_uri"][0]
            def get(params):
                try:
                    urllib.request.urlopen(f"{redirect}/?{urllib.parse.urlencode(params)}", timeout=5).read()
                except OSError:
                    pass
            get({"code": "stolen", "state": "wrong"})
            get({"code": "synthetic-code", "state": state_override or query["state"][0]})
        threading.Thread(target=respond, daemon=True).start()

    def transport(method, url, headers, body, max_bytes):
        assert (method, url) == ("POST", auth.TOKEN_URL)
        if sent is not None:
            sent["token"] = urllib.parse.parse_qs(body.decode())
        answer = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_in": 3600, "scope": scope}
        return HttpResponse(token_status, json.dumps(answer).encode())

    return open_browser, transport


def test_sign_in_uses_pkce_verifies_state_and_stores_one_permission(homes):
    sent, lines = {}, []
    open_browser, transport = _browser(sent=sent)
    auth.sign_in(METADATA_SCOPE, OAuthClient("synthetic-client"), transport=transport, open_browser=open_browser,
                 announce=lines.append, timeout=20)
    assert sent["auth"]["scope"] == [METADATA_SCOPE]
    assert sent["auth"]["code_challenge_method"] == ["S256"]
    assert "include_granted_scopes" not in sent["auth"]
    assert sent["token"]["code"] == ["synthetic-code"], "a response with the wrong state was accepted"
    assert sent["token"]["code_verifier"]
    assert sent["auth"]["redirect_uri"][0].startswith("http://127.0.0.1:")
    assert json.loads(auth.token_file(METADATA_SCOPE).read_text())["scope"] == METADATA_SCOPE
    assert not auth.is_connected(CONTENT_SCOPE)
    assert "synthetic-access" not in "".join(lines) and "synthetic-code" not in "".join(lines)


@pytest.mark.parametrize("granted", [CONTENT_SCOPE, f"{METADATA_SCOPE} {CONTENT_SCOPE}",
                                     f"{METADATA_SCOPE} https://www.googleapis.com/auth/drive", "openid"])
def test_a_grant_other_than_the_one_requested_is_discarded(homes, granted):
    open_browser, transport = _browser(scope=granted)
    with pytest.raises(DriveAuthError):
        auth.sign_in(METADATA_SCOPE, OAuthClient("c"), transport=transport, open_browser=open_browser,
                     announce=lambda _: None, timeout=20)
    assert not auth.is_connected(METADATA_SCOPE)


def test_a_cancelled_sign_in_stores_nothing(homes):
    def open_browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        params = urllib.parse.urlencode({"error": "access_denied", "state": query["state"][0]})
        threading.Thread(target=lambda: urllib.request.urlopen(f"{query['redirect_uri'][0]}/?{params}", timeout=5).read(),
                         daemon=True).start()

    with pytest.raises(DriveAuthError, match="cancelled"):
        auth.sign_in(METADATA_SCOPE, OAuthClient("c"), transport=None, open_browser=open_browser,
                     announce=lambda _: None, timeout=20)
    assert not auth.is_connected(METADATA_SCOPE)


def test_only_a_desktop_client_file_is_accepted():
    assert auth.parse_client('{"installed": {"client_id": "x", "client_secret": "y"}}') == OAuthClient("x", "y")
    for text in ('{"web": {"client_id": "x"}}', "nonsense", "[]"):
        with pytest.raises(DriveAuthError):
            auth.parse_client(text)


def test_an_expired_token_refreshes_and_a_refused_one_says_to_reconnect(homes):
    auth.client_file().parent.mkdir(parents=True)
    calls = []

    def transport(method, url, headers, body, max_bytes):
        calls.append(url)
        return HttpResponse(200, json.dumps({"access_token": "new", "expires_in": 3600}).encode())

    credentials_ = auth.Credentials(METADATA_SCOPE, OAuthClient("c"), {"access_token": "old", "refresh_token": "r", "expires_at": 0,
                                                                       "scope": METADATA_SCOPE}, transport=transport)
    assert credentials_.bearer() == "new" and calls == [auth.TOKEN_URL]

    refused = auth.Credentials(METADATA_SCOPE, OAuthClient("c"), {"access_token": "old", "refresh_token": "r", "expires_at": 0},
                               transport=lambda *a: HttpResponse(400, b'{"error": "invalid_grant"}'))
    with pytest.raises(DriveAuthError, match="Connect again"):
        refused.bearer()


def test_redirects_may_not_carry_the_token_away_from_google():
    handler = auth._GoogleOnlyRedirects()
    request = urllib.request.Request("https://www.googleapis.com/drive/v3/files/x", headers={"Authorization": "Bearer t"})
    for bad in ("https://evil.example/x", "http://www.googleapis.com/x", "https://googleapis.com.evil.example/x"):
        with pytest.raises(DriveError):
            handler.redirect_request(request, None, 302, "Found", {}, bad)
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://x.googleusercontent.com/y") is not None


def test_disconnect_removes_tokens_and_revoke_keeps_them_if_google_refuses(homes):
    auth._write_private(auth.token_file(METADATA_SCOPE), {"scope": METADATA_SCOPE, "refresh_token": "r"})
    with pytest.raises(DriveAuthError):
        auth.disconnect(revoke=True, transport=lambda *a: HttpResponse(500, b""))
    assert auth.is_connected(METADATA_SCOPE)
    assert auth.disconnect(revoke=True, transport=lambda *a: HttpResponse(200, b"")) == 1
    assert not auth.is_connected(METADATA_SCOPE)


# --- the client can only read --------------------------------------------------------------------------------


def test_the_discovery_permission_cannot_download():
    drive = FakeDrive()
    drive.file("f1", "a.py", content="x")
    with pytest.raises(DriveAuthError):
        client_for(drive).download("f1", 10)
    assert not drive.calls


def test_a_name_cannot_change_a_search_query():
    assert quote_query("it's") == r"'it\'s'"
    drive = FakeDrive()
    drive.folder("d1", "it's' or name contains '")
    options = discovery.DriveOptions(folders=("it's' or name contains '",))
    assert [t.drive_id for t in discovery.resolve_targets(client_for(drive), options)] == ["d1"]
    with pytest.raises(DriveError):
        client_for(drive).get("../../evil")


def test_rate_limits_are_retried_and_an_expired_token_is_refreshed_once():
    drive = FakeDrive()
    drive.responses = [HttpResponse(429, b"{}"),
                       HttpResponse(403, json.dumps({"error": {"errors": [{"reason": "rateLimitExceeded"}]}}).encode())]
    assert client_for(drive).get("root").id == ROOT_ID
    assert len(drive.calls) == 3

    refreshes = []

    class Tokens:
        scope = METADATA_SCOPE

        def bearer(self):
            return "t"

        def refresh(self):
            refreshes.append(1)

    drive = FakeDrive()
    drive.responses = [HttpResponse(401, b"{}"), HttpResponse(401, b"{}")]
    with pytest.raises(DriveAuthError):
        discovery.DriveClient(Tokens(), transport=drive, sleep=lambda s: None).get("root")
    assert refreshes == [1]


def test_errors_never_repeat_what_google_said():
    drive = FakeDrive()
    drive.responses = [HttpResponse(403, b'{"error": {"message": "secret-project.ipynb is restricted"}}')]
    with pytest.raises(DriveError) as caught:
        client_for(drive).get("root")
    assert "secret-project" not in str(caught.value)


# --- discovery follows the local scanner's rules --------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    ("..", "_"), (".", "_"), ("a/b", "a_b"), ("../../evil.py", ".._.._evil.py"), ("a\\b.py", "a_b.py"),
    ("CON", "_CON"), ("nul.txt", "_nul.txt"), ("trailing. ", "trailing"), ("what?*.py", "what__.py"),
    ("C:evil.py", "C_evil.py"), ("  spaced  ", "spaced"), ("", "_"), ("tab\there.py", "tab_here.py"),
])
def test_drive_names_become_safe_path_parts(raw, expected):
    assert discovery.safe_name(raw) == expected


def test_a_very_long_name_is_cut_and_keeps_its_extension():
    name = discovery.safe_name("x" * 400 + ".py")
    assert len(name) <= 120 and name.endswith(".py")


#: No line endings, so a file is the same size on every platform.
LOCAL_COPY = {"Sorting Project/main.py": "def sort_items(items): return sorted(items)", "Sorting Project/README.md": "# Sorting",
              "Sorting Project/requirements.txt": "numpy", "Sorting Project/results.csv": "a,b"}


def _realistic_drive() -> FakeDrive:
    drive = FakeDrive()
    project = drive.folder("d_project", "Sorting Project")
    for drive_id, path in (("f_main", "main.py"), ("f_readme", "README.md"), ("f_req", "requirements.txt"),
                           ("f_data", "results.csv")):
        drive.file(drive_id, path, project, LOCAL_COPY[f"Sorting Project/{path}"])
    drive.file("f_report", "Report", project, None, mime=DOC_MIME)
    drive.file("f_link", "shortcut.py", project, None, mime=SHORTCUT_MIME)
    drive.file("f_key", "server.pem", project, "PRIVATE")
    drive.file("f_env", ".env", project, "KEY=1")
    drive.file("f_token", "token.json", project, "{}")
    drive.file("f_lock", "~$report.docx", project, "x")
    drive.file("f_unknown", "notes.xyz", project, "x")
    drive.file("f_image", "plot.png", project, "x")
    drive.folder("d_git", ".git", project)
    drive.file("f_gitconfig", "config.py", "d_git", "x")
    drive.folder("d_build", "build", project)
    drive.file("f_built", "output.py", "d_build", "x")
    env = drive.folder("d_env", "my_env", project)
    drive.file("f_cfg", "pyvenv.cfg", env, "home = x")
    drive.file("f_envcode", "thing.py", env, "x")
    taxes = drive.folder("d_private", "Taxes")
    drive.file("f_tax", "return_2023.py", taxes, "x")
    aws = drive.folder("d_aws", ".aws")
    drive.file("f_aws", "helper.py", aws, "x")
    colab = drive.folder("d_colab", "Colab Notebooks")
    drive.file("f_nb", "Churn Model.ipynb", colab, notebook("def churn_rate(rows): return sum(rows) / len(rows)"),
               mime=COLAB_MIME)
    drive.file("f_nb2", "Title Without Extension", colab, notebook("x"), mime=COLAB_MIME)
    drive.file("f_nb3", "uploaded.ipynb", colab, notebook("y"), mime="application/octet-stream")
    drive.file("f_trash", "binned.py", colab, "x", trashed=True)
    drive.file("f_shared", "someone_elses.py", ROOT_ID, "x", ownedByMe=False)
    return drive


def test_discovery_records_the_same_things_a_local_scan_would(homes):
    inventory, result = inventory_of(_realistic_drive())
    assert paths_of(inventory) == {
        "Sorting Project/main.py", "Sorting Project/README.md", "Sorting Project/requirements.txt",
        "Sorting Project/results.csv", "Colab Notebooks/Churn Model.ipynb",
        "Colab Notebooks/Title Without Extension.ipynb", "Colab Notebooks/uploaded.ipynb",
    }
    skips = skips_of(inventory)
    for expected in [("[private]", "private_folder"), (".aws", "credential_location"),
                     ("Sorting Project/shortcut.py", "link"), ("Sorting Project/[redacted]", "sensitive_name"),
                     ("Sorting Project/~$report.docx", "office_lock_file"), ("Sorting Project/.git", "hidden"),
                     ("Sorting Project/build", "build_dir"), ("Sorting Project/my_env", "virtual_env")]:
        assert expected in skips
    text = inventory.to_json()
    for never in ("Taxes", "return_2023", "server.pem", "token.json", "binned", "someone_elses", "synthetic-"):
        assert never not in text
    assert inventory.report.mode is ScanMode.GOOGLE_DRIVE
    summary = inventory.summary
    assert summary.cloud_only_files_skipped == 1  # the Google Doc
    assert summary.excluded_image_files == 1 and summary.unrecognized_files >= 1
    project = next(d for d in inventory.directories if d.relative_path == "Sorting Project")
    assert ".git" in project.marker_dirs and "README.md" in project.marker_files
    assert {f.category.value for f in inventory.files if f.parent_dir == "Colab Notebooks"} == {"notebook"}
    assert len(result.refs) == len(inventory.files)


def test_nothing_is_downloaded_or_created_by_discovery(homes):
    drive = _realistic_drive()
    inventory, _ = inventory_of(drive)
    assert not drive.downloads
    assert not paths.drive_cache_dir().exists()
    assert all(not Path(f.absolute_path).exists() for f in inventory.files)


def test_notebooks_are_found_however_drive_labels_them(homes):
    drive = FakeDrive()
    drive.file("a", "From Colab", ROOT_ID, "{}", mime=COLAB_MIME)
    drive.file("b", "from_jupyter.ipynb", ROOT_ID, "{}", mime="application/x-ipynb+json")
    drive.file("c", "uploaded.IPYNB", ROOT_ID, "{}", mime="application/octet-stream")
    drive.file("d", "plain.py", ROOT_ID, "{}")
    inventory, _ = inventory_of(drive, discovery.DriveOptions(colab_only=True))
    assert paths_of(inventory) == {"From Colab.ipynb", "from_jupyter.ipynb", "uploaded.IPYNB"}


def test_colab_only_records_just_the_notebooks_and_the_way_to_them(homes):
    inventory, _ = inventory_of(_realistic_drive(), discovery.DriveOptions(colab_only=True))
    assert {f.parent_dir for f in inventory.files} == {"Colab Notebooks"}
    assert {d.relative_path for d in inventory.directories} == {"", "Colab Notebooks"}
    assert not inventory.skipped, "folders without notebooks should not even be mentioned"
    assert "Sorting Project" not in inventory.to_json()


def test_a_targeted_scan_lists_only_that_folder_and_never_a_private_one_inside_it(homes):
    drive = FakeDrive()
    wanted = drive.folder("d_w", "Coursework")
    drive.file("f_w", "kept.py", wanted, "x")
    private = drive.folder("d_p", "Medical", wanted)
    drive.file("f_p", "hidden.py", private, "x")
    other = drive.folder("d_o", "Elsewhere")
    drive.file("f_o", "other.py", other, "x")
    inventory, _ = inventory_of(drive, discovery.DriveOptions(folders=("Coursework",)))
    assert paths_of(inventory) == {"kept.py"}
    listed = " ".join(urllib.parse.unquote_plus(c) for c in drive.calls)
    assert "'d_p' in parents" not in listed and "'d_o' in parents" not in listed
    assert ("[private]", "private_folder") in skips_of(inventory)


def test_a_private_folder_cannot_be_chosen_directly(homes):
    drive = FakeDrive()
    drive.folder("d_p", "Passwords")
    with pytest.raises(DriveError):
        inventory_of(drive, discovery.DriveOptions(folders=("Passwords",)))


def test_your_own_private_folder_names_apply(homes):
    drive = FakeDrive()
    private = drive.folder("d", "Job Applications")
    drive.file("f", "cover.py", private, "x")
    names = frozenset({"job applications"})
    inventory, _ = inventory_of(drive, discovery.DriveOptions(private_names=names))
    assert not inventory.files and "Job Applications" not in inventory.to_json()


def test_folder_choices_must_be_clear_and_real(homes):
    drive = FakeDrive()
    drive.folder("d1", "Twin")
    drive.folder("d2", "Twin")
    drive.file("f1", "plain.py", ROOT_ID, "x")
    for wanted, message in (("Twin", "Several"), ("No such folder", "No top-level"), ("f1", "not a folder")):
        with pytest.raises(DriveError, match=message):
            inventory_of(drive, discovery.DriveOptions(folders=(wanted,)))


def test_someone_elses_folder_needs_asking_for(homes):
    drive = FakeDrive()
    shared = drive.folder("d_shared", "Theirs", "elsewhere", ownedByMe=False)
    drive.file("f", "code.py", shared, "x", ownedByMe=False)
    with pytest.raises(DriveError, match="include-shared"):
        inventory_of(drive, discovery.DriveOptions(folders=("d_shared",)))
    inventory, _ = inventory_of(drive, discovery.DriveOptions(folders=("d_shared",), include_shared=True))
    assert paths_of(inventory) == {"code.py"}


def test_items_inside_a_shared_folder_you_dont_own_are_named_nowhere(homes):
    drive = FakeDrive()
    mine = drive.folder("d", "Group Project")
    drive.file("f1", "mine.py", mine, "x")
    drive.file("f2", "theirs_secret.py", mine, "x", ownedByMe=False)
    inventory, _ = inventory_of(drive, discovery.DriveOptions(folders=("Group Project",)))
    assert paths_of(inventory) == {"mine.py"}
    assert "theirs_secret" not in inventory.to_json()
    assert ("[private]", SkipReason.NOT_OWNED.value) in skips_of(inventory)


def test_same_named_items_stay_separate_and_keep_stable_paths(homes):
    drive = FakeDrive()
    folder = drive.folder("d", "Project")
    drive.file("f1", "main.py", folder, "one")
    drive.file("f2", "main.py", folder, "two!")
    drive.file("f3", "MAIN.py", folder, "three")
    first, _ = inventory_of(drive)
    second, _ = inventory_of(drive)
    assert len({f.relative_path.casefold() for f in first.files}) == 3
    assert paths_of(first) == paths_of(second)
    assert {f.id for f in first.files} == {f.id for f in second.files}
    assert len({f.absolute_path for f in first.files}) == 3


def test_a_hostile_name_stays_inside_the_cache_and_the_project(homes):
    drive = FakeDrive()
    drive.file("f1", "../../../outside.py", ROOT_ID, "x")
    drive.file("f2", "..\\..\\outside2.py", ROOT_ID, "x")
    folder = drive.folder("d", "..")
    drive.file("f3", "inner.py", folder, "x")
    inventory, _ = inventory_of(drive)
    cache = paths.drive_cache_dir().resolve()
    for record in inventory.files:
        assert ".." not in record.relative_path.split("/")
        assert Path(record.absolute_path).resolve().is_relative_to(cache)


def test_my_files_in_a_folder_i_cant_see_stay_together(homes):
    drive = FakeDrive()
    drive.file("f1", "a.py", "hiddenParent", "x")
    drive.file("f2", "b.py", "hiddenParent", "x")
    drive.file("f3", "lone.py", "anotherParent", "x")
    inventory, _ = inventory_of(drive)
    parents = {f.name: f.parent_dir for f in inventory.files}
    assert parents["a.py"] == parents["b.py"] != parents["lone.py"]
    assert "hiddenParent" not in inventory.to_json()
    root = next(d for d in inventory.directories if d.relative_path == "")
    assert root.subdirectory_count == 2
    assert sum(d.relative_path == "" for d in inventory.directories) == 1


def test_a_file_without_a_size_is_reported_rather_than_lost(homes):
    drive = FakeDrive()
    drive.file("f1", "Sizeless", ROOT_ID, None, mime=COLAB_MIME)
    inventory, _ = inventory_of(drive)
    assert not inventory.files
    entry = inventory.skipped[0]
    assert (entry.reason, entry.detail) == (SkipReason.CLOUD_ONLY, "no_size")


def test_limits_stop_the_scan_and_say_so(homes):
    drive = FakeDrive()
    for index in range(5):
        drive.file(f"f{index}", f"file{index}.py", ROOT_ID, "x")
    inventory, result = inventory_of(drive, discovery.DriveOptions(limits=ResourceLimits(max_files=2)))
    assert len(inventory.files) == 2
    assert result.report.stop_reason == "max_files" and not result.report.complete

    folder = "d0"
    drive.folder(folder, "level0")
    for index in range(1, 4):
        drive.folder(f"d{index}", f"level{index}", f"d{index - 1}")
    inventory, result = inventory_of(drive, discovery.DriveOptions(limits=ResourceLimits(max_depth=2)))
    assert "max_depth" in result.report.limits_reached and not result.report.complete


def test_a_folder_loop_cannot_hang_the_scan(homes):
    drive = FakeDrive()
    drive.folder("a", "A", "b")
    drive.folder("b", "B", "a")
    drive.folder("c", "C")
    drive.folder("d", "D", "c")
    drive.items["c"]["parents"] = ["d"]
    inventory, _ = inventory_of(drive)  # finishes, and the loop isn't reachable from My Drive
    assert not inventory.files


# --- the command line and saved inventories -------------------------------------------------------------------


@pytest.fixture
def cli(homes, monkeypatch, capsys):
    drive = _realistic_drive()
    monkeypatch.setattr(dcli, "make_client", lambda: client_for(drive))

    def run(*args, answer=None):
        def asked(prompt):
            assert answer is not None, f"unexpected question: {prompt}"
            return answer
        code = dcli.main(list(args), input_fn=asked)
        output = capsys.readouterr()
        return code, output.out + output.err

    run.drive = drive
    return run


def _drive_key() -> str:
    return next(i.id for i in data.stored_inventories() if i.id.startswith("drive-"))


def test_scanning_all_of_my_drive_asks_first(cli):
    code, out = cli("scan", "--compare-with", "none", answer="no")
    assert code == 1 and "Nothing was scanned" in out
    assert not data.stored_inventories()
    assert all("in owners" not in urllib.parse.unquote_plus(c) for c in cli.drive.calls)
    code, out = cli("scan", "--compare-with", "none", answer="yes")
    assert code == 0 and "Next: python -m portfolioforge.batch" in out


def test_a_plan_lists_nothing_and_a_colab_scan_needs_no_question(cli):
    code, out = cli("scan", "--plan-only")
    assert code == 0 and "nothing was scanned" in out and not data.stored_inventories()
    code, _ = cli("scan", "--colab-only", "--compare-with", "none")
    assert code == 0
    assert paths_of(load_inventory(paths.inventories_dir() / _drive_key())) == {
        "Colab Notebooks/Churn Model.ipynb", "Colab Notebooks/Title Without Extension.ipynb", "Colab Notebooks/uploaded.ipynb"}


def test_a_failed_listing_leaves_no_inventory(cli):
    cli.drive.fail_after = 2
    code, out = cli("scan", "--yes", "--compare-with", "none")
    assert code == 1 and "HTTP 500" in out
    assert not data.stored_inventories()


def test_redacted_output_names_nothing(cli):
    code, out = cli("scan", "--folder", "Sorting Project", "--redact", "--compare-with", "none")
    assert code == 0 and "Sorting Project" not in out and "main.py" not in out


def test_without_permission_the_scan_says_how_to_grant_it(homes, capsys):
    assert dcli.main(["scan", "--colab-only"]) == 1
    assert "connect" in capsys.readouterr().err


def test_status_never_shows_a_token(homes, capsys):
    auth._write_private(auth.token_file(METADATA_SCOPE), {"scope": METADATA_SCOPE, "access_token": "synthetic-visible"})
    assert dcli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "List metadata: allowed" in out and "Download approved files: not allowed" in out
    assert "synthetic-visible" not in out


def _local_inventory(tmp_path, fake_system, files, key=LOCAL_KEY):
    root = make_tree(tmp_path / "Local", files)
    with JsonlSink(paths.inventories_dir() / key) as sink:
        run_scan(plan_targeted([root], system=fake_system), sink=sink)


def test_a_drive_scan_never_becomes_the_latest_local_inventory(cli, tmp_path, fake_system):
    _local_inventory(tmp_path, fake_system, {"Work/app.py": "x\n"})
    cli("scan", "--colab-only", "--compare-with", "none")
    assert gcli.resolve_inventory("latest")[0] == LOCAL_KEY
    assert gcli.resolve_inventory("latest-drive")[0] == _drive_key()
    assert data._resolve(data.stored_inventories(), "latest").id == LOCAL_KEY


def test_latest_drive_is_an_error_when_there_is_none(homes):
    with pytest.raises(gcli.UsageError):
        gcli.resolve_inventory("latest-drive")


def test_data_tools_handle_a_drive_inventory_and_its_downloads(cli, capsys):
    cli("scan", "--colab-only", "--compare-with", "none")
    key = _drive_key()
    cached = paths.drive_cache_dir() / key / "abc" / "copy.ipynb"
    cached.parent.mkdir(parents=True)
    cached.write_text("{}")
    assert data.main(["stats", "latest-drive"]) == 0
    assert data.main(["purge", key, "--yes"]) == 0
    assert not (paths.inventories_dir() / key).exists()
    assert not (paths.drive_cache_dir() / key).exists()
    assert "Drive downloads" in (data.main(["where"]) == 0 and capsys.readouterr().out)


# --- duplicates between Drive and this computer ---------------------------------------------------------------


def _compare(drive: FakeDrive, tmp_path, fake_system, local_files, config=None):
    _local_inventory(tmp_path, fake_system, local_files)
    key, local = gcli.resolve_inventory("latest")
    cloud, _ = inventory_of(drive)
    return duplicates.find_matches(cloud, propose_groups(cloud), local, propose_groups(local), config)


def test_a_project_copied_to_drive_is_a_possible_duplicate(homes, tmp_path, fake_system):
    files = {"Sorter/main.py": "print('sort')", "Sorter/utils.py": "x = 1", "Sorter/README.md": "# Sorter"}
    drive = FakeDrive()
    folder = drive.folder("d", "Sorter")
    for index, (path, content) in enumerate(files.items()):
        drive.file(f"f{index}", path.split("/")[1], folder, content)
    matches = _compare(drive, tmp_path, fake_system, files)
    assert [m.kind for m in matches] == ["possible_duplicate"]


def test_a_colab_rewrite_of_a_local_notebook_is_a_possible_variant(homes, tmp_path, fake_system):
    drive = FakeDrive()
    colab = drive.folder("d", "Colab Notebooks")
    drive.file("f", "churn_model.ipynb", colab, notebook("rewritten by colab, so a different size"), mime=COLAB_MIME)
    matches = _compare(drive, tmp_path, fake_system, {"Local Work/churn_model.ipynb": notebook()})
    assert [m.kind for m in matches] == ["possible_variant"]


def test_common_names_and_unrelated_projects_are_not_matches(homes, tmp_path, fake_system):
    drive = FakeDrive()
    colab = drive.folder("d", "Colab Notebooks")
    drive.file("f1", "Untitled0.ipynb", colab, notebook("a"), mime=COLAB_MIME)
    drive.file("f2", "unrelated.py", colab, "y = 2\n")
    matches = _compare(drive, tmp_path, fake_system, {"Other/Untitled0.ipynb": notebook("bb"), "Other/different.py": "z\n"})
    assert matches == []


def test_duplicates_are_reported_both_ways_and_nothing_is_removed(cli, tmp_path, fake_system):
    _local_inventory(tmp_path, fake_system, LOCAL_COPY)
    before = len(load_inventory(paths.inventories_dir() / LOCAL_KEY).files)
    code, out = cli("scan", "--yes")
    assert code == 0 and "Possible duplicates with local inventory" in out and "possible_duplicate" in out
    drive_key = _drive_key()
    drive_concerns, local_concerns = duplicates.load_concerns(drive_key), duplicates.load_concerns(LOCAL_KEY)
    assert any("local project" in c for cs in drive_concerns.values() for c in cs)
    assert any("Google Drive project" in c for cs in local_concerns.values() for c in cs)
    assert len(load_inventory(paths.inventories_dir() / LOCAL_KEY).files) == before
    assert not (paths.data_dir() / "groupings" / LOCAL_KEY).exists(), "comparing must not write to the local inventory's records"
    text = json.dumps({**drive_concerns, **local_concerns})
    assert "Sorting Project" not in text, "concerns are shown under --redact, so they hold only group IDs"


def test_a_missing_or_broken_comparison_is_not_an_error(homes):
    assert duplicates.load_concerns("nothing-here") == {}
    path = duplicates._concerns_path("broken")
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    assert duplicates.load_concerns("broken") == {}


# --- into the batch: the existing gates still decide everything -----------------------------------------------


@pytest.fixture
def pipeline(cli, homes, monkeypatch, capsys):
    """A scanned Drive, ready for the normal batch. Downloads go through the same pretend Google."""
    monkeypatch.setattr(hydrate, "make_client", lambda: content_client_for(cli.drive))
    provider_log: list[str] = []
    monkeypatch.setattr(ucli, "make_provider", lambda name, model: FakeProvider(model, provider_log))
    cli("scan", "--yes", "--compare-with", "none")

    def batch(*args, answer=None):
        def asked(prompt):
            return answer if answer is not None else ""
        code = bcli.main(["--inventory", "latest-drive", *args], input_fn=asked)
        output = capsys.readouterr()
        return code, output.out + output.err

    batch.provider_log = provider_log
    batch.drive = cli.drive
    return batch


def _project_group() -> str:
    from portfolioforge.batch import workflow as bw
    ctx = bw.load_context("latest-drive")
    return next(g.id for g in ctx.run.groups if g.working_title.text == "Sorting Project" or "Sorting" in g.working_title.text)


def test_the_batch_finds_drive_projects_without_downloading_anything(pipeline):
    code, out = pipeline("run", "--provider", "anthropic")
    assert code == 0 and "Nothing was sent" in out
    assert not pipeline.drive.downloads
    assert not paths.drive_cache_dir().exists()
    assert not pipeline.provider_log
    code, review = pipeline("review")
    assert "needs_read_approval" in review or "allow reading" in review


def test_downloading_needs_its_own_permission_before_any_question(pipeline, monkeypatch):
    pipeline("run")
    group = _project_group()

    def not_granted():
        raise DriveAuthError("PortfolioForge does not have permission to download approved files.")

    monkeypatch.setattr(hydrate, "make_client", not_granted)

    def never_asked(prompt):
        raise AssertionError("asked before checking the permission")

    assert bcli.main(["--inventory", "latest-drive", "approve-read", group, "--acknowledge-review"], input_fn=never_asked) == 1
    assert not pipeline.drive.downloads


def test_declining_downloads_nothing(pipeline):
    pipeline("run")
    group = _project_group()
    code, out = pipeline("approve-read", group, "--acknowledge-review", answer="no")
    assert code == 1 and "Nothing was read" in out
    assert "downloaded read-only from Google Drive" in out
    assert not pipeline.drive.downloads and not paths.drive_cache_dir().exists()


def test_approval_downloads_only_the_approved_files_and_nothing_goes_to_the_provider(pipeline):
    pipeline("run", "--provider", "anthropic")
    group = _project_group()
    code, out = pipeline("approve-read", group, "--acknowledge-review", answer="download 1")
    assert code == 0, out
    fetched = {urllib.parse.urlsplit(c).path.rsplit("/", 1)[-1] for c in pipeline.drive.downloads}
    assert fetched == {"f_main", "f_readme", "f_req"}, "only files approved for reading may be fetched"
    assert not pipeline.provider_log, "reading a file must not send it anywhere"
    cached = [p for p in paths.drive_cache_dir().rglob("*") if p.is_file()]
    assert len(cached) == 3 and not [p for p in cached if p.suffix == ".part"]
    _, status = pipeline("status")
    assert "needs_disclosure_approval" in status


def test_the_disclosure_gate_still_decides_what_reaches_the_provider(pipeline):
    pipeline("run", "--provider", "anthropic")
    group = _project_group()
    pipeline("approve-read", group, "--acknowledge-review", answer="download 1")
    code, out = pipeline("send", group, answer="no")
    assert code == 1 and "Nothing was sent" in out and not pipeline.provider_log
    code, out = pipeline("send", group, answer="send 1 to anthropic")
    assert code == 0, out
    assert len(pipeline.provider_log) == 1
    _, status = pipeline("status")
    assert "needs_name" in status


def test_a_drive_project_reaches_publishing_only_through_every_gate(pipeline, monkeypatch):
    github = FakeGitHub()
    monkeypatch.setattr(pcli, "make_runner", github)
    pipeline("run", "--provider", "anthropic")
    group = _project_group()
    pipeline("approve-read", group, "--acknowledge-review", answer="download 1")
    pipeline("send", group, answer="send 1 to anthropic")
    code, out = pipeline("accept-names", group, answer="accept 1")
    assert code == 0, out
    code, out = pipeline("stage", group, "--ownership", "mine", "--license", "none", answer="stage 1")
    assert code == 0, out
    assert not github.created, "staging must not publish"
    _, status = pipeline("status")
    assert "needs_publish_approval" in status, status
    from portfolioforge.batch import workflow as bw
    name = bw.latest_stage(_drive_key(), group).repo_name
    code, out = pipeline("publish", group, answer="no")
    assert not github.created, "declining must not publish"
    code, out = pipeline("publish", group, answer=f"publish {name}")
    assert code == 0, out
    assert [c.visibility for c in github.created] == ["PRIVATE"]
    staged = {f.path for f in bw.latest_stage(_drive_key(), group).files}
    assert "main.py" in staged and "results.csv" not in staged
    assert all("drive-cache" not in p for p in staged)


def test_a_colab_notebook_goes_through_the_batch_on_its_own(cli, pipeline):
    pipeline("run", "--provider", "anthropic")
    from portfolioforge.batch import workflow as bw
    ctx = bw.load_context("latest-drive")
    group = next(g.id for g in ctx.run.groups if g.working_title.text == "Churn Model")
    code, out = pipeline("approve-read", group, "--acknowledge-review", answer="download 1")
    assert code == 0, out
    assert [urllib.parse.urlsplit(c).path.rsplit("/", 1)[-1] for c in pipeline.drive.downloads] == ["f_nb"]
    code, out = pipeline("send", group, answer="send 1 to anthropic")
    assert code == 0, out
    assert len(pipeline.provider_log) == 1


def test_a_file_changed_since_discovery_is_not_kept_or_approved(pipeline):
    pipeline("run")
    group = _project_group()
    pipeline.drive.contents["f_main"] = b"print('changed in Drive after the scan')\n"
    code, out = pipeline("approve-read", group, "--acknowledge-review", answer="download 1")
    assert code == 1 and "changed after it was discovered" in out
    from portfolioforge.understanding.store import latest_file_approval
    assert latest_file_approval(_drive_key(), group) is None
    assert not [p for p in paths.drive_cache_dir().rglob("*") if p.is_file() and "main" in p.name]


def test_a_failed_download_can_be_approved_again_without_refetching(pipeline):
    pipeline("run")
    group = _project_group()
    good = pipeline.drive.contents["f_req"]
    pipeline.drive.contents["f_req"] = b"different size\n"
    assert pipeline("approve-read", group, "--acknowledge-review", answer="download 1")[0] == 1
    first = len(pipeline.drive.downloads)
    pipeline.drive.contents["f_req"] = good
    code, out = pipeline("approve-read", group, "--acknowledge-review", answer="download 1")
    assert code == 0, out
    assert len(set(pipeline.drive.downloads)) == 3, "every approved file arrived"
    assert len(pipeline.drive.downloads) < first + 3, "files already cached are not fetched again"


def test_a_tampered_inventory_cannot_write_outside_the_cache(pipeline, tmp_path):
    key = _drive_key()
    inventory = load_inventory(paths.inventories_dir() / key)
    record = next(f for f in inventory.files if f.name == "main.py")
    from dataclasses import replace
    outside = tmp_path / "outside.py"
    tampered = replace(inventory, files=tuple(replace(f, absolute_path=str(outside)) if f.id == record.id else f
                                              for f in inventory.files))
    with pytest.raises(DriveError, match="outside the download cache"):
        hydrate.download_approved(tampered, key, [record.id], content_client_for(pipeline.drive))
    assert not outside.exists()


def test_a_sensitive_name_is_never_downloaded(pipeline):
    key = _drive_key()
    inventory = load_inventory(paths.inventories_dir() / key)
    record = next(f for f in inventory.files if f.name == "main.py")
    from dataclasses import replace
    tampered = replace(inventory, files=tuple(replace(f, name="token.json") if f.id == record.id else f
                                              for f in inventory.files))
    with pytest.raises(DriveError, match="never downloaded"):
        hydrate.download_approved(tampered, key, [record.id], content_client_for(pipeline.drive))
    assert not pipeline.drive.downloads


def test_a_local_inventory_needs_no_google_permission_and_downloads_nothing(homes, tmp_path, fake_system):
    _local_inventory(tmp_path, fake_system, {"Work/app.py": "x\n"})
    _, inventory = gcli.resolve_inventory("latest")
    hydrate.require_download_permission(inventory)
    assert hydrate.download_approved(inventory, LOCAL_KEY, [inventory.files[0].id]) == 0


def test_the_batch_shows_a_cross_source_duplicate_in_its_review_and_at_approval(cli, pipeline, tmp_path, fake_system):
    _local_inventory(tmp_path, fake_system, LOCAL_COPY)
    assert cli("duplicates")[0] == 0
    pipeline("run")
    group = _project_group()
    _, review = pipeline("review")
    assert "local project" in review
    _, approval = pipeline("approve-read", group, "--acknowledge-review", answer="no")
    assert "local project" in approval
    assert bcli.main(["--inventory", LOCAL_KEY, "status"]) == 0


def test_nothing_in_the_drive_package_runs_code_or_publishes():
    package = Path(auth.__file__).parent
    source = "".join(p.read_text(encoding="utf-8") for p in package.glob("*.py"))
    imports = "\n".join(re.findall(r"(?m)^\s*(?:from|import) .*$", source))
    for forbidden in ("subprocess", "publishing", "anthropic", "validation"):
        assert forbidden not in imports, forbidden
    for call in ("os.system", "exec(", "eval("):
        assert call not in source, call
    for verb in ('"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in source, verb
    assert '"POST"' not in (package / "client.py").read_text(encoding="utf-8"), "the Drive client only ever issues GET"
    assert source.count('"POST"') == 2, "only the token and revocation requests post, and neither goes to Drive"


def test_each_notebook_in_the_colab_folder_is_its_own_candidate(homes):
    drive = FakeDrive()
    colab = drive.folder("d", "Colab Notebooks")
    for index, name in enumerate(("churn_model", "image_classifier", "sentiment analysis")):
        drive.file(f"f{index}", f"{name}.ipynb", colab, notebook(name), mime=COLAB_MIME,
                   modified=f"202{index}-03-01T10:00:00.000Z")
    inventory, _ = inventory_of(drive)
    run = propose_groups(inventory)
    assert sorted(g.working_title.text for g in run.groups) == ["churn_model", "image_classifier", "sentiment analysis"]
    assert all(len(g.members) == 1 and g.kind.value == "project" for g in run.groups)
    assert not run.unassigned


def test_related_notebooks_in_the_colab_folder_still_group_together(homes):
    drive = FakeDrive()
    colab = drive.folder("d", "Colab Notebooks")
    drive.file("f1", "churn_model.ipynb", colab, notebook("a"), mime=COLAB_MIME)
    drive.file("f2", "Copy of churn_model.ipynb", colab, notebook("b"), mime=COLAB_MIME)
    inventory, _ = inventory_of(drive)
    run = propose_groups(inventory)
    assert [len(g.members) for g in run.groups] == [2]


def test_other_folders_keep_their_loose_files_loose(homes):
    drive = FakeDrive()
    folder = drive.folder("d", "Downloads")
    drive.file("f1", "churn.ipynb", folder, notebook("a"), mime=COLAB_MIME, modified="2020-03-01T10:00:00.000Z")
    inventory, _ = inventory_of(drive)
    assert [u.reason for u in propose_groups(inventory).unassigned] == ["no grouping evidence (loose file)"]


def test_a_loose_local_file_is_still_mentioned_on_the_drive_side(homes, tmp_path, fake_system):
    drive = FakeDrive()
    colab = drive.folder("d", "Colab Notebooks")
    drive.file("f", "churn_model.ipynb", colab, notebook("rewritten"), mime=COLAB_MIME)
    _local_inventory(tmp_path, fake_system, {"Downloads/churn_model.ipynb": notebook()})
    key, local = gcli.resolve_inventory("latest")
    cloud, _ = inventory_of(drive)
    matches = duplicates.find_matches(cloud, propose_groups(cloud), local, propose_groups(local))
    assert [m.local_group.startswith("file:") for m in matches] == [True]
    duplicates.save_matches("drive-test", key, matches)
    assert any("local file" in c for cs in duplicates.load_concerns("drive-test").values() for c in cs)
    assert duplicates.load_concerns(key) == {}
