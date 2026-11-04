"""Tests for the recap plugin. Run with the hub's backend on the path:

    pip install pytest respx httpx fastapi
    DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest

The hub is never started and no real service is called: every service is simulated with respx.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security import get_current_user
from app.plugins.manifest import validate_manifest
from fh_plugin_recap import build, collectors, plugin as mod, store, story, views
from fh_plugin_recap.plugin import RecapPlugin

from helpers import ABS, ANNA, ANNA_ID, BEN_ID, GLOBAL_ENV, JF, SECRETS, install

ROOT = Path(__file__).resolve().parents[1]
MONTH = "2026-10"
TZ = collectors.local_tz("Europe/Berlin")


@pytest.fixture
def people(monkeypatch):
    """Server settings plus two people: 1 (Anna, everything connected) and 2 (nothing connected)."""
    for key, value in GLOBAL_ENV.items():
        monkeypatch.setenv(key, value)
    personal = {1: dict(ANNA), 2: {}}
    monkeypatch.setattr(mod, "_personal", lambda uid, key: personal.get(uid, {}).get(key, ""))
    monkeypatch.setattr(mod, "last_month", lambda: MONTH)
    return personal


@pytest.fixture
def services():
    with respx.mock(using="httpx", assert_all_called=False) as http:
        install(http)
        yield http


def run(coro):
    return asyncio.run(coro)


def build_for(uid=1, month=MONTH, **kw):
    return run(build.build_month(uid, month, mod.load_config(uid), TZ, **kw))


# --- manifest and loading ---

def test_manifest_is_valid_and_matches_the_code():
    manifest = validate_manifest(json.loads((ROOT / "familienhub-plugin.json").read_text()))
    plugin = RecapPlugin()
    assert plugin.id == manifest["id"] == "recap" and manifest["version"] == plugin.version
    assert {s.key for s in plugin.settings} == {s["key"] for s in manifest["settings"]} == set(mod.GLOBAL_KEYS)
    assert {s.key for s in plugin.personal_settings} == {s["key"] for s in manifest["personal_settings"]} == set(mod.PERSONAL_KEYS)
    assert all(not s["required"] for s in manifest["settings"] + manifest["personal_settings"])
    assert all(s["key"].startswith("RECAP_") for s in manifest["settings"] + manifest["personal_settings"])
    secret = {s["key"] for s in manifest["settings"] + manifest["personal_settings"] if s["type"] == "password"}
    assert {"RECAP_JELLYFIN_TOKEN", "RECAP_LISTENBRAINZ_TOKEN", "RECAP_ABS_TOKEN", "RECAP_IMMICH_KEY",
            "RECAP_NAVIDROME_PASSWORD", "RECAP_NEXTCLOUD_APP_PASSWORD", "RECAP_ROMM_TOKEN"} == secret
    assert [p["id"] for p in manifest["pages"]] == ["recaps"] and manifest["pages"][0]["title"] == {"de": "Rückblicke", "en": "Recaps"}
    assert manifest["pages"][0]["icon"] == "celebration"
    assert plugin.schedule_minutes == 60 and plugin.has_personal_test
    assert {"api.listenbrainz.org", "coverartarchive.org"} <= set(manifest["hosts"])
    assert 'recap = "fh_plugin_recap.plugin:RecapPlugin"' in (ROOT / "pyproject.toml").read_text()
    for r in plugin.routers():
        assert all(route.path.startswith("/api/plugins/recap/") for route in r.routes)


def test_store_entry_is_consistent():
    entry = json.loads((ROOT / "store-entry.json").read_text())
    manifest = json.loads((ROOT / "familienhub-plugin.json").read_text())
    assert entry["id"] == manifest["id"] and entry["version"] == manifest["version"]
    assert entry["path"] == "plugins/recap" and entry["sha"] == "TBD"


def test_active_without_any_setting():
    assert RecapPlugin().is_configured() is True  # works per person


def test_loads_like_an_installed_plugin(tmp_path):
    """The hub's own loader (same checks as after a real install), in a fresh interpreter."""
    backend = [p for p in sys.path if (Path(p) / "app" / "plugins" / "installer.py").is_file()][0]
    script = textwrap.dedent(f"""
        import json
        from pathlib import Path
        from fastapi import FastAPI
        from app.plugins import installer
        from app.plugins.manifest import validate_manifest
        root = Path({str(ROOT)!r})
        manifest = validate_manifest(json.loads((root / "familienhub-plugin.json").read_text()))
        app = FastAPI()
        plugin = installer.load_plugin(app, manifest, root)
        paths = sorted(p for p in app.openapi()["paths"] if p.startswith("/api/plugins/"))
        assert paths == ["/api/plugins/recap/build", "/api/plugins/recap/markdown/{{month}}",
                         "/api/plugins/recap/months"], paths
        assert plugin.id == "recap" and plugin.has_personal_test and plugin.pages[0].id == "recaps"
        print("LOADED")
    """)
    env = {**os.environ, "PYTHONPATH": backend, "DATA_DIR": str(tmp_path / "data"), "SECRET_KEY": "t" * 40,
           "DATABASE_URL": f"sqlite:///{tmp_path}/t.db", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONHOME", None)
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert "LOADED" in result.stdout, result.stdout[-1500:] + result.stderr[-2500:]


# --- building a month ---

def test_full_build_writes_story_snapshot_and_assets(people, services):
    data = build_for()
    assert data["errors"] == {}
    folder = store.month_dir(1, MONTH)
    story_html = (folder / "index.html").read_text()
    assert store.read_snapshot(1, MONTH)["month"] == MONTH and store.has_story(1, MONTH)
    assert data["jellyfin"]["series"][0]["name"] == "Great Show" and data["jellyfin"]["episodes"] == 1
    assert data["jellyfin"]["movies"][0]["name"] == "Big Film"
    assert data["music"]["plays"] == 30 and data["audiobooks"]["books"][0]["title"] == "Long Book"
    assert data["photos"]["total"] == 5 and data["podcasts"]["podcasts"][0]["title"] == "Talk Show"
    assert len(data["soundtrack"]) >= 3
    assert data["totals"]["media_hours"] > 0
    files = sorted(p.name for p in (folder / "img").iterdir())
    assert any(f.startswith("jf-") for f in files) and any(f.startswith("clip-") and f.endswith(".mp3") for f in files)
    assert any(f.startswith("ph-") for f in files) and any(f.startswith("mu-") for f in files)
    # the story only uses its own files and links back to the index
    assert "Great Show" in story_html and 'href="../"' in story_html and "data-clip=" in story_html
    assert not re.findall(r'(?:src|href)="https?://', story_html) and "url('http" not in story_html
    assert not list(store.user_dir(1).glob(".build-*"))  # temporary folder is gone


def test_jellyfin_plays_are_filtered_by_the_persons_user_id(people, services):
    build_for()
    sent = [json.loads(c.request.content) for c in services.calls
            if c.request.url.path == "/user_usage_stats/submit_custom_query"]
    assert len(sent) == 1
    query = sent[0]["CustomQueryString"]
    assert f"'{ANNA_ID}'" in query and BEN_ID not in query and "UserId" in query
    lookups = [c.request.url.path for c in services.calls if "/Items" in c.request.url.path and c.request.url.host == "jf.example.org"]
    assert any(p.startswith(f"/Users/{ANNA_ID}/Items") for p in lookups) and not any(BEN_ID in p for p in lookups)


def test_unknown_jellyfin_user_is_recorded_as_error(people, services):
    people[1]["RECAP_JELLYFIN_USER"] = "nobody"
    data = build_for()
    assert "jellyfin" in data["errors"] and data["jellyfin"] == {}
    assert "Great Show" not in (store.month_dir(1, MONTH) / "index.html").read_text()  # its slides are omitted


def test_failing_sources_are_skipped_and_errors_hold_no_secrets(people):
    with respx.mock(using="httpx", assert_all_called=False) as http:
        install(http, fail=("photos", "podcasts", "audiobooks", "soundtrack"))
        data = build_for()
    assert set(data["errors"]) == {"photos", "podcasts", "audiobooks"}
    assert data["music"]["plays"] == 30 and data["soundtrack"] == []
    blob = json.dumps(data) + (store.month_dir(1, MONTH) / "index.html").read_text()
    assert not any(s in blob for s in SECRETS) and "example.org" not in json.dumps(data["errors"])
    assert 'data-clip' not in blob  # no sound without clips


def test_all_sources_down_fails_and_leaves_no_snapshot(people):
    with respx.mock(using="httpx", assert_all_called=False) as http:
        install(http, fail=("jellyfin", "music", "audiobooks", "podcasts", "photos"))
        with pytest.raises(build.BuildFailed):
            build_for()
    assert store.read_snapshot(1, MONTH) is None and not store.month_dir(1, MONTH).exists()


def test_quiet_month_gets_an_empty_snapshot_without_story(people):
    with respx.mock(using="httpx", assert_all_called=False) as http:
        install(http)
        http.post(f"{JF}/user_usage_stats/submit_custom_query").mock(return_value=httpx.Response(200, json={"results": []}))
        people[1] = {"RECAP_JELLYFIN_USER": "Anna"}  # connected and reachable, but nothing was played
        data = build_for()
    assert data["empty"] and store.read_snapshot(1, MONTH)["empty"] and store.list_months(1) == []


def test_rebuild_keeps_design_and_notified_flag(people, services):
    first = build_for()
    snap = store.read_snapshot(1, MONTH)
    snap["notified"] = True
    store.write_snapshot(1, MONTH, snap)
    second = build_for()
    assert second["design"] == first["design"] and second["notified"] is True


# --- design memory ---

def test_months_differ_in_palette_shape_and_variants(people, services):
    designs = []
    for offset, month in enumerate(("2026-07", "2026-08", "2026-09", "2026-10")):
        with respx.mock(using="httpx", assert_all_called=False) as http:
            install(http, month=7 + offset)
            designs.append(build_for(month=month)["design"])
    for a, b in zip(designs, designs[1:]):
        assert a["palette"] != b["palette"] or a["shape"] != b["shape"]
        assert a["shape"] != b["shape"] and a["type"] != b["type"]
        assert sum(a["variants"][k] != b["variants"][k] for k in a["variants"]) >= 6
    assert len({d["palette"] for d in designs[-3:]}) == 3  # no palette twice in three months
    # the history is the person's own: person 2 starts without memory
    snap = store.read_snapshot(1, "2026-10")
    assert [h["month"] for h in store.history(1, "2026-10")] == ["2026-07", "2026-08", "2026-09"]
    assert store.history(2, "2026-10") == [] and snap["design"] == designs[-1]


def test_design_differs_between_people_for_the_same_month():
    data = {"month": MONTH, "seed": "1"}, {"month": MONTH, "seed": "2"}
    seen = {json.dumps(story.choose_design(d, []), sort_keys=True) for d in data}
    assert len(seen) == 2


def test_story_renders_highlights_and_text_pools(people, services):
    data = build_for()
    assert data["highlights"]["finished"] == ["Great Show"]
    html_text = (store.month_dir(1, MONTH) / "index.html").read_text()
    assert "Durchgeschaut" in html_text and 'class="slide s-outro' in html_text


# --- pages and path confinement ---

def page(plugin, uid, path, page_id="recaps"):
    return run(plugin.page(SimpleNamespace(id=uid), page_id, path))


def test_index_and_story_and_assets_are_served(people, services):
    build_for()
    plugin = RecapPlugin()
    ctype, body = page(plugin, 1, "")
    assert ctype.startswith("text/html") and b'href="2026-10/"' in body and "Oktober 2026".encode() in body
    ctype, body = page(plugin, 1, "2026-10/")
    assert ctype.startswith("text/html") and b"Great Show" in body and b"<base" not in body
    ctype, body = page(plugin, 1, "2026-10")  # opened without the trailing slash: a base keeps relative files working
    assert b'<base href="2026-10/">' in body
    assert page(plugin, 1, "2026-10/index.html")[0].startswith("text/html")
    names = {p.name for p in (store.month_dir(1, MONTH) / "img").iterdir()}
    poster = next(n for n in names if n.startswith("jf-"))
    clip = next(n for n in names if n.endswith(".mp3"))
    assert page(plugin, 1, f"2026-10/img/{poster}")[0] == "image/jpeg"
    ctype, body = page(plugin, 1, f"2026-10/img/{clip}")
    assert ctype == "audio/mpeg" and body.startswith(b"ID3")
    assert page(plugin, 1, "2026-10/", page_id="other") is None


def test_empty_index_has_a_friendly_text(people):
    ctype, body = page(RecapPlugin(), 2, "")
    assert b"Noch kein R" in body and b"<a href" not in body


@pytest.mark.parametrize("path", [
    "../1/2026-10/index.html", "2026-10/../2026-09/index.html", "2026-10/img/../index.html", "2026-10/img/", "2026-10/img",
    "2026-10/img/x.jpg/extra", "2026-10/img/.hidden.jpg", "2026-10/img/a.html", "2026-10/img/a.svg", "2026-10/img/a.json",
    "2026-10/img//a.jpg", "2026-10/index.html/x", "2026-13/", "2026-1/", "x/", "/etc/passwd", "2026-10/img/a%2Fb.jpg",
    "2026-10\\index.html", "2026-10/img/..\\a.jpg", "2026-10.json", "2026-10/../2026-10.json", "snapshot", ".", "..", "img/clip-00.mp3",
    "2026-10/img/clip-00.mp3\x00.jpg", "2026-10/img/" + "a" * 90 + ".jpg",
])
def test_paths_outside_the_allowed_shape_are_refused(people, services, path):
    build_for()
    assert page(RecapPlugin(), 1, path) is None


def test_people_only_reach_their_own_folder(people, services):
    build_for()
    plugin = RecapPlugin()
    poster = next(p.name for p in (store.month_dir(1, MONTH) / "img").iterdir() if p.name.startswith("jf-"))
    assert page(plugin, 1, f"2026-10/img/{poster}") is not None
    assert page(plugin, 2, f"2026-10/img/{poster}") is None and page(plugin, 2, "2026-10/") is None
    assert b"2026-10" not in page(plugin, 2, "")[1]


def test_symlinks_cannot_escape(people, services, tmp_path):
    build_for()
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"\xff\xd8\xff" + b"s" * 600)
    (store.month_dir(1, MONTH) / "img" / "leak.jpg").symlink_to(secret)
    other = store.month_dir(2, MONTH) / "img"
    other.mkdir(parents=True)
    (other / "theirs.jpg").write_bytes(b"\xff\xd8\xff" + b"t" * 600)
    (store.month_dir(1, MONTH) / "img" / "cross.jpg").symlink_to(other / "theirs.jpg")
    plugin = RecapPlugin()
    assert page(plugin, 1, "2026-10/img/leak.jpg") is None and page(plugin, 1, "2026-10/img/cross.jpg") is None


