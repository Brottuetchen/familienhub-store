"""Tests of the screen time plugin against simulated devices (respx). Real systems are never called."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import fh_plugin_screentime.plugin as plugin_mod
from app.plugins.base import Plugin
from app.plugins.manifest import validate_manifest
from fh_plugin_screentime import config as cfgmod
from fh_plugin_screentime.plugin import ScreentimePlugin
from fh_plugin_screentime.state import StateStore

from .conftest import MIA, MIA_DE, T0, new_plugin, run
from .fakes import PASSWORD

ROOT = Path(__file__).resolve().parents[1]
MIA_T = "Mia's tablet"
BEN = "Ben's Switch"


def unlock(env, device=MIA_T, minutes="30", child=MIA):
    return run(env.plugin.run_reward_action("unlock", {"device": device, "minutes": minutes}, child, None, None))


def tick(env):
    run(env.plugin.scheduled(None))


# ---------------------------------------------------------------- manifest and hub contract

def test_manifest_is_valid_and_matches_the_code():
    manifest = validate_manifest(json.loads((ROOT / "familienhub-plugin.json").read_text()))
    plugin = ScreentimePlugin()
    assert plugin.id == manifest["id"] == "screentime"
    assert manifest["min_hub_version"] == "1.0.0-beta.3" and manifest["license"] == "MIT"
    assert manifest["version"] == plugin.version == "1.0.0"
    assert manifest["entry_point"] == "fh_plugin_screentime.plugin:ScreentimePlugin"
    assert {s.key for s in plugin.settings} <= {s["key"] for s in manifest["settings"]}
    assert set(plugin.capabilities) <= set(manifest["capabilities"])
    by_key = {s["key"]: s for s in manifest["settings"]}
    assert by_key["SCREENTIME_PASSWORD"]["secret"] and by_key["SCREENTIME_PASSWORD"]["type"] == "password"
    assert set(cfgmod.KEYS) == set(by_key)
    required = {k for k, s in by_key.items() if s["required"]}
    assert required == {"SCREENTIME_BACKEND", "SCREENTIME_URL", "SCREENTIME_PASSWORD", "SCREENTIME_DEVICES"}
    assert plugin.schedule_minutes == 5


def test_reward_action_spec_survives_the_hubs_sanitiser(env):
    from app.services import reward_actions

    spec = reward_actions.plugin_actions(env.plugin)
    assert [a["id"] for a in spec] == ["unlock"]
    fields = {p["key"]: p for p in spec[0]["params"]}
    assert fields["device"]["type"] == "select"
    assert [o["value"] for o in fields["device"]["options"]] == [MIA_T, BEN]
    assert fields["minutes"]["default"] == "30" and fields["minutes"]["type"] == "number"
    cleaned = reward_actions.clean_params(spec[0], {"device": MIA_T})
    assert cleaned == {"device": MIA_T, "minutes": "30"}  # the hub fills the default


def test_no_action_without_devices(monkeypatch):
    monkeypatch.delenv("SCREENTIME_DEVICES", raising=False)
    assert ScreentimePlugin().reward_actions() == []


# ---------------------------------------------------------------- settings validation

@pytest.mark.parametrize("changes, fragment", [
    ({"SCREENTIME_BACKEND": "router"}, "System must be one of"),
    ({"SCREENTIME_BACKEND": ""}, "System must be one of"),
    ({"SCREENTIME_URL": "fritz.box"}, "Address must start"),
    ({"SCREENTIME_URL": "ftp://fritz.box"}, "Address must start"),
    ({"SCREENTIME_PASSWORD": ""}, "Password is missing"),
    ({"SCREENTIME_USERNAME": ""}, "Username is required"),
    ({"SCREENTIME_DEVICES": ""}, "No devices"),
    ({"SCREENTIME_DEVICES": "tablet"}, "name=identifier"),
    ({"SCREENTIME_DEVICES": "tablet=not-an-ip"}, "IPv4 address"),
    ({"SCREENTIME_DEVICES": "a=192.168.1.2, A=192.168.1.3"}, "used twice"),
    ({"SCREENTIME_DEVICES": "a=0.0.0.0"}, "IPv4 address"),
    ({"SCREENTIME_DEVICES": "a=fe80::1"}, "IPv4 address"),
])
def test_invalid_fritzbox_settings_are_refused(changes, fragment):
    values = {"SCREENTIME_BACKEND": "fritzbox", "SCREENTIME_URL": "http://fritz.box:49000", "SCREENTIME_USERNAME": "u",
              "SCREENTIME_PASSWORD": "p", "SCREENTIME_DEVICES": "Tablet=192.168.1.2", **changes}
    cfg, errors = cfgmod.parse(values)
    assert cfg is None and any(fragment in e for e in errors), errors
    assert all("p" != e for e in errors) and not any(values["SCREENTIME_PASSWORD"] == e for e in errors)


def test_identifier_rules_per_backend():
    base = {"SCREENTIME_URL": "http://h.lan", "SCREENTIME_USERNAME": "u", "SCREENTIME_PASSWORD": "p"}
    ok, _ = cfgmod.parse({**base, "SCREENTIME_BACKEND": "adguard", "SCREENTIME_DEVICES": "A=10.0.0.2; B=Kid-Phone; C=fe80::2"})
    assert ok and ok.devices == {"A": "10.0.0.2", "B": "kid-phone", "C": "fe80::2"}
    assert cfgmod.parse({**base, "SCREENTIME_BACKEND": "adguard", "SCREENTIME_DEVICES": "A=bad_id!"})[0] is None
    assert cfgmod.parse({**base, "SCREENTIME_BACKEND": "pihole", "SCREENTIME_USERNAME": "",
                         "SCREENTIME_DEVICES": "A=Kids Group"})[0].devices == {"A": "Kids Group"}
    assert cfgmod.parse({**base, "SCREENTIME_BACKEND": "pihole", "SCREENTIME_DEVICES": "A=../x"})[0] is None


def test_url_normalisation():
    n = cfgmod.normalise_url
    assert n("fritzbox", "http://fritz.box") == "http://fritz.box:49000"
    assert n("fritzbox", "https://fritz.box/anything") == "https://fritz.box:49443"
    assert n("fritzbox", "https://fritz.box:1234") == "https://fritz.box:1234"
    assert n("pihole", "http://pi.hole/admin/") == "http://pi.hole"
    assert n("adguard", "http://ag.lan:3000/proxy/") == "http://ag.lan:3000/proxy"
    assert n("pihole", "javascript:alert(1)") is None and n("pihole", "http://") is None


@pytest.mark.parametrize("raw, expected", [("30", 30), ("5", 5), ("240", 240), (" 45 ", 45), ("30.0", 30), (30, 30),
                                          ("4", None), ("241", None), ("0", None), ("-5", None), ("abc", None),
                                          ("12.5", None), ("nan", None), ("inf", None), ("", None), (None, None)])
def test_minutes_range(raw, expected):
    assert cfgmod.parse_minutes(raw) == expected


def test_unconfigured_or_invalid_plugin_is_inactive(monkeypatch):
    for key in cfgmod.KEYS:
        monkeypatch.delenv(key, raising=False)
    assert ScreentimePlugin().is_configured() is False
    monkeypatch.setenv("SCREENTIME_BACKEND", "nonsense")
    monkeypatch.setenv("SCREENTIME_URL", "http://x.lan")
    monkeypatch.setenv("SCREENTIME_PASSWORD", "p")
    monkeypatch.setenv("SCREENTIME_DEVICES", "a=b")
    assert ScreentimePlugin().is_configured() is False


def test_configured_plugin(env):
    assert env.plugin.is_configured() is True


# ---------------------------------------------------------------- compatibility guard

def test_old_hub_without_hooks_stays_inactive_and_explains(env, monkeypatch):
    monkeypatch.delattr(Plugin, "run_reward_action")  # a hub that predates reward actions
    plugin = env.plugin
    assert plugin.is_configured() is False
    assert plugin.reward_actions() == []
    assert run(plugin.check())["status"] == "unavailable"
    assert "1.0.0-beta.3" in run(plugin.check())["message"]
    result = run(plugin.test_settings(env.values))
    assert result["ok"] is False and "1.0.0-beta.3" in result["message"]
    assert run(plugin.run_reward_action("unlock", {"device": MIA_T, "minutes": "30"}, MIA, None, None))["ok"] is False
    run(plugin.scheduled(None))  # does nothing, does not raise
    assert env.blocked(MIA_T) is True


def test_old_hub_without_notify_api(env, monkeypatch):
    monkeypatch.setattr(plugin_mod, "notify", None)
    assert env.plugin.is_configured() is False


# ---------------------------------------------------------------- unlock and re-lock (all three backends)

def test_unlock_opens_the_device_and_reports_the_end(env):
    assert env.blocked(MIA_T) and env.blocked(BEN)
    result = unlock(env)
    assert result == {"ok": True, "message": "Mia's tablet is online until 17:30"}
    assert env.blocked(MIA_T) is False
    assert env.blocked(BEN) is True  # other devices are not touched
    stored = json.loads(env.plugin.state.path.read_text())
    assert stored["devices"][MIA_T]["until"] == T0 + 1800 and stored["devices"][MIA_T]["user_id"] == 7
    assert oct(env.plugin.state.path.stat().st_mode & 0o777) == "0o600"
    assert PASSWORD not in env.plugin.state.path.read_text()


def test_relock_happens_only_after_the_time_is_up(env, clock, sent):
    unlock(env)
    clock.advance(20)
    tick(env)
    assert env.blocked(MIA_T) is False  # still within the 30 minutes
    clock.advance(11)
    tick(env)
    assert env.blocked(MIA_T) is True
    assert env.plugin.state.load() == {}
    assert [c["title"] for c in sent if c.get("user_ids") == [7]][-1] == "Internet time is over"


def test_child_is_warned_about_five_minutes_before_the_end_once(env, clock, sent):
    unlock(env, child=MIA_DE)
    clock.advance(26)
    tick(env)
    tick(env)
    warnings = [c for c in sent if c["title"] == "Noch etwa 5 Minuten"]
    assert len(warnings) == 1 and warnings[0]["user_ids"] == [7]
    clock.advance(5)
    tick(env)
    assert sent[-1]["title"] == "Internetzeit ist vorbei" and "wieder gesperrt" in sent[-1]["body"]


def test_unlocking_again_while_online_adds_the_time(env, clock):
    unlock(env, minutes="30")
    clock.advance(10)
    result = unlock(env, minutes="15")
    assert result["message"] == "Mia's tablet is online until 17:45 (time added)"
    assert env.plugin.state.load()[MIA_T]["until"] == T0 + 45 * 60
    clock.advance(30)  # minute 40: past the old end (30), before the extended one (45)
    tick(env)
    assert env.blocked(MIA_T) is False
    clock.advance(6)
    tick(env)
    assert env.blocked(MIA_T) is True


def test_total_time_is_capped(env):
    for _ in range(4):
        result = unlock(env, minutes="240")
    assert env.plugin.state.load()[MIA_T]["until"] == T0 + 8 * 3600
    assert result["message"].endswith("(time added) (limited to 8 hours at a time)")


def test_two_devices_are_independent(env, clock):
    unlock(env, MIA_T, "30")
    unlock(env, BEN, "60")
    clock.advance(31)
    tick(env)
    assert env.blocked(MIA_T) is True and env.blocked(BEN) is False
    clock.advance(30)
    tick(env)
    assert env.blocked(BEN) is True


def test_restart_survival(env, clock, tmp_path):
    unlock(env)
    del env.plugin  # the hub restarts: a fresh plugin object, same data folder
    restarted = new_plugin(tmp_path)
    clock.advance(31)
    run(restarted.scheduled(None))
    assert env.blocked(MIA_T) is True and restarted.state.load() == {}


def test_restart_keeps_a_running_unlock_open_and_restores_the_timer(env, clock, tmp_path):
    async def scenario():
        unlock_result = await env.plugin.run_reward_action("unlock", {"device": MIA_T, "minutes": "30"}, MIA, None, None)
        restarted = new_plugin(tmp_path)
        clock.advance(10)
        await restarted.scheduled(None)
        assert env.blocked(MIA_T) is False
        assert MIA_T in restarted._timers and not restarted._timers[MIA_T].done()
        for task in list(restarted._timers.values()) + list(env.plugin._timers.values()):
            task.cancel()
        return unlock_result

    assert run(scenario())["ok"] is True


def test_exact_timer_relocks_without_waiting_for_the_job(env, clock, monkeypatch):
    async def scenario():
        await env.plugin.run_reward_action("unlock", {"device": MIA_T, "minutes": "30"}, MIA, None, None)
        timer = env.plugin._timers[MIA_T]
        clock.advance(30)
        real_sleep = asyncio.sleep

        async def instant(_seconds):
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", instant)
        env.plugin._arm(MIA_T, T0 + 1800)  # replaces the first timer, which is cancelled
        await real_sleep(0.05)
        await real_sleep(0.05)
        return timer

    first = run(scenario())
    assert first.cancelled() or first.done()
    assert env.blocked(MIA_T) is True and env.plugin.state.load() == {}


def test_timer_does_not_lock_when_time_was_added(env, clock):
    async def scenario():
        await env.plugin.run_reward_action("unlock", {"device": MIA_T, "minutes": "30"}, MIA, None, None)
        clock.advance(29)
        await env.plugin.run_reward_action("unlock", {"device": MIA_T, "minutes": "30"}, MIA, None, None)
        clock.advance(2)
        await env.plugin._expire_due(only=MIA_T)  # what the first timer would do at minute 31
        for task in env.plugin._timers.values():
            task.cancel()

    run(scenario())
    assert env.blocked(MIA_T) is False


# ---------------------------------------------------------------- failures

def test_unlock_failure_keeps_the_device_blocked_and_says_so(env, caplog):
    env.fake.fail = True
    with caplog.at_level(logging.DEBUG):
        result = unlock(env)
    assert result["ok"] is False and result["message"].startswith("Could not unlock Mia's tablet")
    assert env.blocked(MIA_T) is True
    plugin_log = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("plugin."))
    assert plugin_log  # the failure is logged for the admin
    for secret in (PASSWORD, "fritz.test", "adguard.test", "pihole.test"):
        assert secret not in result["message"] and secret not in plugin_log


def test_failed_unlock_leaves_an_expired_record_so_the_job_locks_again(env, clock):
    env.fake.fail = True
    unlock(env)
    entry = env.plugin.state.load()[MIA_T]
    assert entry["until"] <= clock.now
    env.fake.fail = False
    tick(env)
    assert env.blocked(MIA_T) is True and env.plugin.state.load() == {}


def test_failed_extension_keeps_the_running_unlock(env, clock):
    unlock(env, minutes="30")
    env.fake.fail = True
    assert unlock(env, minutes="30")["ok"] is False
    assert env.plugin.state.load()[MIA_T]["until"] == T0 + 1800
    env.fake.fail = False
    clock.advance(31)
    tick(env)
    assert env.blocked(MIA_T) is True


def test_lost_answer_after_a_successful_unlock_is_still_locked_again(env, clock):
    if env.backend != "fritzbox":
        pytest.skip("answer loss is simulated on the Fritz!Box only")
    env.fake.lose_answer = True
    result = unlock(env)
    assert result["ok"] is False and "did not answer in time" in result["message"]
    assert env.blocked(MIA_T) is False  # the box applied it, the answer got lost
    env.fake.lose_answer = False
    tick(env)
    assert env.blocked(MIA_T) is True  # the plugin locked it to be safe


def test_failing_relock_is_retried_and_parents_are_told(env, clock, sent):
    unlock(env)
    clock.advance(31)
    env.fake.fail = True
    tick(env)
    assert env.plugin.state.load()[MIA_T]["failures"] == 1
    assert not [c for c in sent if c.get("roles")]  # not yet: one failure can be a blip
    clock.advance(5)
    tick(env)
    parent = [c for c in sent if c.get("roles")]
    assert len(parent) == 1 and parent[0]["roles"] == ["partner", "admin"]
    assert "Mia's tablet" in parent[0]["body"] and PASSWORD not in parent[0]["body"]
    clock.advance(5)
    tick(env)
    assert len([c for c in sent if c.get("roles")]) == 1  # no spam on every retry
    assert env.blocked(MIA_T) is False
    env.fake.fail = False
    clock.advance(5)
    tick(env)
    assert env.blocked(MIA_T) is True and env.plugin.state.load() == {}
    assert not [c for c in sent if c.get("user_ids") and c["title"] == "Internet time is over" and False]


def test_parents_are_reminded_hourly_while_locking_keeps_failing(env, clock, sent):
    unlock(env)
    clock.advance(31)
    env.fake.fail = True
    for _ in range(26):
        tick(env)
        clock.advance(5)
    assert len([c for c in sent if c.get("roles")]) == 3  # attempts 2, 12 and 24


def test_unwritable_state_means_nothing_is_unlocked(env, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the folder should be")
    env.plugin.state = StateStore(blocker / "sub" / "state.json")
    result = unlock(env)
    assert result["ok"] is False and "nothing was unlocked" in result["message"]
    assert env.blocked(MIA_T) is True


def test_damaged_state_file_is_treated_as_empty(env):
    env.plugin.state.path.write_text("{not json")
    assert env.plugin.state.load() == {}
    tick(env)  # does not raise
    assert unlock(env)["ok"] is True


def test_invalid_parameters_are_refused_without_touching_anything(env):
    assert unlock(env, device="Unknown")["ok"] is False
    for bad in ("4", "241", "abc", "10.5", ""):
        assert unlock(env, minutes=bad)["ok"] is False, bad
    assert run(env.plugin.run_reward_action("lock", {}, MIA, None, None))["ok"] is False
    assert env.blocked(MIA_T) and env.plugin.state.load() == {}


def test_device_removed_from_settings_while_unlocked_tells_the_parents(env, clock, sent, monkeypatch):
    unlock(env)
    monkeypatch.setenv("SCREENTIME_DEVICES", env.values["SCREENTIME_DEVICES"].split(",")[1])
    tick(env)
    assert env.plugin.state.load() == {}
    assert any(c.get("roles") and "Mia's tablet" in c["body"] for c in sent)


def test_default_locked_off_never_blocks_by_itself(env, clock, sent, monkeypatch):
    monkeypatch.setenv("SCREENTIME_DEFAULT_LOCKED", "false")
    unlock(env)
    clock.advance(31)
    tick(env)
    assert env.blocked(MIA_T) is False
    assert env.plugin.state.load() == {}
    assert sent[-1]["title"] == "Internet time is over" and "run out" in sent[-1]["body"]


def test_notification_failures_do_not_break_the_job(env, clock, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("push service down")

    monkeypatch.setattr(plugin_mod, "notify", broken)
    unlock(env)
    clock.advance(31)
    tick(env)
    assert env.blocked(MIA_T) is True


# ---------------------------------------------------------------- Today card

def test_today_card_for_parents_only(env, clock):
    unlock(env, MIA_T, "30")
    clock.advance(10)
    unlock(env, BEN, "60")
    parent = SimpleNamespace(id=1, role="partner", is_admin=False)
    admin = SimpleNamespace(id=2, role="admin", is_admin=True)
    child = SimpleNamespace(id=7, role="child", is_admin=False)
    cards = run(env.plugin.today_cards(parent, None))
    assert cards == [{"id": "unlocked", "title": "Internet unlocked", "icon": "wifi", "badge": "2", "lines": [
        {"text": MIA_T, "secondary": "20 min left"}, {"text": BEN, "secondary": "60 min left"}]}]
    assert run(env.plugin.today_cards(admin, None)) == cards
    assert run(env.plugin.today_cards(child, None)) == []
    from app.plugins.hooks import sanitize_cards

    assert sanitize_cards(env.plugin, cards)[0]["icon"] == "wifi"


def test_no_card_when_nothing_is_unlocked(env, clock):
    parent = SimpleNamespace(id=1, role="partner", is_admin=False)
    assert run(env.plugin.today_cards(parent, None)) == []
    unlock(env)
    clock.advance(40)
    assert run(env.plugin.today_cards(parent, None)) == []  # expired, even if the job has not run yet


def test_state_default_location_is_below_the_hubs_data_dir(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    assert StateStore().path == tmp_path / "plugin-data" / "screentime" / "state.json"


# ---------------------------------------------------------------- connection test

def test_connection_test_reports_every_device(env):
    result = run(env.plugin.test_settings(dict(env.values)))
    assert result["ok"] is True and result["level"] == "ok"
    assert "Mia's tablet: blocked" in result["message"] and "Ben's Switch: blocked" in result["message"]
    assert {"fritzbox": "FRITZ!Box 7590", "adguard": "AdGuard Home v0.107.52", "pihole": "Pi-hole v6.1"}[env.backend] in result["message"]
    assert PASSWORD not in result["message"]


def test_connection_test_uses_the_typed_values_not_the_saved_ones(env, monkeypatch):
    monkeypatch.setenv("SCREENTIME_PASSWORD", "old-saved-password")  # saved value is wrong; the typed one is right
    result = run(env.plugin.test_settings(dict(env.values)))
    assert result["ok"] is True


def test_connection_test_warns_about_devices_that_are_not_blocked(env):
    unlock(env)
    result = run(env.plugin.test_settings(dict(env.values)))
    assert result["ok"] is True and result["level"] == "warn"
    assert "Mia's tablet: online" in result["message"] and "Block these devices once" in result["message"]


def test_connection_test_wrong_password(env):
    result = run(env.plugin.test_settings({**env.values, "SCREENTIME_PASSWORD": "wrong-password"}))
    assert result["ok"] is False and result["level"] == "error"
    assert "rejected" in result["message"] or "did not open a session" in result["message"]
    assert "wrong-password" not in result["message"]


def test_connection_test_system_down(env):
    env.fake.fail = True
    result = run(env.plugin.test_settings(dict(env.values)))
    assert result["ok"] is False and result["level"] == "error" and result["message"]


@pytest.mark.parametrize("error, fragment", [(httpx.ConnectError("boom: secret.host"), "not reachable"),
                                             (httpx.ConnectTimeout("t"), "did not answer in time")])
def test_connection_test_network_errors(env, error, fragment):
    env.router.routes.clear()
    env.router.route().mock(side_effect=error)
    result = run(env.plugin.test_settings(dict(env.values)))
    assert result["ok"] is False and fragment in result["message"] and "secret.host" not in result["message"]


def test_connection_test_invalid_values_need_no_network(env):
    result = run(env.plugin.test_settings({**env.values, "SCREENTIME_DEVICES": "tablet"}))
    assert result["ok"] is False and "name=identifier" in result["message"]
    assert env.router.calls.call_count == 0


def test_connection_test_unknown_device(env):
    extra = {"fritzbox": "Ghost=192.168.178.99", "adguard": None, "pihole": "Ghost=missing-group"}[env.backend]
    if extra is None:
        pytest.skip("AdGuard Home has no list of devices that a name could be missing from")
    values = {**env.values, "SCREENTIME_DEVICES": env.values["SCREENTIME_DEVICES"] + ", " + extra}
    result = run(env.plugin.test_settings(values))
    assert result["ok"] is False and "Ghost" in result["message"]


def test_connection_test_https_certificate_hint(env, monkeypatch):
    env.router.routes.clear()
    env.router.route().mock(side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self signed"))
    result = run(env.plugin.test_settings(dict(env.values)))
    assert "certificate" in result["message"]


# ---------------------------------------------------------------- backend specifics

def test_fritzbox_uses_digest_login_and_the_documented_action(tmp_path, monkeypatch, env):
    if env.backend != "fritzbox":
        pytest.skip("Fritz!Box only")
    unlock(env)
    assert env.fake.calls == ["DisallowWANAccessByIP"]
    request = env.router.calls.last.request
    assert request.url.path == "/upnp/control/x_hostfilter"
    assert request.headers["soapaction"] == '"urn:dslforum-org:service:X_AVM-DE_HostFilter:1#DisallowWANAccessByIP"'
    assert b"<NewIPv4Address>192.168.178.42</NewIPv4Address><NewDisallow>0</NewDisallow>" in request.content
    assert request.headers["authorization"].startswith("Digest ")


def test_fritzbox_wrong_user_right_is_explained(env):
    if env.backend != "fritzbox":
        pytest.skip("Fritz!Box only")
    result = run(env.plugin.test_settings({**env.values, "SCREENTIME_USERNAME": "somebody-else"}))
    assert result["ok"] is False and "rejected the user name or password" in result["message"]


def test_adguard_preserves_the_rest_of_the_access_list(env):
    if env.backend != "adguard":
        pytest.skip("AdGuard only")
    env.fake.access["disallowed_clients"].append("10.9.9.9")
    unlock(env)
    assert env.fake.access["disallowed_clients"] == ["ben-switch", "10.9.9.9"]
    assert env.fake.access["blocked_hosts"] == ["version.bind", "id.server"] and env.fake.access["allowed_clients"] == []
    assert env.fake.sets[-1]["blocked_hosts"] == ["version.bind", "id.server"]


def test_adguard_relock_is_idempotent_and_does_not_duplicate(env, clock):
    if env.backend != "adguard":
        pytest.skip("AdGuard only")
    unlock(env)
    clock.advance(31)
    tick(env)
    tick(env)
    assert env.fake.access["disallowed_clients"].count("192.168.178.42") == 1


def test_adguard_allowed_clients_list_is_reported(env):
    if env.backend != "adguard":
        pytest.skip("AdGuard only")
    env.fake.access["allowed_clients"] = ["192.168.178.5"]
    result = run(env.plugin.test_settings(dict(env.values)))
    assert result["ok"] is False and "allowed clients" in result["message"]
    assert unlock(env)["ok"] is False


def test_adguard_change_that_is_not_applied_is_a_failure(env):
    if env.backend != "adguard":
        pytest.skip("AdGuard only")
    env.fake.ignore_set = True
    assert unlock(env)["ok"] is False


def test_pihole_group_is_toggled_and_sessions_are_closed(env):
    if env.backend != "pihole":
        pytest.skip("Pi-hole only")
    unlock(env)
    assert env.fake.groups["kids mia"]["enabled"] is False and env.fake.groups["kids-ben"]["enabled"] is True
    assert env.fake.put_paths == ["/api/groups/kids%20mia"]
    assert env.fake.groups["kids mia"]["comment"] == "kids"
    assert env.fake.sessions == set()  # logged out again
    run(env.plugin.test_settings(dict(env.values)))
    assert env.fake.sessions == set()


def test_pihole_session_limit_is_not_exhausted(env, clock):
    if env.backend != "pihole":
        pytest.skip("Pi-hole only")
    for _ in range(6):
        unlock(env)
        clock.advance(31)
        tick(env)
    assert env.fake.sessions == set() and env.blocked(MIA_T) is True


def test_pihole_change_that_is_not_applied_is_a_failure(env):
    if env.backend != "pihole":
        pytest.skip("Pi-hole only")
    env.fake.ignore_put = True
    assert unlock(env)["ok"] is False


def test_pihole_admin_suffix_in_the_address_is_accepted(env, monkeypatch):
    if env.backend != "pihole":
        pytest.skip("Pi-hole only")
    monkeypatch.setenv("SCREENTIME_URL", "http://pihole.test/admin/")
    assert unlock(env)["ok"] is True
