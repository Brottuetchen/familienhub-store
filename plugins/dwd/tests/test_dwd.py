"""Tests for the DWD plugin. Run with the hub's backend on the path:

    pip install pytest respx
    PYTHONPATH=/path/to/familienhub/backend pytest

Services are simulated with respx (shapes: Bright Sky documentation, and a recording of the real DWD pollen file).
The hub's own code (notify, plugin classes, manifest validation) is the real one; only the final push delivery of
the hub is recorded.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import ALERTS_URL, NOW, POLLEN_URL, FIXTURES, alert, alerts_payload, pollen_payload, region_pollen

from app.core.security import get_current_user
from app.plugins.manifest import validate_manifest
from conftest import ok
from fh_plugin_dwd import dwd, plugin as plugin_module, state
from fh_plugin_dwd.plugin import DwdPlugin

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client():
    app = FastAPI()
    for router in DwdPlugin().routers():
        app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


# ---------------------------------------------------------------- manifest

def test_manifest_is_valid_and_matches_the_code():
    manifest = validate_manifest(json.loads((ROOT / "familienhub-plugin.json").read_text()))
    plugin = DwdPlugin()
    assert plugin.id == manifest["id"] == "dwd"
    assert manifest["entry_point"] == "fh_plugin_dwd.plugin:DwdPlugin"
    assert manifest["min_hub_version"] == "1.0.0-beta.3"
    assert set(manifest["hosts"]) == {"api.brightsky.dev", "opendata.dwd.de"}
    assert {s.key for s in plugin.settings} == {s["key"] for s in manifest["settings"]}
    assert set(plugin.capabilities) <= set(manifest["capabilities"])
    assert all(not s.get("required", True) for s in manifest["settings"])
    for router in plugin.routers():
        assert all(r.path.startswith("/api/plugins/dwd/") for r in router.routes)
    assert plugin.schedule_minutes == 15


def test_store_entry_is_valid():
    from app.store.index import parse_index

    entry = json.loads((ROOT / "store-entry.json").read_text())
    if entry["sha"] == "TBD":  # the maintainer pins the commit when the entry goes into index.json
        entry["sha"] = "0" * 40
    base = "https://raw.githubusercontent.com/Brottuetchen/familienhub-store/main/index.json"
    parsed = parse_index(json.dumps({"version": 1, "entries": [entry]}).encode(), base, frozenset({"raw.githubusercontent.com"}))
    assert not parsed.skipped and parsed.entries[0].id == "dwd"


# ---------------------------------------------------------------- parsing

def test_parse_alerts_sorted_and_filtered():
    payload = alerts_payload(
        alert(22), alert(52), alert(51),
        alert(33, ident="expired", start_h=-10, end_h=-1),
        alert(22, ident="cancelled", status="cancel"),
        alert(51, ident="allclear", response_type="AllClear"),
        {"nonsense": True}, "junk",
    )
    parsed = dwd.parse_alerts(payload, NOW)
    assert [a.event_code for a in parsed] == [52, 51, 22]
    assert [a.rank for a in parsed] == [3, 2, 1]
    top = parsed[0]
    assert top.event["de"] == "STURMBÖEN" and top.headline["en"].startswith("Official WARNING")
    assert top.onset < NOW < top.expires


def test_alert_without_severity_counts_as_minor_and_open_end_is_allowed():
    a = dwd.parse_alerts(alerts_payload(alert(22, severity="", expires=None)), NOW)
    assert len(a) == 1 and a[0].rank == 1


def test_alerts_with_wrong_shape_raise():
    for bad in ({"alerts": "x"}, [], None, {"foo": 1}):
        with pytest.raises(ValueError):
            dwd.parse_alerts(bad, NOW)


def test_recorded_empty_answer_parses():
    payload = json.loads((FIXTURES / "brightsky_no_alerts.json").read_text())
    assert dwd.parse_alerts(payload, NOW) == []


@pytest.mark.parametrize("value, level", [
    ("0", "none"), ("0-1", "low"), ("1", "low"), ("1-2", "medium"), ("2", "medium"), ("2-3", "high"), ("3", "high"),
    (" 2 - 3 ", "high"), ("-1", "none"), ("", "none"), (None, "none"), ("lots", "none"),
])
def test_pollen_level_mapping(value, level):
    assert dwd.pollen_level(value) == level


def test_every_value_of_the_recorded_legend_is_mapped():
    legend = json.loads((FIXTURES / "s31fg.json").read_text())["legend"]
    values = [v for k, v in legend.items() if not k.endswith("_desc")]
    assert sorted(values) == sorted(dwd.DWD_VALUE_LEVEL)


def test_recorded_file_has_the_pollen_types_we_expect():
    data = json.loads((FIXTURES / "s31fg.json").read_text())
    assert set(data["content"][0]["Pollen"]) == set(dwd.POLLEN_TYPES)


@pytest.mark.parametrize("text, keys", [
    ("", list(dwd.POLLEN_TYPES)),
    ("Birke, Gräser", ["Birke", "Graeser"]),
    ("birch;grasses ; Beifuß", ["Birke", "Graeser", "Beifuss"]),
    ("RAGWEED", ["Ambrosia"]),
])
def test_pollen_types_list(text, keys):
    got, unknown = dwd.parse_pollen_types(text)
    assert got == keys and unknown == []


def test_unknown_pollen_type_is_reported():
    assert dwd.parse_pollen_types("Birke, Tulpe") == (["Birke"], ["Tulpe"])


@pytest.mark.parametrize("wanted, region", [
    ("92", "Rhein-Main"), ("rhein-main", "Rhein-Main"), ("RHEIN-MAIN", "Rhein-Main"),
    ("50", "Brandenburg und Berlin"), ("Berlin", "Brandenburg und Berlin"), ("Brandenburg", "Brandenburg und Berlin"),
    ("Mecklenburg-Vorpommern", "Mecklenburg-Vorpommern"),
])
def test_region_matching(wanted, region):
    fc = dwd.forecast_for(json.loads((FIXTURES / "s31fg.json").read_text()), wanted)
    assert fc is not None and fc.region == region


def test_state_with_several_partregions_takes_the_highest_level():
    data = region_pollen(92, Birke=("3", "2"))  # only Rhein-Main has birch
    fc = dwd.forecast_for(data, "Hessen")
    assert fc.region == "Hessen" and fc.today["Birke"] == "high" and fc.tomorrow["Birke"] == "medium"
    assert dwd.forecast_for(data, "Rhein-Main").today["Birke"] == "high"
    assert dwd.forecast_for(data, "91").today["Birke"] == "none"  # Nordhessen has none


def test_saxony_does_not_match_saxony_anhalt():
    data = json.loads((FIXTURES / "s31fg.json").read_text())
    names = {e["region_name"] for e in dwd.match_regions(data["content"], "Sachsen")}
    assert names == {"Sachsen"}


@pytest.mark.parametrize("wanted", ["", "Atlantis", "-1", "999"])
def test_unknown_region_gives_none(wanted):
    assert dwd.forecast_for(json.loads((FIXTURES / "s31fg.json").read_text()), wanted) is None


# ---------------------------------------------------------------- dedupe and state

def _plan(alerts, entries, min_rank=2):
    return state.plan(dwd.parse_alerts(alerts_payload(*alerts), NOW), entries, min_rank, NOW)


def test_new_alert_is_announced_once_across_runs(tmp_path):
    path = tmp_path / "state.json"
    pending, entries = _plan([alert(51)], state.load(path))
    assert [p.alert.event_code for p in pending] == [51] and not pending[0].update
    state.mark_notified(entries, pending)
    assert state.save(path, entries)
    # next run, and a "restart": state is read from the file again
    pending, entries = _plan([alert(51)], state.load(path))
    assert pending == []


def test_alert_that_was_not_delivered_is_tried_again():
    pending, entries = _plan([alert(51)], {})
    # nothing marked as notified (e.g. rate limited)
    again, _ = _plan([alert(51)], entries)
    assert len(again) == 1


def test_below_minimum_is_never_announced_but_remembered():
    pending, entries = _plan([alert(22)], {})
    assert pending == [] and entries["2.49.0.0.276.0.DWD.PVW.22.test"]["notified"] == 0


def test_upgrade_with_the_same_id_is_announced_as_update():
    pending, entries = _plan([alert(51)], {})
    state.mark_notified(entries, pending)
    pending, entries = _plan([alert(51, severity="severe")], entries)
    assert len(pending) == 1 and pending[0].update and pending[0].alert.rank == 3
    state.mark_notified(entries, pending)
    assert _plan([alert(51, severity="severe")], entries)[0] == []


def test_reissued_warning_under_a_new_id_is_not_announced_again():
    pending, entries = _plan([alert(51, ident="old")], {})
    state.mark_notified(entries, pending)
    pending, entries = _plan([alert(51, ident="new")], entries)  # same event, same window, same level
    assert pending == []


def test_reissued_warning_with_higher_level_is_an_update():
    pending, entries = _plan([alert(51, ident="old")], {})
    state.mark_notified(entries, pending)
    pending, entries = _plan([alert(51, ident="new", severity="severe")], entries)
    assert len(pending) == 1 and pending[0].update


def test_other_event_at_the_same_time_is_a_new_warning():
    pending, entries = _plan([alert(51)], {})
    state.mark_notified(entries, pending)
    pending, _ = _plan([alert(51), alert(52)], entries)
    assert [p.alert.event_code for p in pending] == [52] and not pending[0].update


def test_alert_rising_above_the_minimum_is_new_not_an_update():
    _, entries = _plan([alert(22)], {})  # minor, remembered, not announced
    pending, _ = _plan([alert(22, severity="severe")], entries)
    assert len(pending) == 1 and not pending[0].update


def test_expired_entries_are_pruned_and_file_stays_small():
    _, entries = _plan([alert(51, ident="gone", end_h=1)], {})
    later = NOW.replace(day=22)
    pending, entries = state.plan([], entries, 2, later)
    assert entries == {} and pending == []


def test_state_file_problems_do_not_crash(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ not json")
    assert state.load(path) == {}
    path.write_text(json.dumps({"alerts": ["x"]}))
    assert state.load(path) == {}
    assert state.load(tmp_path / "missing.json") == {}
    blocked = tmp_path / "file"
    blocked.write_text("x")
    assert state.save(blocked / "sub" / "s.json", {}) is False


# ---------------------------------------------------------------- fetching and errors

@pytest.mark.parametrize("answer", [
    httpx.Response(500), httpx.Response(200, text="not json"), httpx.Response(200, json={"unexpected": 1}),
    httpx.ConnectError("down"), httpx.ReadTimeout("slow"),
])
def test_a_broken_alert_service_is_unavailable_not_an_error(http, answer):
    http.get(ALERTS_URL).mock(side_effect=[answer])
    res = run(dwd.fetch_alerts(50.11, 8.68, "Europe/Berlin", NOW))
    assert res.status == "unavailable" and res.alerts == []


def test_position_outside_germany_is_not_covered(http):
    http.get(ALERTS_URL).mock(return_value=httpx.Response(404, json={"detail": "Requested position is not covered by the DWD"}))
    assert run(dwd.fetch_alerts(48.0, 2.0, "Europe/Berlin", NOW)).status == "not_covered"


def test_request_carries_the_position_and_timeout_is_short(http):
    route = http.get(ALERTS_URL).mock(return_value=ok(alerts_payload()))
    run(dwd.fetch_alerts(50.11, 8.68, "Europe/Berlin", NOW))
    q = route.calls.last.request.url.params
    assert q["lat"] == "50.1100" and q["lon"] == "8.6800" and q["tz"] == "Europe/Berlin"
    assert dwd.TIMEOUT <= 8


def test_answers_are_cached_and_stale_copy_survives_an_outage(http):
    route = http.get(ALERTS_URL).mock(side_effect=[ok(alerts_payload(alert(51))), httpx.ConnectError("down")])
    first = run(dwd.fetch_alerts(50.11, 8.68, "Europe/Berlin", NOW))
    again = run(dwd.fetch_alerts(50.11, 8.68, "Europe/Berlin", NOW))
    assert route.call_count == 1 and len(again.alerts) == 1
    forced = run(dwd.fetch_alerts(50.11, 8.68, "Europe/Berlin", NOW, force=True))  # outage on a forced refresh
    assert route.call_count == 2 and forced.status == "ok" and forced.stale and len(forced.alerts) == 1
    assert first.status == "ok"


def test_pollen_file_error_paths(http):
    http.get(POLLEN_URL).mock(side_effect=[httpx.Response(503)])
    assert run(dwd.fetch_pollen_file()) is None
    dwd.clear_cache()
    http.get(POLLEN_URL).mock(side_effect=[httpx.Response(200, json={"content": "x"})])
    assert run(dwd.fetch_pollen_file()) is None


# ---------------------------------------------------------------- Today cards

def _cards(http, alerts=(), pollen=None, user=None, state_name="Hessen"):
    http.get(ALERTS_URL).mock(return_value=ok(alerts_payload(*alerts, state=state_name)))
    http.get(POLLEN_URL).mock(return_value=ok(pollen if pollen is not None else pollen_payload()))
    return run(DwdPlugin().today_cards(user or object(), None))


def test_no_card_when_nothing_is_relevant(http):
    assert _cards(http) == []


def test_warnings_card_content(http):
    cards = _cards(http, [alert(51), alert(52, start_h=3, end_h=8)])
    assert len(cards) == 1
    c = cards[0]
    assert c["id"] == "warnings" and c["icon"] == "warning" and c["badge"] == "Unwetter"
    assert c["title"] == "Wetterwarnungen"
    assert c["lines"][0] == {"text": "Amtliche WARNUNG vor STURMBÖEN", "secondary": "Di 17:00–22:00"}
    assert c["lines"][1] == {"text": "Amtliche WARNUNG vor WINDBÖEN", "secondary": "bis Di 20:00"}


def test_warnings_card_in_english_for_an_english_user(http):
    class U:
        language = "en"

    c = _cards(http, [alert(51)], user=U())[0]
    assert c["title"] == "Weather warning" and c["badge"] == "Moderate"
    assert c["lines"] == [{"text": "Official WARNING of WIND GUSTS", "secondary": "until Tue 20:00"}]


def test_warning_window_across_midnight():
    a = dwd.parse_alerts(alerts_payload(alert(51, start_h=9, end_h=20)), NOW)[0]
    assert plugin_module.window_text(a, NOW, "en") == "Tue 23:00 – Wed 10:00"
    assert plugin_module.window_text(a, NOW, "de") == "Di 23:00 – Mi 10:00"


def test_card_with_many_warnings_is_cut_to_six_lines(http):
    many = [alert(51, ident=f"a{i}") for i in range(9)]
    c = _cards(http, many)[0]
    assert len(c["lines"]) == 6 and c["lines"][5]["text"] == "+4 weitere Warnungen"


def test_pollen_card_content_and_minimum(http):
    data = region_pollen(92, Birke=("3", "2"), Graeser=("2", "1"), Erle=("1", "0"), Hasel=("0-1", "0"))
    c = _cards(http, pollen=data)[0]
    assert c["id"] == "pollen" and c["icon"] == "eco" and c["title"] == "Pollenflug · Hessen" and c["badge"] == "Hoch"
    assert c["lines"] == [{"text": "Birke: hoch", "secondary": "morgen: mittel"},
                          {"text": "Gräser: mittel", "secondary": "morgen: gering"}]  # low is below the default


def test_pollen_card_low_minimum_and_type_filter(http, monkeypatch):
    monkeypatch.setenv("DWD_POLLEN_MIN", "low")
    monkeypatch.setenv("DWD_POLLEN_TYPES", "Erle, Birch")
    data = region_pollen(92, Birke=("3", "2"), Graeser=("2", "1"), Erle=("1", "0"))
    c = _cards(http, pollen=data)[0]
    assert [l["text"] for l in c["lines"]] == ["Birke: hoch", "Erle: gering"]


def test_pollen_card_uses_the_configured_region(http, monkeypatch):
    monkeypatch.setenv("DWD_POLLEN_REGION", "Rhein-Main")
    data = region_pollen(92, Birke=("3", "3"))
    c = _cards(http, pollen=data, state_name="Bayern")[0]  # the state would give Bavaria; the region wins
    assert c["title"] == "Pollenflug · Rhein-Main"
    monkeypatch.setenv("DWD_POLLEN_REGION", "Atlantis")
    assert _cards(http, pollen=data) == []


def test_pollen_in_english(http):
    class U:
        language = "en-GB"

    c = _cards(http, pollen=region_pollen(92, Graeser=("3", "3"), Beifuss=("2", "2")), user=U())[0]
    assert c["title"] == "Pollen · Hessen" and c["badge"] == "High"
    assert c["lines"][0] == {"text": "Grasses: high", "secondary": "tomorrow: high"}
    assert c["lines"][1]["text"] == "Mugwort: medium"


def test_both_cards_survive_the_hubs_validation_unchanged(http):
    from app.plugins.hooks import sanitize_cards

    plugin = DwdPlugin()
    cards = _cards(http, [alert(51), alert(52)], pollen=region_pollen(92, Birke=("3", "3")))
    assert [c["id"] for c in cards] == ["warnings", "pollen"]
    clean = sanitize_cards(plugin, cards)
    assert [c["id"] for c in clean] == ["warnings", "pollen"]
    for raw, ok_card in zip(cards, clean):
        assert ok_card["icon"] == raw["icon"] and ok_card["title"] == raw["title"] and ok_card["badge"] == raw["badge"]
        assert [l["text"] for l in ok_card["lines"]] == [l["text"] for l in raw["lines"]]


def test_cards_survive_both_services_being_down(http):
    http.get(ALERTS_URL).mock(side_effect=httpx.ConnectError("down"))
    http.get(POLLEN_URL).mock(side_effect=httpx.ConnectError("down"))
    assert run(DwdPlugin().today_cards(object(), None)) == []


def test_warnings_card_without_pollen_service(http):
    http.get(ALERTS_URL).mock(return_value=ok(alerts_payload(alert(51))))
    http.get(POLLEN_URL).mock(return_value=httpx.Response(500))
    assert [c["id"] for c in run(DwdPlugin().today_cards(object(), None))] == ["warnings"]


# ---------------------------------------------------------------- location

def test_weather_plugin_location_is_the_default():
    plugin = DwdPlugin()
    assert plugin_module.resolve_location() == ((50.11, 8.68), None)
    assert plugin.is_configured()


def test_own_coordinates_win(monkeypatch):
    monkeypatch.setenv("DWD_LATITUDE", "48,1")
    monkeypatch.setenv("DWD_LONGITUDE", "11.6")
    assert plugin_module.resolve_location()[0] == (48.1, 11.6)


@pytest.mark.parametrize("lat, lon", [("48.1", ""), ("", "11.6"), ("abc", "11"), ("91", "11"), ("48", "181")])
def test_bad_coordinates_are_a_problem_not_a_guess(monkeypatch, lat, lon):
    monkeypatch.setenv("DWD_LATITUDE", lat)
    monkeypatch.setenv("DWD_LONGITUDE", lon)
    loc, problem = plugin_module.resolve_location()
    assert loc is None and problem
    assert not DwdPlugin().is_configured()


def test_the_hubs_default_location_is_not_taken_for_the_home(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "weather_latitude", 52.52)
    monkeypatch.setattr(settings, "weather_longitude", 13.405)
    plugin = DwdPlugin()
    assert not plugin.is_configured()
    assert plugin.missing_settings() == ["DWD_LATITUDE", "DWD_LONGITUDE"]
    assert "No location" in plugin.not_ready_reason()
    assert run(plugin.today_cards(object(), None)) == []


# ---------------------------------------------------------------- compatibility guard

def test_old_hub_without_plugin_api_is_never_configured_and_never_crashes(monkeypatch, http):
    import sys

    monkeypatch.setitem(sys.modules, "app.plugins.api", None)  # import fails like on a hub without the module
    plugin = DwdPlugin()
    assert not plugin.is_configured()
    assert "1.0.0-beta.3" in plugin.not_ready_reason()
    assert plugin.missing_settings() == []
    assert run(plugin.today_cards(object(), None)) == []
    assert run(plugin.scheduled(None)) is None
    result = run(plugin.test_settings({}))
    assert result["ok"] is False and "1.0.0-beta.3" in result["message"]
    assert run(plugin.check())["status"] == "off"
    assert not http.calls  # nothing was fetched


def test_old_hub_without_hooks_in_the_base_class(monkeypatch):
    from app.plugins.base import Plugin

    monkeypatch.delattr(Plugin, "today_cards")
    assert not DwdPlugin().is_configured()


def test_endpoint_on_old_hub(monkeypatch, client):
    import sys

    monkeypatch.setitem(sys.modules, "app.plugins.api", None)
    body = client.get("/api/plugins/dwd/status").json()
    assert body["status"] == "not_configured" and "1.0.0-beta.3" in body["error"] and body["items"] == []


# ---------------------------------------------------------------- notifications (hub's real notify, recorded delivery)

@pytest.fixture
def hub(db, monkeypatch):
    """The hub's real ``app.plugins.api.notify`` with the plugin registered; only the push delivery is recorded."""
    from app.models.user import User
    from app.plugins.registry import plugin_registry
    from app.services import notify as hub_notify_module

    def person(name, role, language=None, active=True):
        u = User(username=name, email=f"{name}@example.org", hashed_password="x", role=role, language=language,
                 is_active=active, is_admin=role == "admin")
        db.add(u)
        db.commit()
        return u

    users = {
        "admin": person("admin", "admin", "de"), "partner": person("partner", "partner", "en"),
        "child": person("child", "child", None), "guest": person("guest", "guest", "de"),
        "gone": person("gone", "partner", "de", active=False),
    }
    sent = []

    async def deliver(user_id, title, body, data=None, category=None, source=None, **kw):
        sent.append({"user": user_id, "title": title, "body": body, "data": data, "category": category,
                     "source": source})
        return 1

    monkeypatch.setattr(hub_notify_module, "notify", deliver)
    plugin = DwdPlugin()
    plugin_registry.unregister("dwd")
    plugin_registry.register(plugin)
    monkeypatch.setattr(type(plugin), "active", property(lambda self: True))
    yield plugin, users, sent
    plugin_registry.unregister("dwd")


