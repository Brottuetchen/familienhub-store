"""Tests for the departures plugin. Run with the hub's backend on the path:

    DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest

The transport.rest instance is simulated with respx (answers shaped like the documented hafas-rest v6 API);
the real API is never called. The clock is fixed: Tuesday 2026-10-20, 07:30 in Europe/Berlin (UTC+2).
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from app.plugins import Plugin
from app.plugins.hooks import sanitize_cards
from app.plugins.manifest import hub_version_ok, validate_manifest
from fh_plugin_departures import logic, plugin as mod
from fh_plugin_departures.plugin import DeparturesPlugin

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://v6.db.transport.rest"
BERLIN = ZoneInfo("Europe/Berlin")
STOP = "8000999"
DEPS = f"{BASE}/stops/{STOP}/departures"
NOW = datetime(2026, 10, 20, 7, 30, 20, tzinfo=BERLIN)
KEYS = ["DEPARTURES_STOP", "DEPARTURES_BASE_URL", "DEPARTURES_LINES", "DEPARTURES_DIRECTIONS", "DEPARTURES_MAX",
        "DEPARTURES_WALK_MINUTES", "DEPARTURES_SHOW_FROM", "DEPARTURES_SHOW_UNTIL", "DEPARTURES_WEEKDAYS_ONLY"]


def dep(line, product, direction, planned, when="same", delay=0, cancelled=False, platform=None, stop="Musterstadt Schulzentrum"):
    """One departure row the way the API answers it (``when`` is null when cancelled)."""
    row = {
        "tripId": f"1|{abs(hash((line, planned)))}|0|80|20102026",
        "stop": {"type": "stop", "id": STOP, "name": stop},
        "when": planned if when == "same" else when,
        "plannedWhen": planned,
        "prognosedWhen": None if when is None else (planned if when == "same" else when),
        "delay": delay,
        "platform": platform, "plannedPlatform": platform,
        "direction": direction,
        "provenance": None,
        "line": {"type": "line", "id": line.lower().replace(" ", "-"), "name": line, "mode": "bus" if product == "bus" else "train",
                 "product": product, "operator": {"type": "operator", "id": "x", "name": "Muster Verkehrsbetriebe"}},
        "remarks": [],
    }
    if cancelled:
        row.update({"cancelled": True, "when": None, "prognosedWhen": None, "delay": None})
    return row


def answer(*rows):
    return {"departures": list(rows), "realtimeDataUpdatedAt": 1792474200}


# Realistic morning at a fictional stop
MORNING = answer(
    dep("Bus 12", "bus", "Schulzentrum", "2026-10-20T07:25:00+02:00", "2026-10-20T07:25:00+02:00", 0),  # left 5 min ago
    dep("Bus 12", "bus", "Schulzentrum", "2026-10-20T07:40:00+02:00", "2026-10-20T07:42:00+02:00", 120, platform="2"),
    dep("Bus 14", "bus", "Hauptbahnhof", "2026-10-20T07:44:00+02:00", "2026-10-20T07:44:00+02:00", 0),
    dep("Bus 12", "bus", "Schulzentrum", "2026-10-20T07:50:00+02:00", None, None, cancelled=True),
    dep("RB 7", "regional", "Beispielheim", "2026-10-20T07:52:00+02:00", "2026-10-20T07:53:00+02:00", 60, platform="1"),
    dep("Bus 12", "bus", "Schulzentrum", "2026-10-20T08:10:00+02:00", "2026-10-20T08:10:00+02:00", 0),
)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DEPARTURES_STOP", STOP)
    monkeypatch.setattr(mod, "_now", lambda: NOW)
    mod.clear_caches()
    yield
    mod.clear_caches()


@pytest.fixture
def api():
    with respx.mock(using="httpx", assert_all_called=False) as service:
        yield service


def cards(user=None):
    return asyncio.run(DeparturesPlugin().today_cards(user or type("U", (), {"language": "en"})(), None))


def texts(card):
    return [(ln["text"], ln["secondary"]) for ln in card["lines"]]


# --- manifest ---

def test_manifest_is_valid_and_matches_the_code():
    manifest = validate_manifest(json.loads((ROOT / "familienhub-plugin.json").read_text()))
    plugin = DeparturesPlugin()
    assert plugin.id == manifest["id"] == "departures"
    assert manifest["entry_point"] == "fh_plugin_departures.plugin:DeparturesPlugin"
    assert manifest["min_hub_version"] == "1.0.0-beta.3" and manifest["version"] == "1.0.0"
    assert {s.key for s in plugin.settings} == {s["key"] for s in manifest["settings"]} == set(KEYS)
    assert set(plugin.capabilities) <= set(manifest["capabilities"])
    required = {s["key"] for s in manifest["settings"] if s["required"]}
    assert required == {"DEPARTURES_STOP"} == {s.key for s in plugin.settings if s.required}
    assert "v6.db.transport.rest" in manifest["hosts"] and "v6.vbb.transport.rest" in manifest["hosts"]
    defaults = {s["key"]: s.get("default") for s in manifest["settings"]}
    assert defaults["DEPARTURES_BASE_URL"] == BASE and defaults["DEPARTURES_MAX"] in (4, "4")
    assert plugin.routers() == []


def test_manifest_default_url_is_the_code_default():
    assert logic.DEFAULT_BASE_URL == BASE


# --- compatibility guard ---

def test_guard_on_an_old_hub(monkeypatch, api):
    monkeypatch.setattr(mod.importlib.util, "find_spec", lambda name: None)  # no app.plugins.hooks, as in beta.2
    plugin = DeparturesPlugin()
    assert plugin.is_configured() is False and plugin.active is False
    assert "1.0.0-beta.3" in plugin.description
    route = api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    assert asyncio.run(plugin.today_cards(object(), None)) == []
    result = asyncio.run(plugin.test_settings({"DEPARTURES_STOP": STOP}))
    assert result["ok"] is False and "1.0.0-beta.3" in result["message"]
    assert asyncio.run(plugin.check())["status"] == "error"
    assert not route.called


def test_guard_when_the_base_class_lacks_the_hooks(monkeypatch):
    monkeypatch.delattr(Plugin, "today_cards")
    assert mod.hub_problem() is not None and DeparturesPlugin().is_configured() is False


def test_guard_never_raises(monkeypatch):
    def boom(name):
        raise ValueError("broken")
    monkeypatch.setattr(mod.importlib.util, "find_spec", boom)
    assert mod.hub_problem() is not None


def test_active_on_this_hub():
    assert mod.hub_problem() is None and DeparturesPlugin().is_configured() is True
    assert hub_version_ok("1.0.0-beta.3", "1.0.0-beta.2") is False  # the hub itself refuses the install there


def test_not_configured_without_a_stop(monkeypatch):
    monkeypatch.delenv("DEPARTURES_STOP")
    assert DeparturesPlugin().is_configured() is False


# --- the card ---

def test_card_from_a_realistic_answer(api):
    route = api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    [card] = cards()
    assert card["id"] == "departures" and card["title"] == "Musterstadt Schulzentrum"
    assert card["icon"] == "directions_bus" and card["badge"] == "12 min"
    assert texts(card) == [
        ("Bus 12 → Schulzentrum · 7:42 (+2)", "in 12 min · platform 2"),
        ("Bus 14 → Hauptbahnhof · 7:44", "in 14 min"),
        ("Bus 12 → Schulzentrum · 7:50", "cancelled"),
        ("RB 7 → Beispielheim · 7:53 (+1)", "in 23 min · platform 1"),
    ]
    sent = route.calls.last.request
    assert sent.headers["User-Agent"].startswith("familienhub-plugin-departures/")
    assert sent.url.params["duration"] and sent.url.params["results"]


def test_card_survives_the_hubs_own_sanitizer(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    sanitized = sanitize_cards(DeparturesPlugin(), cards())
    assert len(sanitized) == 1 and sanitized[0]["icon"] == "directions_bus" and len(sanitized[0]["lines"]) == 4
    assert sanitized[0]["key"] == "plugin:departures:departures" and sanitized[0]["badge"] == "12 min"


def test_german_texts_from_the_users_language(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    [card] = cards(type("U", (), {"language": "de"})())
    assert card["lines"][2]["secondary"] == "fällt aus" and card["badge"] == "12 Min."
    assert card["lines"][0]["secondary"] == "in 12 Min. · Gleis 2"


def test_unknown_language_falls_back_to_german(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    [card] = cards(object())
    assert card["badge"] == "12 Min."


def test_max_departures(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_MAX", "2")
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    [card] = cards()
    assert len(card["lines"]) == 2


@pytest.mark.parametrize("value, expected", [("0", 1), ("99", 6), ("abc", 4), ("", 4), ("3.7", 3)])
def test_max_is_clamped_and_lenient_at_runtime(monkeypatch, value, expected):
    monkeypatch.setenv("DEPARTURES_MAX", value)
    assert logic.read_config(lambda k: __import__("os").environ.get(k)).max_rows == expected


def test_line_filter_accepts_full_name_or_number(api, monkeypatch):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    monkeypatch.setenv("DEPARTURES_LINES", "bus12, RB 7")
    [card] = cards()
    assert [t[0].split(" →")[0] for t in texts(card)] == ["Bus 12", "Bus 12", "RB 7", "Bus 12"]
    mod.clear_caches()
    monkeypatch.setenv("DEPARTURES_LINES", "14")
    [card] = cards()
    assert [t[0].split(" →")[0] for t in texts(card)] == ["Bus 14"]


def test_direction_filter_is_a_case_insensitive_substring(api, monkeypatch):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    monkeypatch.setenv("DEPARTURES_DIRECTIONS", "hauptbahn; BEISPIEL")
    [card] = cards()
    assert [t[0].split("→")[1].split("·")[0].strip() for t in texts(card)] == ["Hauptbahnhof", "Beispielheim"]


def test_line_and_direction_combine(api, monkeypatch):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    monkeypatch.setenv("DEPARTURES_LINES", "Bus 12")
    monkeypatch.setenv("DEPARTURES_DIRECTIONS", "Hauptbahnhof")
    assert cards() == []


def test_delay_formats(api):
    rows = answer(
        dep("Bus 1", "bus", "A", "2026-10-20T07:40:00+02:00", "2026-10-20T07:40:00+02:00", 0),
        dep("Bus 2", "bus", "B", "2026-10-20T07:41:00+02:00", "2026-10-20T07:40:00+02:00", -60),
        dep("Bus 3", "bus", "C", "2026-10-20T07:42:00+02:00", "2026-10-20T07:47:00+02:00", 300),
        dep("Bus 4", "bus", "D", "2026-10-20T07:50:00+02:00", "2026-10-20T07:55:00+02:00", None),  # delay missing: from times
    )
    api.get(DEPS).mock(return_value=httpx.Response(200, json=rows))
    [card] = cards()
    assert [t[0] for t in texts(card)] == ["Bus 1 → A · 7:40", "Bus 2 → B · 7:40 (-1)", "Bus 3 → C · 7:47 (+5)", "Bus 4 → D · 7:55 (+5)"]


def test_a_cancelled_departure_is_not_the_badge(api):
    rows = answer(dep("Bus 12", "bus", "S", "2026-10-20T07:35:00+02:00", None, None, cancelled=True),
                  dep("Bus 12", "bus", "S", "2026-10-20T07:50:00+02:00"))
    api.get(DEPS).mock(return_value=httpx.Response(200, json=rows))
    [card] = cards()
    assert card["badge"] == "20 min" and card["lines"][0]["secondary"] == "cancelled"
    assert card["lines"][0]["text"] == "Bus 12 → S · 7:35"  # planned time, no delay suffix


def test_only_cancelled_departures_still_show_a_card_without_badge(api):
    rows = answer(dep("Bus 12", "bus", "S", "2026-10-20T07:35:00+02:00", None, None, cancelled=True))
    api.get(DEPS).mock(return_value=httpx.Response(200, json=rows))
    [card] = cards()
    assert "badge" not in card


def test_departure_that_just_left_is_hidden_and_current_minute_is_now(api):
    rows = answer(dep("Bus 1", "bus", "A", "2026-10-20T07:29:00+02:00"), dep("Bus 2", "bus", "B", "2026-10-20T07:30:00+02:00"))
    api.get(DEPS).mock(return_value=httpx.Response(200, json=rows))
    [card] = cards()
    assert len(card["lines"]) == 1 and card["badge"] == "now" and card["lines"][0]["secondary"] == "now"


def test_no_card_without_departures(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=answer()))
    assert cards() == []


# --- walking time ---

def test_walking_time_hides_unreachable_and_shows_leave_in(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_WALK_MINUTES", "8")
    rows = answer(
        dep("Bus 9", "bus", "A", "2026-10-20T07:34:00+02:00"),   # in 4 min: cannot be reached
        dep("Bus 10", "bus", "B", "2026-10-20T07:38:00+02:00"),  # in 8 min: leave now
        dep("Bus 11", "bus", "C", "2026-10-20T07:45:00+02:00"),  # in 15 min: leave in 7
        dep("Bus 11", "bus", "C", "2026-10-20T07:36:00+02:00", None, None, cancelled=True),  # cancelled and unreachable
    )
    api.get(DEPS).mock(return_value=httpx.Response(200, json=rows))
    [card] = cards()
    assert texts(card) == [("Bus 10 → B · 7:38", "leave now"), ("Bus 11 → C · 7:45", "leave in 7 min")]
    assert card["badge"] == "8 min"  # the badge is the time until the departure itself


def test_walking_time_uses_the_delay(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_WALK_MINUTES", "10")
    # planned 07:38 would be reachable-not (8 min), but it is 5 min late: 07:43, leave in 3
    rows = answer(dep("Bus 9", "bus", "A", "2026-10-20T07:38:00+02:00", "2026-10-20T07:43:00+02:00", 300))
    api.get(DEPS).mock(return_value=httpx.Response(200, json=rows))
    [card] = cards()
    assert card["lines"][0]["secondary"] == "leave in 3 min"


def test_german_leave_texts(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_WALK_MINUTES", "5")
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    [card] = cards(type("U", (), {"language": "de"})())
    assert card["lines"][0]["secondary"].startswith("Losgehen in 7 Min.")


# --- time window ---

def cfg_with(**env):
    import os
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return logic.read_config(os.environ.get)
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@pytest.mark.parametrize("hhmm, frm, until, shown", [
    ("07:30", "06:30", "08:30", True),
    ("06:30", "06:30", "08:30", True),
    ("08:30", "06:30", "08:30", True),
    ("08:31", "06:30", "08:30", False),
    ("06:29", "06:30", "08:30", False),
    ("12:00", "", "", True),
    ("05:00", "06:30", "", False),
    ("23:00", "06:30", "", True),
    ("09:00", "", "08:30", False),
    ("00:10", "", "08:30", True),
    ("23:30", "22:00", "01:00", True),   # overnight window
    ("00:30", "22:00", "01:00", True),
    ("12:00", "22:00", "01:00", False),
])
def test_time_window(hhmm, frm, until, shown):
    cfg = cfg_with(DEPARTURES_SHOW_FROM=frm, DEPARTURES_SHOW_UNTIL=until)
    h, m = map(int, hhmm.split(":"))
    assert logic.in_window(NOW.replace(hour=h, minute=m), cfg) is shown


def test_card_hidden_outside_the_window_without_calling_the_api(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_SHOW_FROM", "08:00")
    monkeypatch.setenv("DEPARTURES_SHOW_UNTIL", "09:00")
    route = api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    assert cards() == [] and not route.called


def test_card_shown_inside_the_window(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_SHOW_FROM", "06:30")
    monkeypatch.setenv("DEPARTURES_SHOW_UNTIL", "08:30")
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    assert len(cards()) == 1


def test_weekdays_only(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_WEEKDAYS_ONLY", "true")
    route = api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    assert len(cards()) == 1  # Tuesday
    for day in (24, 25):  # Saturday, Sunday
        monkeypatch.setattr(mod, "_now", lambda day=day: NOW.replace(day=day))
        assert cards() == []
    assert route.call_count == 1
    cfg = logic.read_config(lambda k: "true" if k == "DEPARTURES_WEEKDAYS_ONLY" else None)
    assert logic.in_window(NOW.replace(day=23), cfg) is True  # Friday
    assert logic.in_window(NOW.replace(day=26), cfg) is True  # Monday


def test_weekend_without_the_switch_is_not_blocked():
    cfg = logic.read_config(lambda k: None)
    assert logic.in_window(NOW.replace(day=24), cfg) is True


# --- timezone ---

def test_times_are_shown_in_the_hub_timezone_whatever_the_api_sends(api):
    rows = answer(dep("Bus 1", "bus", "A", "2026-10-20T05:40:00Z", "2026-10-20T05:42:00Z", 120),  # UTC = 07:42 Berlin
                  dep("Bus 2", "bus", "B", "2026-10-20T07:50:00", "2026-10-20T07:50:00", 0),      # no offset: hub local time
                  dep("Bus 3", "bus", "C", "2026-10-20T06:55:00+01:00", "2026-10-20T06:55:00+01:00", 0))  # 07:55 Berlin
    api.get(DEPS).mock(return_value=httpx.Response(200, json=rows))
    [card] = cards()
    assert [t[0] for t in texts(card)] == ["Bus 1 → A · 7:42 (+2)", "Bus 2 → B · 7:50", "Bus 3 → C · 7:55"]
    assert card["badge"] == "12 min"


def test_hub_timezone_comes_from_the_hub_settings(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "timezone", "Europe/Lisbon")
    assert str(mod.hub_zone()) == "Europe/Lisbon"
    monkeypatch.setattr(settings, "timezone", "Not/AZone")
    assert str(mod.hub_zone()) == "UTC"


# --- errors: fail softly, never hammer ---

@pytest.mark.parametrize("answer_", [
    httpx.Response(500),
    httpx.Response(503, text="Service Unavailable"),
    httpx.Response(429, headers={"Retry-After": "30"}),
    httpx.Response(404, json={"error": True, "msg": "stop not found"}),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json={"unexpected": "shape"}),
    httpx.Response(200, json="text"),
    httpx.ConnectError("down"),
    httpx.ReadTimeout("slow"),
])
def test_broken_service_gives_no_card_and_no_exception(api, answer_):
    api.get(DEPS).mock(side_effect=[answer_])
    assert cards() == []


def test_broken_rows_are_skipped(api):
    rows = {"departures": [None, "x", {"line": {"name": "Bus 1"}}, {"plannedWhen": "garbage"},
                           dep("Bus 12", "bus", "S", "2026-10-20T07:50:00+02:00")]}
    api.get(DEPS).mock(return_value=httpx.Response(200, json=rows))
    [card] = cards()
    assert len(card["lines"]) == 1


def test_old_bare_list_answer_is_accepted(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING["departures"]))
    assert len(cards()) == 1


def test_answers_are_cached_and_failures_too(api):
    route = api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    for _ in range(5):
        assert len(cards()) == 1
    assert route.call_count == 1


def test_failures_are_cached_too(api):
    down = api.get(DEPS).mock(return_value=httpx.Response(500))
    for _ in range(5):
        assert cards() == []
    assert down.call_count == 1  # a failing instance is not hammered either


def test_cache_expires(api, monkeypatch):
    route = api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    clock = [1000.0]
    monkeypatch.setattr(mod._time, "monotonic", lambda: clock[0])
    cards()
    clock[0] += 44
    cards()
    assert route.call_count == 1
    clock[0] += 2
    cards()
    assert route.call_count == 2


def test_parallel_screen_loads_share_one_call(api):
    route = api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))

    async def run():
        return await asyncio.gather(*(DeparturesPlugin().today_cards(object(), None) for _ in range(6)))
    results = asyncio.run(run())
    assert all(len(r) == 1 for r in results) and route.call_count == 1


def test_slow_instance_does_not_block_today_but_fills_the_cache(api, monkeypatch):
    monkeypatch.setattr(mod, "CARD_WAIT", 0.05)

    async def slow(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=MORNING)
    route = api.get(DEPS).mock(side_effect=slow)

    async def run():
        plugin = DeparturesPlugin()
        first = await plugin.today_cards(object(), None)
        await asyncio.sleep(0.4)
        second = await plugin.today_cards(object(), None)
        return first, second
    first, second = asyncio.run(run())
    assert first == [] and len(second) == 1 and route.call_count == 1


def test_timeouts_are_at_most_8_seconds():
    assert mod.HTTP_TIMEOUT <= 8 and mod.CARD_WAIT < 4
    assert mod.CACHE_OK >= 30 and mod.CACHE_OK <= 60


def test_stop_id_is_url_quoted_in_the_path(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_STOP", "de:08111:6115")
    route = api.get(url__regex=r"https://v6\.db\.transport\.rest/stops/.*").mock(return_value=httpx.Response(200, json=MORNING))
    assert len(cards()) == 1
    assert route.calls.last.request.url.raw_path.startswith(b"/stops/de%3A08111%3A6115/departures")


def test_redirects_are_not_followed(api):
    api.get(DEPS).mock(return_value=httpx.Response(302, headers={"Location": "http://169.254.169.254/"}))
    assert cards() == []


# --- other instances and names ---

def test_other_instance_from_the_setting(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_BASE_URL", "https://v6.vbb.transport.rest/")
    route = api.get(f"https://v6.vbb.transport.rest/stops/{STOP}/departures").mock(return_value=httpx.Response(200, json=MORNING))
    assert len(cards()) == 1 and route.called


def test_a_stop_name_is_resolved_at_run_time_once(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_STOP", "Musterstadt Schulzentrum")
    search = api.get(f"{BASE}/locations").mock(return_value=httpx.Response(200, json=LOCATIONS))
    route = api.get(f"{BASE}/stops/8000999/departures").mock(return_value=httpx.Response(200, json=MORNING))
    assert len(cards()) == 1 and len(cards()) == 1
    assert search.call_count == 1 and route.call_count == 1
    assert search.calls.last.request.url.params["query"] == "Musterstadt Schulzentrum"


def test_unknown_name_gives_no_card(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_STOP", "Nirgendwo")
    api.get(f"{BASE}/locations").mock(return_value=httpx.Response(200, json=[]))
    assert cards() == []


# --- connection test ---

LOCATIONS = [
    {"type": "stop", "id": "8000999", "name": "Musterstadt Schulzentrum", "location": {"type": "location", "latitude": 50.1, "longitude": 8.1}},
    {"type": "stop", "id": "8000998", "name": "Musterstadt Markt"},
    {"type": "location", "id": None, "address": "Musterweg 1"},
    {"type": "stop", "id": "8000997", "name": "Musterstadt Bahnhof"},
]


def test_test_by_id_shows_name_and_next_departure(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": STOP, "DEPARTURES_BASE_URL": BASE}))
    assert r["ok"] is True and r["level"] == "ok"
    assert "Musterstadt Schulzentrum" in r["message"] and "Bus 12 → Schulzentrum · 7:42 (+2)" in r["message"]


def test_test_uses_the_typed_values_not_the_saved_ones(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_STOP", "1111111")
    monkeypatch.setenv("DEPARTURES_LINES", "Bus 14")
    route = api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": STOP, "DEPARTURES_LINES": "RB 7"}))
    assert route.called and "RB 7" in r["message"] and "Bus 14" not in r["message"]


def test_test_resolves_a_name_and_tells_the_id(api):
    search = api.get(f"{BASE}/locations").mock(return_value=httpx.Response(200, json=LOCATIONS))
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": "Musterstadt Schulzentrum"}))
    assert r["ok"] is True and r["level"] == "warn"
    assert "8000999" in r["message"] and "Musterstadt Markt = 8000998" in r["message"]
    assert search.called


def test_test_unknown_name(api):
    api.get(f"{BASE}/locations").mock(return_value=httpx.Response(200, json=[]))
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": "Nirgendwo"}))
    assert r["ok"] is False and r["level"] == "error" and "Nirgendwo" in r["message"]


def test_test_unknown_id(api):
    api.get(DEPS).mock(return_value=httpx.Response(404, json={"error": True, "msg": "stop not found"}))
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": STOP}))
    assert r["ok"] is False and "README" in r["message"] and "IDs differ" in r["message"]


def test_test_unknown_id_as_vbb_answers_it(api):
    """Seen live on v6.vbb.transport.rest: an invalid id is an HTTP 500 with this message."""
    api.get(DEPS).mock(return_value=httpx.Response(500, json={"message": "id must be an IBNR"}))
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": STOP}))
    assert r["ok"] is False and "IDs differ" in r["message"]


def test_test_no_departures_is_a_warning(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=answer()))
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": STOP}))
    assert r["ok"] is True and r["level"] == "warn" and "no departures" in r["message"]


def test_test_filters_that_match_nothing(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": STOP, "DEPARTURES_LINES": "Bus 99"}))
    assert r["ok"] is True and r["level"] == "warn" and "matches your settings" in r["message"] and "line" in r["message"]


def test_test_mentions_the_window(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    r = asyncio.run(DeparturesPlugin().test_settings({
        "DEPARTURES_STOP": STOP, "DEPARTURES_SHOW_FROM": "06:30", "DEPARTURES_SHOW_UNTIL": "08:30", "DEPARTURES_WEEKDAYS_ONLY": "true"}))
    assert "Monday to Friday between 06:30 and 08:30" in r["message"]


@pytest.mark.parametrize("answer_, text", [
    (httpx.ConnectError("down"), "not reachable"),
    (httpx.ReadTimeout("slow"), "not reachable"),
    (httpx.Response(500), "error 500"),
    (httpx.Response(429), "too many requests"),
    (httpx.Response(200, text="<html>"), "not like a transport.rest"),
    (httpx.Response(200, json={"x": 1}), "not like a transport.rest"),
])
def test_test_error_paths(api, answer_, text):
    api.get(DEPS).mock(side_effect=[answer_])
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": STOP}))
    assert r["ok"] is False or r["level"] == "warn"
    assert text in r["message"]
    assert "transport.rest" in r["message"] or "error" in r["message"] or "requests" in r["message"]
    assert BASE not in r["message"]  # no addresses in messages


def test_test_instance_unreachable_when_searching_a_name(api):
    api.get(f"{BASE}/locations").mock(side_effect=[httpx.ConnectError("down")])
    r = asyncio.run(DeparturesPlugin().test_settings({"DEPARTURES_STOP": "Musterstadt"}))
    assert r["ok"] is False and "not reachable" in r["message"]


# --- settings validation ---

@pytest.mark.parametrize("values, fragment", [
    ({}, "stop ID"),
    ({"DEPARTURES_STOP": "  "}, "stop ID"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_BASE_URL": "ftp://x"}, "http"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_BASE_URL": "not a url"}, "http"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_MAX": "0"}, "between 1 and 6"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_MAX": "9"}, "between 1 and 6"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_MAX": "x"}, "number"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_MAX": "2.5"}, "whole number"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_MAX": "nan"}, "Number of departures"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_WALK_MINUTES": "-1"}, "between 0 and 90"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_WALK_MINUTES": "500"}, "between 0 and 90"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_SHOW_FROM": "25:00"}, "06:30"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_SHOW_FROM": "6.30"}, "06:30"),
    ({"DEPARTURES_STOP": "1", "DEPARTURES_SHOW_UNTIL": "8:75"}, "06:30"),
])
def test_invalid_settings_are_explained_without_any_request(api, values, fragment):
    route = api.route().mock(return_value=httpx.Response(200, json=MORNING))
    r = asyncio.run(DeparturesPlugin().test_settings(values))
    assert r["ok"] is False and r["level"] == "error" and fragment in r["message"]
    assert not route.called


def test_valid_settings_pass_validation():
    values = {"DEPARTURES_STOP": "8000105", "DEPARTURES_BASE_URL": "", "DEPARTURES_MAX": "4", "DEPARTURES_WALK_MINUTES": "5",
              "DEPARTURES_SHOW_FROM": "6:30", "DEPARTURES_SHOW_UNTIL": "08:30", "DEPARTURES_WEEKDAYS_ONLY": "false",
              "DEPARTURES_LINES": None}
    assert logic.validate_values({k: ("" if v is None else v) for k, v in values.items()}) == []


def test_invalid_values_at_run_time_fall_back_quietly(api, monkeypatch):
    monkeypatch.setenv("DEPARTURES_SHOW_FROM", "late")
    monkeypatch.setenv("DEPARTURES_WALK_MINUTES", "far")
    monkeypatch.setenv("DEPARTURES_BASE_URL", "javascript:alert(1)")
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    assert len(cards()) == 1  # default URL, no window, no walking time


@pytest.mark.parametrize("stop, is_id", [("8000105", True), ("900000100003", True), ("de:08111:6115", True),
                                         ("Hauptbahnhof", False), ("Musterstadt Markt", False), ("Berlin Hbf", False)])
def test_id_or_name(stop, is_id):
    assert logic.looks_like_id(stop) is is_id


def test_check_for_the_admin_overview(api):
    api.get(DEPS).mock(return_value=httpx.Response(200, json=MORNING))
    assert asyncio.run(DeparturesPlugin().check()) == {"status": "ok", "message": "Reachable"}
    mod.clear_caches()
    api.get(DEPS).mock(return_value=httpx.Response(500))
    assert asyncio.run(DeparturesPlugin().check())["status"] == "error"
    assert DeparturesPlugin().health_url() is None


def test_only_plain_platform_numbers_are_shown(api):
    rows = answer(dep("M6", "tram", "A", "2026-10-20T07:40:00+02:00", platform="Pos. 15"),
                  dep("U5", "subway", "B", "2026-10-20T07:41:00+02:00", platform="1 (U5)"),
                  dep("S9", "suburban", "C", "2026-10-20T07:42:00+02:00", platform="3a"))
    api.get(DEPS).mock(return_value=httpx.Response(200, json=rows))
    [card] = cards()
    assert [t[1] for t in texts(card)] == ["in 10 min", "in 11 min", "in 12 min · platform 3a"]