def test_resolve_never_returns_a_path_outside_the_users_folder(people, services):
    build_for()
    root = store.user_dir(1).resolve()
    for path in ("2026-10/", "2026-10/index.html"):
        kind, file = store.resolve(1, path)
        assert root in file.resolve().parents


# --- scheduler ---

class FakeDB:
    def __init__(self, ids):
        self.ids = ids

    def query(self, _model):
        return self

    def filter(self, *_):
        return self

    def order_by(self, *_):
        return self

    def all(self):
        return [SimpleNamespace(id=i) for i in self.ids]


@pytest.fixture
def fake_build(monkeypatch):
    """Replaces the real build by a recorder that writes a minimal story."""
    calls = []

    async def fake(uid, month, cfg, tz, redesign=False):
        calls.append((uid, month))
        await asyncio.sleep(0)
        store.month_dir(uid, month).mkdir(parents=True)
        (store.month_dir(uid, month) / "index.html").write_text("<html></html>")
        data = {"month": month, "totals": {"media_hours": 3}, "errors": {}, "notified": False}
        store.write_snapshot(uid, month, data)
        return data

    monkeypatch.setattr(build, "build_month", fake)
    sent = []

    async def fake_notify(plugin_id, **kw):
        sent.append((plugin_id, kw))
        return {"recipients": 1, "delivered": 1, "sent": True, "reason": None}

    monkeypatch.setattr(mod, "notify", fake_notify)
    return calls, sent


