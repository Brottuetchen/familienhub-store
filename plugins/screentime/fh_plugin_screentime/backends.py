"""The three ways of blocking a device. Each backend talks to one system and raises ``BackendError`` with a short,
safe message (never an address, a password or a response body) when anything goes wrong.

Backend contract (``blocked`` means: the device has no internet access):

- ``probe()``                 reach and log in; returns a short description such as "AdGuard Home v0.107".
- ``device_state(name)``      True = blocked, False = open; raises if the system does not know the device.
- ``set_blocked(name, bool)`` block or unblock one configured device and verify where the system allows it.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from .config import Config

REQUEST_TIMEOUT = httpx.Timeout(6.0, connect=4.0)  # per request, well below the hub's own limits


class BackendError(Exception):
    """A failure with a message that is safe to show to a parent."""


def make_client(cfg: Config, **kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT, verify=cfg.verify_tls, follow_redirects=False, **kwargs)


async def send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """One request; transport problems become ``BackendError`` without leaking the exception text."""
    try:
        return await client.request(method, url, **kwargs)
    except httpx.TimeoutException:
        raise BackendError("The system did not answer in time") from None
    except httpx.HTTPError as exc:
        if "CERTIFICATE" in str(exc).upper() or "SSL" in str(exc).upper():
            raise BackendError("HTTPS certificate not accepted (use http:// or switch off the certificate check)") from None
        raise BackendError("The system is not reachable (address, port, network)") from None


class Backend:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def probe(self) -> str:
        raise NotImplementedError

    async def device_state(self, name: str) -> bool:
        raise NotImplementedError

    async def set_blocked(self, name: str, blocked: bool) -> None:
        raise NotImplementedError

    def ident(self, name: str) -> str:
        return self.cfg.devices[name]


# --------------------------------------------------------------------------------------------------------------
# Fritz!Box: TR-064, service X_AVM-DE_HostFilter. Documented by AVM; ASSUMED here (not tested on a real box):
# the exact action names, that the box answers "unknown IP" with a SOAP fault, and the user right needed.
# --------------------------------------------------------------------------------------------------------------

FRITZ_SERVICE = "urn:dslforum-org:service:X_AVM-DE_HostFilter:1"
FRITZ_CONTROL = "/upnp/control/x_hostfilter"
_ENVELOPE = ('<?xml version="1.0" encoding="utf-8"?>'
             '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
             's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
             '<u:{action} xmlns:u="{service}">{args}</u:{action}></s:Body></s:Envelope>')


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_values(body: bytes) -> Dict[str, str]:
    """Text of every leaf element by local name. Refuses documents with a DTD (no entity tricks)."""
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise BackendError("Unexpected answer from the Fritz!Box")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise BackendError("Unexpected answer from the Fritz!Box") from None
    return {_local(e.tag): (e.text or "").strip() for e in root.iter() if len(e) == 0}


class FritzBox(Backend):
    async def _call(self, client: httpx.AsyncClient, action: str, args: Dict[str, str], *,
                    service: str = FRITZ_SERVICE, control: str = FRITZ_CONTROL) -> Dict[str, str]:
        xml_args = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())  # values are validated IPs / 0 / 1
        response = await send(
            client, "POST", self.cfg.url + control,
            content=_ENVELOPE.format(action=action, service=service, args=xml_args).encode(),
            headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'},
            auth=httpx.DigestAuth(self.cfg.username, self.cfg.password))
        if response.status_code == 401:
            raise BackendError("The Fritz!Box rejected the user name or password (or the user lacks the "
                               "'FRITZ!Box settings' right)")
        if response.status_code == 404:
            raise BackendError("The Fritz!Box does not offer this service (switch on 'Allow access for applications' "
                               "in Home Network > Network > Network Settings)")
        values = _xml_values(response.content)
        if response.status_code == 500:
            code = values.get("errorCode", "")
            raise BackendError("The Fritz!Box refused the request"
                               + (f" (TR-064 error {code})" if re.fullmatch(r"\d{1,4}", code) else ""))
        if response.status_code != 200:
            raise BackendError(f"The Fritz!Box answered with HTTP {response.status_code}")
        return values

    async def probe(self) -> str:
        # DeviceInfo:GetInfo needs the login, so this proves address, TR-064 switch, user and password at once
        async with make_client(self.cfg) as client:
            values = await self._call(client, "GetInfo", {}, service="urn:dslforum-org:service:DeviceInfo:1",
                                      control="/upnp/control/deviceinfo")
        model = values.get("NewModelName", "")
        if not re.fullmatch(r"[A-Za-z0-9 ._!-]{1,40}", model):
            return "Fritz!Box"
        return model if model.lower().startswith("fritz") else f"Fritz!Box {model}"

    async def device_state(self, name: str) -> bool:
        async with make_client(self.cfg) as client:
            values = await self._call(client, "GetWANAccessByIP", {"NewIPv4Address": self.ident(name)})
        if values.get("NewDisallow") in ("0", "1"):
            return values["NewDisallow"] == "1"
        if values.get("NewWANAccess", "").lower() in ("granted", "denied"):
            return values["NewWANAccess"].lower() == "denied"
        raise BackendError("The Fritz!Box gave no usable state for this device")

    async def set_blocked(self, name: str, blocked: bool) -> None:
        async with make_client(self.cfg) as client:
            await self._call(client, "DisallowWANAccessByIP",
                             {"NewIPv4Address": self.ident(name), "NewDisallow": "1" if blocked else "0"})


# --------------------------------------------------------------------------------------------------------------
# AdGuard Home: /control/access/{list,set}. "Disallowed clients" make AdGuard refuse every DNS query of that
# client, so all name lookups fail. The access list is replaced as a whole by /control/access/set, so we read it,
# change only our entry, and write it back.
# --------------------------------------------------------------------------------------------------------------


class AdGuard(Backend):
    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.cfg.username, self.cfg.password)

    async def _get_access(self, client: httpx.AsyncClient) -> Dict[str, List[str]]:
        response = await send(client, "GET", self.cfg.url + "/control/access/list", auth=self._auth())
        self._check(response)
        try:
            data = response.json()
        except ValueError:
            raise BackendError("Unexpected answer from AdGuard Home") from None
        if not isinstance(data, dict):
            raise BackendError("Unexpected answer from AdGuard Home")
        out = {k: [str(x) for x in data.get(k) or [] if isinstance(x, (str, int))]
               for k in ("allowed_clients", "disallowed_clients", "blocked_hosts")}
        return out

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise BackendError("AdGuard Home rejected the user name or password")
        if response.status_code != 200:
            raise BackendError(f"AdGuard Home answered with HTTP {response.status_code}")

    async def probe(self) -> str:
        async with make_client(self.cfg) as client:
            response = await send(client, "GET", self.cfg.url + "/control/status", auth=self._auth())
            self._check(response)
            try:
                version = str(response.json().get("version", ""))
            except (ValueError, AttributeError):
                raise BackendError("Unexpected answer from AdGuard Home") from None
            access = await self._get_access(client)
        if access["allowed_clients"]:
            raise BackendError("AdGuard Home has an 'allowed clients' list. Unlocking would not work; the plugin only "
                               "manages the 'disallowed clients' list. Empty the allowed list first")
        return "AdGuard Home" + (f" {version}" if re.fullmatch(r"v?[0-9][0-9A-Za-z.+-]{0,20}", version) else "")

    async def device_state(self, name: str) -> bool:
        async with make_client(self.cfg) as client:
            access = await self._get_access(client)
        return self.ident(name).lower() in (x.lower() for x in access["disallowed_clients"])

    async def set_blocked(self, name: str, blocked: bool) -> None:
        ident = self.ident(name).lower()
        async with make_client(self.cfg) as client:
            access = await self._get_access(client)
            if access["allowed_clients"]:
                raise BackendError("AdGuard Home has an 'allowed clients' list; the plugin cannot manage it")
            kept = [x for x in access["disallowed_clients"] if x.lower() != ident]
            access["disallowed_clients"] = kept + [ident] if blocked else kept
            response = await send(client, "POST", self.cfg.url + "/control/access/set", json=access, auth=self._auth())
            self._check(response)
            after = await self._get_access(client)  # verify: AdGuard must really hold the new list
        if (ident in (x.lower() for x in after["disallowed_clients"])) != blocked:
            raise BackendError("AdGuard Home did not apply the change")


# --------------------------------------------------------------------------------------------------------------
# Pi-hole v6: REST API with an app password. Every device has its own Pi-hole *group* that blocks everything for
# the clients in it (a deny regex ".*" assigned to that group). Enabling the group = blocked, disabling = open.
# ASSUMED (not tested on a real Pi-hole): header name X-FTL-SID, PUT /api/groups/{name} applying at once.
# --------------------------------------------------------------------------------------------------------------


class PiHole(Backend):
    async def _login(self, client: httpx.AsyncClient) -> Dict[str, str]:
        response = await send(client, "POST", self.cfg.url + "/api/auth", json={"password": self.cfg.password})
        if response.status_code == 404:
            raise BackendError("This is not a Pi-hole v6 API (Pi-hole v5 is not supported)")
        if response.status_code in (401, 403):
            raise BackendError("Pi-hole rejected the (app) password")
        if response.status_code == 429:
            raise BackendError("Pi-hole refuses more logins for now (too many sessions)")
        try:
            session = response.json().get("session", {})
            sid = session.get("sid")
            valid = session.get("valid") is True
        except (ValueError, AttributeError):
            raise BackendError("Unexpected answer from Pi-hole") from None
        if response.status_code != 200 or not valid or not isinstance(sid, str) or not sid:
            raise BackendError("Pi-hole did not open a session (wrong password, or two-factor login without an app password)")
        return {"X-FTL-SID": sid}

    async def _logout(self, client: httpx.AsyncClient, headers: Dict[str, str]) -> None:
        try:  # frees one of Pi-hole's few session slots; best effort
            await client.request("DELETE", self.cfg.url + "/api/auth", headers=headers)
        except httpx.HTTPError:
            pass

    async def _groups(self, client: httpx.AsyncClient, headers: Dict[str, str]) -> List[Dict[str, Any]]:
        response = await send(client, "GET", self.cfg.url + "/api/groups", headers=headers)
        if response.status_code in (401, 403):
            raise BackendError("Pi-hole rejected the session")
        try:
            groups = response.json().get("groups")
        except (ValueError, AttributeError):
            groups = None
        if response.status_code != 200 or not isinstance(groups, list):
            raise BackendError("Unexpected answer from Pi-hole")
        return [g for g in groups if isinstance(g, dict)]

    def _find(self, groups: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
        wanted = self.ident(name)
        for group in groups:
            if group.get("name") == wanted:
                return group
        raise BackendError(f"Pi-hole has no group named '{wanted}'")

    async def probe(self) -> str:
        async with make_client(self.cfg) as client:
            headers = await self._login(client)
            try:
                response = await send(client, "GET", self.cfg.url + "/api/info/version", headers=headers)
                if response.status_code != 200:
                    raise BackendError(f"Pi-hole answered with HTTP {response.status_code}")
                try:
                    version = str(response.json()["version"]["core"]["local"]["version"])
                except (ValueError, KeyError, TypeError):
                    version = ""
            finally:
                await self._logout(client, headers)
        return "Pi-hole" + (f" {version}" if re.fullmatch(r"v?[0-9][0-9A-Za-z.+-]{0,20}", version) else "")

    async def device_state(self, name: str) -> bool:
        async with make_client(self.cfg) as client:
            headers = await self._login(client)
            try:
                return self._find(await self._groups(client, headers), name).get("enabled") is True
            finally:
                await self._logout(client, headers)

    async def set_blocked(self, name: str, blocked: bool) -> None:
        async with make_client(self.cfg) as client:
            headers = await self._login(client)
            try:
                group = self._find(await self._groups(client, headers), name)
                body = {"name": group["name"], "comment": group.get("comment"), "enabled": blocked}
                response = await send(client, "PUT", self.cfg.url + "/api/groups/" + quote(str(group["name"]), safe=""),
                                      json=body, headers=headers)
                if response.status_code in (401, 403):
                    raise BackendError("Pi-hole rejected the session")
                if response.status_code != 200:
                    raise BackendError(f"Pi-hole answered with HTTP {response.status_code}")
                try:
                    errors = (response.json().get("processed") or {}).get("errors") or []
                except (ValueError, AttributeError):
                    errors = []
                if errors:
                    raise BackendError("Pi-hole refused to change the group")
                after = self._find(await self._groups(client, headers), name)  # verify
                if (after.get("enabled") is True) != blocked:
                    raise BackendError("Pi-hole did not apply the change")
            finally:
                await self._logout(client, headers)


def make_backend(cfg: Config) -> Backend:
    return {"fritzbox": FritzBox, "adguard": AdGuard, "pihole": PiHole}[cfg.backend](cfg)


async def run(coro, timeout: float = 15.0):
    """Whole backend operation with an upper limit (several requests may be involved)."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        raise BackendError("The system did not answer in time") from None