def _job(plugin, db, http, *alerts, state_name="Hessen"):
    http.get(ALERTS_URL).mock(return_value=ok(alerts_payload(*alerts, state=state_name)))
    run(plugin.scheduled(db))


def test_new_warning_reaches_family_but_not_guests(hub, db, http):
    plugin, users, sent = hub
    _job(plugin, db, http, alert(52))
    by_user = {s["user"]: s for s in sent}
    assert set(by_user) == {users["admin"].id, users["partner"].id, users["child"].id}
    assert by_user[users["admin"].id]["title"] == "Unwetterwarnung: STURMBÖEN"  # German user
    assert by_user[users["child"].id]["title"] == "Unwetterwarnung: STURMBÖEN"  # no language = hub default German
    assert by_user[users["partner"].id]["title"] == "Severe weather warning: storm gusts"
    assert by_user[users["partner"].id]["body"].startswith("until Tue 20:00. There is a risk of storm gusts")
    assert all(s["category"] == "plugins" and s["source"] == "plugin:dwd" and s["data"]["route"] == "/today" for s in sent)


def test_same_warning_is_not_sent_again_even_after_restart(hub, db, http):
    plugin, users, sent = hub
    _job(plugin, db, http, alert(52))
    first = len(sent)
    dwd.clear_cache()
    _job(DwdPlugin(), db, http, alert(52))  # a fresh plugin object = a restart; state comes from the file
    assert first == 3 and len(sent) == first
    assert (Path(plugin_module._data_dir()) / "plugin_dwd_state.json").is_file()


