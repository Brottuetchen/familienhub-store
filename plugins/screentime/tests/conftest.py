"""Fixtures: a plugin wired to a simulated Fritz!Box / AdGuard Home / Pi-hole, a fake clock and a recorder for the
hub's notifications. Run with the hub's backend on the path:

    DATA_DIR=$(mktemp -d) PYTHONPATH=/path/to/familienhub/backend pytest
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import respx

import fh_plugin_screentime.plugin as plugin_mod
from fh_plugin_screentime.plugin import ScreentimePlugin
from fh_plugin_screentime.state import StateStore

from .fakes import PASSWORD, USER, FakeAdGuard, FakeFritz, FakePiHole

# 2026-10-20 15:00:00 UTC = 17:00 in Europe/Berlin (summer time still)
T0 = 1792508400.0
MIA = SimpleNamespace(id=7, language="en", role="child")
MIA_DE = SimpleNamespace(id=7, language="de", role="child")


def run(coro):
    return asyncio.run(coro)


class Clock:
    now = T0

    def advance(self, minutes: float) -> None:
        self.now += minutes * 60


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(plugin_mod, "time", SimpleNamespace(time=lambda: c.now))
    return c


@pytest.fixture
def sent(monkeypatch):
    """Recorder instead of the hub's notification service (the hub is not running in these tests)."""
    calls = []

    async def fake_notify(plugin_id, **kwargs):
        calls.append({"plugin_id": plugin_id, **kwargs})
        return {"recipients": 1, "delivered": 1, "sent": True, "reason": None}

    monkeypatch.setattr(plugin_mod, "notify", fake_notify)
    return calls


class Env:
    def __init__(self, backend, plugin, fake, devices, values, router):
        self.backend, self.plugin, self.fake, self.devices, self.values, self.router = (
            backend, plugin, fake, devices, values, router)

    def blocked(self, name):
        ident = self.devices[name]
        if self.backend == "fritzbox":
            return self.fake.disallow[ident]
        if self.backend == "adguard":
            return ident.lower() in [x.lower() for x in self.fake.access["disallowed_clients"]]
        return self.fake.groups[ident]["enabled"]

    def set_env(self, monkeypatch, **changes):
        values = {**self.values, **changes}
        for key, value in values.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        return values


BACKENDS = {
    "fritzbox": dict(url="http://fritz.test:49000", host="fritz.test", devices="Mia's tablet=192.168.178.42, Ben's Switch=192.168.178.50",
                     user=USER, make=lambda: FakeFritz({"192.168.178.42": True, "192.168.178.50": True})),
    "adguard": dict(url="http://adguard.test:3000", host="adguard.test", devices="Mia's tablet=192.168.178.42, Ben's Switch=Ben-Switch",
                    user=USER, make=lambda: FakeAdGuard(["192.168.178.42", "ben-switch"])),
    "pihole": dict(url="http://pihole.test", host="pihole.test", devices="Mia's tablet=kids mia, Ben's Switch=kids-ben",
                   user="", make=lambda: FakePiHole({"kids mia": True, "kids-ben": True})),
}


@pytest.fixture(params=list(BACKENDS))
def env(request, monkeypatch, tmp_path, clock, sent):
    spec = BACKENDS[request.param]
    fake = spec["make"]()
    router = respx.mock(using="httpx", assert_all_called=False)
    router.start()
    router.route(host=spec["host"]).mock(side_effect=fake)
    request.addfinalizer(router.stop)
    values = {
        "SCREENTIME_BACKEND": request.param, "SCREENTIME_URL": spec["url"], "SCREENTIME_USERNAME": spec["user"] or None,
        "SCREENTIME_PASSWORD": PASSWORD, "SCREENTIME_DEVICES": spec["devices"],
        "SCREENTIME_DEFAULT_LOCKED": "true", "SCREENTIME_VERIFY_TLS": "true",
    }
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    plugin = ScreentimePlugin(StateStore(tmp_path / "state.json"))
    devices = {"Mia's tablet": spec["devices"].split(",")[0].split("=")[1].strip(),
               "Ben's Switch": spec["devices"].split(",")[1].split("=")[1].strip()}
    if request.param == "adguard":
        devices["Ben's Switch"] = "ben-switch"
    return Env(request.param, plugin, fake, devices, values, router)


def new_plugin(tmp_path) -> ScreentimePlugin:
    return ScreentimePlugin(StateStore(tmp_path / "state.json"))