def test_scheduler_builds_one_person_per_run_and_is_idempotent(people, fake_build):
    calls, sent = fake_build
    people[3] = dict(ANNA)
    people[2] = {}  # nothing connected: never built
    plugin = RecapPlugin()

    async def tick():
        await plugin.scheduled(FakeDB([1, 2, 3]))
        await build.wait_all()

    run(tick())
    assert calls == [(1, MONTH)]  # one person in this run
    run(tick())
    assert calls == [(1, MONTH), (3, MONTH)]  # the next run takes the next one
    run(tick())
    run(tick())
    assert calls == [(1, MONTH), (3, MONTH)]  # nothing twice, nobody without sources
    assert [(k["user_ids"], k["data"]["route"]) for _, k in sent] == [([1], f"/plugin-page/recap/recaps?path={MONTH}/"),
                                                                     ([3], f"/plugin-page/recap/recaps?path={MONTH}/")]
    assert all(s[0] == "recap" for s in sent)
    assert store.read_snapshot(1, MONTH)["notified"] is True


def test_scheduler_never_runs_two_builds_in_parallel(people, monkeypatch):
    people[3] = dict(ANNA)
    running, peak, calls = [0], [0], []

    async def slow(uid, month, cfg, tz, redesign=False):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        calls.append(uid)
        await asyncio.sleep(0.05)
        running[0] -= 1
        raise build.BuildFailed("x")

    monkeypatch.setattr(build, "build_month", slow)
    plugin = RecapPlugin()

    async def burst():
        for _ in range(5):
            await plugin.scheduled(FakeDB([1, 3]))
        await build.wait_all()

    run(burst())
    assert peak[0] == 1 and calls == [1]  # the same month is not started again while it runs