def test_upgrade_is_announced_as_update(hub, db, http):
    plugin, users, sent = hub
    _job(plugin, db, http, alert(51))
    sent.clear()
    dwd.clear_cache()
    _job(plugin, db, http, alert(51, severity="severe"))
    titles = {s["user"]: s["title"] for s in sent}
    assert titles[users["admin"].id] == "Aktualisiert: Unwetterwarnung: WINDBÖEN"
    assert titles[users["partner"].id] == "Update: Severe weather warning: wind gusts"


def test_minor_warning_is_not_pushed_with_the_default_but_is_with_minor(hub, db, http, monkeypatch):
    plugin, users, sent = hub
    _job(plugin, db, http, alert(22))
    assert sent == []
    monkeypatch.setenv("DWD_MIN_SEVERITY", "minor")
    dwd.clear_cache()
    _job(plugin, db, http, alert(22))
    assert len(sent) == 3 and sent[0]["title"].startswith("Wetterwarnung")


def test_push_can_be_switched_off(hub, db, http, monkeypatch):
    plugin, users, sent = hub
    monkeypatch.setenv("DWD_NOTIFY", "false")
    _job(plugin, db, http, alert(52))
    assert sent == []
    monkeypatch.setenv("DWD_NOTIFY", "true")
    dwd.clear_cache()
    _job(plugin, db, http, alert(52))
    assert len(sent) == 3


def test_many_new_warnings_become_one_summary_per_language(hub, db, http):
    plugin, users, sent = hub
    _job(plugin, db, http, *[alert(c, ident=f"x{c}{i}") for i, c in enumerate((51, 52, 33, 51))])
    assert len(sent) == 3
    assert {s["title"] for s in sent} == {"4 neue Wetterwarnungen", "4 new weather warnings"}


def test_service_down_sends_nothing_and_keeps_state(hub, db, http):
    plugin, users, sent = hub
    http.get(ALERTS_URL).mock(side_effect=httpx.ConnectError("down"))
    run(plugin.scheduled(db))
    assert sent == []
    assert not (Path(plugin_module._data_dir()) / "plugin_dwd_state.json").exists()


def test_rate_limit_of_the_hub_is_respected_and_retried_later(hub, db, http):
    from app.plugins import api

    plugin, users, sent = hub
    for _ in range(api.RATE_LIMIT_PER_HOUR):
        api._allow("dwd")  # the plugin has used its budget
    _job(plugin, db, http, alert(52))
    assert sent == []
    api.reset_limits()
    dwd.clear_cache()
    _job(plugin, db, http, alert(52))  # not marked as announced, so it goes out now
    assert len(sent) == 3


def test_state_file_is_pruned_when_warnings_end(hub, db, http):
    plugin, users, sent = hub
    _job(plugin, db, http, alert(52, end_h=2))
    path = Path(plugin_module._data_dir()) / "plugin_dwd_state.json"
    assert len(json.loads(path.read_text())["alerts"]) == 1
    from helpers import NOW
    import datetime as dt

    plugin_module._now = lambda: NOW + dt.timedelta(hours=5)
    dwd.clear_cache()
    _job(plugin, db, http)
    assert json.loads(path.read_text())["alerts"] == {}