def test_scheduler_does_not_notify_twice_and_retries_a_lost_notification(people, fake_build, monkeypatch):
    calls, sent = fake_build
    plugin = RecapPlugin()
    results = iter([{"sent": False, "reason": "rate_limited"}, {"sent": True, "reason": None}])

    async def flaky(plugin_id, **kw):
        sent.append(kw)
        return next(results)

    monkeypatch.setattr(mod, "notify", flaky)

    async def tick():
        await plugin.scheduled(FakeDB([1]))
        await build.wait_all()

    run(tick())
    assert store.read_snapshot(1, MONTH)["notified"] is False
    run(tick())  # next hour: only the notification is retried, not the build
    run(tick())
    assert len(calls) == 1 and len(sent) == 2 and store.read_snapshot(1, MONTH)["notified"] is True


def test_failed_builds_back_off(people, monkeypatch):
    calls = []

    async def boom(uid, month, cfg, tz, redesign=False):
        calls.append(uid)
        raise build.BuildFailed("x")

    monkeypatch.setattr(build, "build_month", boom)
    plugin = RecapPlugin()

    async def tick():
        await plugin.scheduled(FakeDB([1]))
        await build.wait_all()

    run(tick())
    run(tick())  # within six hours: not again
    assert calls == [1] and store.attempts(1, MONTH)["count"] == 1
    store.record_attempt(1, MONTH, 0)
    store.record_attempt(1, MONTH, 0)
    run(tick())  # three attempts used up: stop
    assert calls == [1]