def test_unwritable_state_folder_does_not_break_the_job(hub, db, http, monkeypatch, tmp_path):
    plugin, users, sent = hub
    blocker = tmp_path / "blocked"
    blocker.write_text("x")
    monkeypatch.setattr(plugin_module, "_data_dir", lambda: str(blocker / "sub"))
    _job(plugin, db, http, alert(52))
    assert len(sent) == 3


# ---------------------------------------------------------------- connection test

def _typed(**kw):
    base = {"DWD_LATITUDE": "50.11", "DWD_LONGITUDE": "8.68", "DWD_MIN_SEVERITY": "moderate", "DWD_NOTIFY": "true",
            "DWD_POLLEN_REGION": "", "DWD_POLLEN_TYPES": "", "DWD_POLLEN_MIN": "medium"}
    base.update(kw)
    return base


def _test(http, values, alerts_answer=None, pollen_answer=None):
    http.get(ALERTS_URL).mock(return_value=alerts_answer or ok(alerts_payload(alert(51))))
    http.get(POLLEN_URL).mock(return_value=pollen_answer or ok(pollen_payload()))
    return run(DwdPlugin().test_settings(values))


def test_connection_test_ok(http):
    r = _test(http, _typed())
    assert r["ok"] and r["level"] == "ok"
    assert "Frankfurt am Main" in r["message"] and "1 warning(s) active" in r["message"] and "Pollen region: Hessen" in r["message"]


def test_connection_test_uses_unsaved_values_and_a_fresh_request(http):
    route = http.get(ALERTS_URL).mock(return_value=ok(alerts_payload()))
    http.get(POLLEN_URL).mock(return_value=ok(pollen_payload()))
    run(DwdPlugin().test_settings(_typed()))
    run(DwdPlugin().test_settings(_typed(DWD_LATITUDE="48.14", DWD_LONGITUDE="11.58")))
    assert route.call_count == 2
    assert route.calls.last.request.url.params["lat"] == "48.1400"


def test_connection_test_with_explicit_region(http):
    r = _test(http, _typed(DWD_POLLEN_REGION="92"))
    assert r["ok"] and "Pollen region: Rhein-Main" in r["message"]


def test_connection_test_bad_region_lists_the_choices(http):
    r = _test(http, _typed(DWD_POLLEN_REGION="Atlantis"))
    assert not r["ok"] and r["level"] == "error"
    assert "'Atlantis' not found" in r["message"] and "92: Rhein-Main" in r["message"]


@pytest.mark.parametrize("override, fragment", [
    ({"DWD_MIN_SEVERITY": "huge"}, "minor, moderate, severe, extreme"),
    ({"DWD_POLLEN_MIN": "none"}, "low, medium, high"),
    ({"DWD_POLLEN_TYPES": "Birke, Tulpe"}, "Unknown pollen type: Tulpe"),
    ({"DWD_LATITUDE": "x"}, "must be numbers"),
    ({"DWD_LONGITUDE": ""}, "both latitude and longitude"),
])
def test_connection_test_validates_the_settings(http, override, fragment):
    r = _test(http, _typed(**override))
    assert not r["ok"] and r["level"] == "error" and fragment in r["message"]