def test_scheduler_skips_people_whose_month_is_done(people, fake_build):
    calls, _ = fake_build
    store.write_snapshot(1, MONTH, {"month": MONTH, "notified": True})
    run(RecapPlugin().scheduled(FakeDB([1])))
    run(build.wait_all())
    assert calls == []


# --- endpoints ---

@pytest.fixture
def client(people):
    app = FastAPI()
    for router in RecapPlugin().routers():
        app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    with TestClient(app) as test_client:  # keeps one event loop alive, so background builds can finish
        yield test_client


def wait_for_months(client, count=1):
    for _ in range(100):
        items = client.get("/api/plugins/recap/months").json()["items"]
        if len(items) >= count:
            return items
        time.sleep(0.05)
    raise AssertionError("build did not finish")


def test_build_endpoint_runs_in_the_background_and_lists_the_month(client, fake_build):
    calls, sent = fake_build
    body = client.post("/api/plugins/recap/build").json()
    assert body["status"] == "started" and body["month"] == MONTH and body["route"].endswith(f"path={MONTH}/")
    items = wait_for_months(client)
    assert items[0]["month"] == MONTH and items[0]["label"] == "Oktober 2026"
    again = client.post("/api/plugins/recap/build").json()
    assert again["status"] == "exists" and len(calls) == 1
    assert client.post("/api/plugins/recap/build?force=true").json()["status"] == "started"
    wait_for_months(client)


def test_build_endpoint_validates_the_month_and_the_sources(client, people, fake_build):
    assert client.post("/api/plugins/recap/build?month=2026-13").status_code == 422
    assert client.post("/api/plugins/recap/build?month=../x").status_code == 422
    assert client.post("/api/plugins/recap/build?month=2030-01").status_code == 422  # not finished yet
    people[1].clear()
    assert client.post("/api/plugins/recap/build").json()["status"] == "no_sources"


def test_build_endpoint_for_an_older_month(client, fake_build):
    calls, _ = fake_build
    assert client.post("/api/plugins/recap/build?month=2026-08").json()["month"] == "2026-08"
    wait_for_months(client)
    assert calls == [(1, "2026-08")]


def test_markdown_endpoint(client, people, services):
    build_for()
    text = client.get(f"/api/plugins/recap/markdown/{MONTH}")
    assert text.status_code == 200 and text.headers["content-type"].startswith("text/markdown")
    body = text.text
    assert body.startswith("---\ntype: rueckblick") and "# Rückblick Oktober 2026" in body
    assert f"(/plugin-page/recap/recaps?path={MONTH}/)" in body and "Great Show" in body and "## Meine Notizen" in body
    assert client.get("/api/plugins/recap/markdown/2026-09").status_code == 404
    assert client.get("/api/plugins/recap/markdown/..%2Fx").status_code in (404, 422)
    assert client.get("/api/plugins/recap/markdown/2026-1").status_code == 422


# --- connection test ---

def test_personal_test_reports_each_source_without_secrets_or_addresses(people, services):
    result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=1), dict(ANNA)))
    assert result["ok"] and result["level"] == "ok"
    for name in ("Jellyfin", "ListenBrainz", "Audiobookshelf", "Immich", "Navidrome", "Nextcloud"):
        assert f"{name}: connected" in result["message"]
    assert not any(s in result["message"] for s in SECRETS) and "example.org" not in result["message"]


def test_personal_test_failures_and_unset_servers(people):
    with respx.mock(using="httpx", assert_all_called=False) as http:
        install(http)
        http.get(f"{JF}/Users").mock(return_value=httpx.Response(401))
        http.get(f"{ABS}/api/me").mock(side_effect=httpx.ConnectError("boom https://abs.example.org"))
        http.get(host="photos.example.org", path="/api/users/me").mock(side_effect=httpx.ReadTimeout("slow"))
        http.get(host="api.listenbrainz.org", path="/1/validate-token").mock(return_value=httpx.Response(
            200, json={"valid": True, "user_name": "someone_else"}))
        result = run(RecapPlugin().test_personal_settings(SimpleNamespace(id=1), dict(ANNA)))
    message = result["message"]
    assert not result["ok"] and result["level"] == "warn"
    assert "Jellyfin: login rejected" in message and "Audiobookshelf: not reachable" in message
    assert "Immich: no answer in time" in message and "ListenBrainz: token belongs to another user" in message
    assert "boom" not in message and "example.org" not in message and not any(s in message for s in SECRETS)