def test_connection_test_outside_germany(http):
    r = _test(http, _typed(DWD_LATITUDE="48.85", DWD_LONGITUDE="2.35"),
              alerts_answer=httpx.Response(404, json={"detail": "Requested position is not covered by the DWD"}))
    assert not r["ok"] and "does not cover" in r["message"]


def test_connection_test_service_down(http):
    r = _test(http, _typed(), alerts_answer=httpx.Response(502))
    assert not r["ok"] and "not reachable" in r["message"]


def test_connection_test_pollen_down_is_only_a_warning(http):
    r = _test(http, _typed(), pollen_answer=httpx.Response(500))
    assert r["ok"] and r["level"] == "warn" and "pollen forecast could not be loaded" in r["message"]


def test_connection_test_state_without_region_asks_for_one(http):
    r = _test(http, _typed(), alerts_answer=ok(alerts_payload(state="Mars")))
    assert r["ok"] and r["level"] == "warn" and "DWD_POLLEN_REGION" in r["message"]


def test_connection_test_without_any_location(http, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "weather_latitude", 52.52)
    monkeypatch.setattr(settings, "weather_longitude", 13.405)
    r = _test(http, _typed(DWD_LATITUDE="", DWD_LONGITUDE=""))
    assert not r["ok"] and "No location" in r["message"]