def test_personal_test_with_nothing_filled_in_and_missing_server_settings(people, monkeypatch):
    plugin = RecapPlugin()
    assert run(plugin.test_personal_settings(SimpleNamespace(id=2), {}))["level"] == "info"
    monkeypatch.delenv("RECAP_JELLYFIN_URL")
    monkeypatch.delenv("RECAP_ABS_URL")
    result = run(plugin.test_personal_settings(SimpleNamespace(id=2), {"RECAP_JELLYFIN_USER": "x", "RECAP_ABS_TOKEN": "y"}))
    assert "Jellyfin: not set up by the admin" in result["message"] and "Audiobookshelf: not set up by the admin" in result["message"]


# --- configuration ---

def test_config_needs_a_server_and_a_personal_account_per_source(people, monkeypatch):
    assert mod.load_config(1).sources() == ["jellyfin", "music", "audiobooks", "podcasts", "photos"]
    assert mod.load_config(2).sources() == []
    monkeypatch.delenv("RECAP_IMMICH_URL")
    monkeypatch.delenv("RECAP_JELLYFIN_TOKEN")
    assert mod.load_config(1).sources() == ["music", "audiobooks", "podcasts"]  # no server, no source
    people[2] = {"RECAP_NAVIDROME_USER": "x", "RECAP_NAVIDROME_PASSWORD": "y"}
    assert mod.load_config(2).sources() == [] and mod.load_config(2).soundtrack  # the soundtrack alone is no source


def test_urls_are_normalised(monkeypatch):
    monkeypatch.setenv("RECAP_IMMICH_URL", "https://photos.example.org/api/")
    monkeypatch.setenv("RECAP_ABS_URL", "javascript:alert(1)")
    urls = mod._global_urls()
    assert urls["immich_url"] == "https://photos.example.org" and urls["abs_url"] == ""


def test_no_secret_is_ever_logged(people, services, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    build_for()
    run(RecapPlugin().test_personal_settings(SimpleNamespace(id=1), dict(ANNA)))
    assert not any(s in caplog.text for s in SECRETS)
    assert logging.getLogger("httpx").level >= logging.WARNING


def test_views_escape_markup(people):
    store.month_dir(1, MONTH).mkdir(parents=True)
    (store.month_dir(1, MONTH) / "index.html").write_text("x")
    store.write_snapshot(1, MONTH, {"month": MONTH, "totals": {"media_hours": 1}, "photos": {"total": "<b>9</b>"}})
    assert "<b>9</b>" not in views.render_index(1) and "&lt;b&gt;" in views.render_index(1)


def test_channel_head_reads_title_and_cover_of_large_feeds():
    from fh_plugin_recap import collectors

    items = b"<item><title>Episode</title><description>" + b"x" * (collectors.MAX_FEED_BYTES + 10) + b"</description></item>"
    raw = (b'<?xml version="1.0"?><rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
           b"<title><![CDATA[Hotel &amp; Matze]]></title>"
           b'<itunes:image href="https://pod.example.org/big.jpg"/>' + items + b"</channel></rss>")
    assert len(raw) > collectors.MAX_FEED_BYTES
    assert collectors._channel_head(raw) == ("Hotel & Matze", "https://pod.example.org/big.jpg")


def test_channel_head_without_title():
    from fh_plugin_recap import collectors

    assert collectors._channel_head(b"<rss><channel><item><title>Only episode</title></item></channel></rss>") == (None, None)


def test_intro_collage_stays_dimmed_behind_the_text():
    # the generic rise animation ends at opacity 1; the collage must use its own animation and a dimming filter
    from fh_plugin_recap import story

    assert ".slide.on .inner>.collage{animation:dim" in story.PAGE
    assert "brightness(.32)" in story.PAGE
    assert ".s-intro .inner>*:not(.collage){position:relative;z-index:1}" in story.PAGE


# --- redesign ---

def test_rebuild_keeps_the_design_and_a_redesign_changes_it(people, services):
    first = build_for()
    assert build_for()["design"] == first["design"] and "design_round" not in {k for k, v in first.items() if v}
    fresh = build_for(redesign=True)
    old, new = first["design"], fresh["design"]
    assert fresh["design_round"] == 1 and new["palette"] != old["palette"]
    assert new["shape"] != old["shape"] and new["type"] != old["type"]
    assert store.read_snapshot(1, MONTH)["design"] == new
    assert build_for()["design"] == new and store.read_snapshot(1, MONTH)["design_round"] == 1  # the new design sticks
    assert build_for(redesign=True)["design_round"] == 2


def test_redesign_avoids_every_choice_of_the_current_design():
    from fh_plugin_recap import story
    for seed in range(12):
        data = {"month": MONTH, "seed": str(seed)}
        current = story.choose_design(data, [])
        new = story.choose_design({**data, "design_round": 1}, [], current=current)
        assert new["palette"] != current["palette"] and new["shape"] != current["shape"] and new["type"] != current["type"]
        for group, sizes in (("variants", story.VARIANTS), ("texts", {k: len(v) for k, v in story.KICKERS.items()})):
            for key, value in new[group].items():
                if sizes[key] > 1:
                    assert value != current[group][key], (seed, group, key)


def test_build_endpoint_passes_redesign_and_rebuilds_an_existing_month(client, fake_build, monkeypatch):
    seen = []
    real = build.start

    def spy(*args, **kw):
        seen.append(kw.get("redesign"))
        return real(*args, **kw)

    monkeypatch.setattr(build, "start", spy)
    client.post("/api/plugins/recap/build")
    wait_for_months(client)
    assert client.post("/api/plugins/recap/build?redesign=true").json()["status"] == "started"
    wait_for_months(client)
    assert seen == [False, True]


# --- "Neu gestalten" from the archive page ---

def test_archive_offers_a_redesign_per_month_that_starts_a_fresh_design(people, services, monkeypatch):
    build_for()
    plugin = RecapPlugin()
    _, index = page(plugin, 1, "")
    assert b'href="2026-10/neu"' in index and "Neu gestalten".encode() in index and b"confirm(" in index
    started = []
    monkeypatch.setattr(build, "start", lambda uid, month, cfg, tz, **kw: started.append((uid, month, kw["redesign"])) or "started")
    ctype, body = page(plugin, 1, "2026-10/neu")
    assert ctype.startswith("text/html") and started == [(1, MONTH, True)]
    assert "neues Design".encode() in body and b"fetch('status'" in body and b"location.replace('./')" in body
    monkeypatch.setattr(build, "is_running", lambda uid, month: True)
    ctype, body = page(plugin, 1, "2026-10/status")
    assert ctype == "application/json" and json.loads(body) == {"running": True}
    ctype, body = page(plugin, 1, "2026-10/neu")  # a second tap while it runs does not start another build
    assert started == [(1, MONTH, True)] and "gerade neu gebaut".encode() in body


def test_redesign_page_explains_when_another_build_is_running(people, services, monkeypatch):
    build_for()
    monkeypatch.setattr(build, "start", lambda *a, **kw: "busy")
    _, body = page(RecapPlugin(), 1, "2026-10/neu")
    assert "anderer Rückblick".encode() in body and b"fetch(" not in body and b'href="../"' in body


def test_redesign_is_only_offered_for_existing_months_of_that_person(people, services, monkeypatch):
    build_for()
    monkeypatch.setattr(build, "start", lambda *a, **kw: pytest.fail("must not build"))
    plugin = RecapPlugin()
    assert page(plugin, 2, "2026-10/neu") is None  # someone else's month
    assert page(plugin, 1, "2026-09/neu") is None  # not built
    for bad in ("2026-13/neu", "../2026-10/neu", "2026-10/neu/x", "2026-10/delete"):
        assert page(plugin, 1, bad) is None