def test_messages_contain_no_urls_or_secrets(http):
    r = _test(http, _typed(DWD_POLLEN_REGION="Atlantis"))
    assert "http" not in r["message"]


# ---------------------------------------------------------------- endpoint and admin check

def test_status_endpoint(client, http):
    http.get(ALERTS_URL).mock(return_value=ok(alerts_payload(alert(52))))
    http.get(POLLEN_URL).mock(return_value=ok(region_pollen(92, Birke=("3", "2"))))
    body = client.get("/api/plugins/dwd/status", headers={"X-App-Language": "en"}).json()
    assert body["status"] == "ok" and body["error"] is None
    assert body["items"][0]["severity"] == "severe" and body["items"][0]["event"] == "storm gusts"
    assert body["pollen"]["region"] == "Hessen" and body["pollen"]["today"] == {"Birke": "high"}


def test_status_endpoint_when_down(client, http):
    http.get(ALERTS_URL).mock(return_value=httpx.Response(500))
    body = client.get("/api/plugins/dwd/status", headers={"X-App-Language": "en"}).json()
    assert body == {"status": "unavailable", "error": "The German Weather Service is not reachable right now",
                    "items": [], "pollen": None}


def test_status_endpoint_without_location(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "weather_latitude", 52.52)
    monkeypatch.setattr(settings, "weather_longitude", 13.405)
    assert client.get("/api/plugins/dwd/status").json()["status"] == "not_configured"


def test_admin_check(http):
    http.get(ALERTS_URL).mock(return_value=ok(alerts_payload(alert(52), alert(51))))
    assert run(DwdPlugin().check()) == {"status": "ok", "message": "2 active warnings"}
    dwd.clear_cache()
    http.get(ALERTS_URL).mock(return_value=httpx.Response(500))
    assert run(DwdPlugin().check())["status"] == "error"
