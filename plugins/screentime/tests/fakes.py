"""Simulations of the three systems, as respx side effects. They behave like the real thing (login, state, errors);
the tests decide nothing through them: they only answer requests and remember what was set."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Dict, List, Optional

import httpx

USER, PASSWORD = "plugin-user", "s3cret-pass"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()  # noqa: S324 - RFC 2617 digest, as the Fritz!Box uses


class FakeFritz:
    """TR-064: digest login, DeviceInfo:GetInfo, HostFilter:GetWANAccessByIP / DisallowWANAccessByIP."""

    REALM, NONCE = "F!Box SOAP-Auth", "7a1c9e"

    def __init__(self, devices: Dict[str, bool]) -> None:
        self.disallow = dict(devices)  # ip -> disallowed
        self.calls: List[str] = []
        self.fail = False  # answer 503 to everything
        self.lose_answer = False  # apply a change, then time out (the answer is lost)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(503)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Digest "):
            return httpx.Response(401, headers={"WWW-Authenticate": (
                f'Digest realm="{self.REALM}", nonce="{self.NONCE}", algorithm=MD5, qop="auth"')})
        fields = dict(re.findall(r'(\w+)="?([^",]*)"?', auth[len("Digest "):]))
        ha1 = _md5(f"{USER}:{self.REALM}:{PASSWORD}")
        ha2 = _md5(f"{request.method}:{fields.get('uri', '')}")
        expected = _md5(f"{ha1}:{fields.get('nonce')}:{fields.get('nc')}:{fields.get('cnonce')}:auth:{ha2}")
        if fields.get("username") != USER or fields.get("response") != expected:
            return httpx.Response(401, headers={"WWW-Authenticate": (
                f'Digest realm="{self.REALM}", nonce="{self.NONCE}", algorithm=MD5, qop="auth"')})
        action = request.headers["soapaction"].strip('"').split("#")[1]
        body = request.content.decode()
        self.calls.append(action)
        if action == "GetInfo":
            return self._ok(action, "<NewModelName>FRITZ!Box 7590</NewModelName>")
        ip = re.search(r"<NewIPv4Address>([^<]+)</NewIPv4Address>", body).group(1)
        if ip not in self.disallow:
            return httpx.Response(500, content=(
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault><detail>'
                '<UPnPError xmlns="urn:dslforum-org:control-1-0"><errorCode>714</errorCode></UPnPError>'
                "</detail></s:Fault></s:Body></s:Envelope>").encode())
        if action == "GetWANAccessByIP":
            flag = self.disallow[ip]
            return self._ok(action, f"<NewDisallow>{int(flag)}</NewDisallow><NewWANAccess>"
                                    f"{'denied' if flag else 'granted'}</NewWANAccess>")
        if action == "DisallowWANAccessByIP":
            self.disallow[ip] = re.search(r"<NewDisallow>(\d)</NewDisallow>", body).group(1) == "1"
            if self.lose_answer:
                raise httpx.ReadTimeout("answer lost")
            return self._ok(action, "")
        return httpx.Response(500)

    @staticmethod
    def _ok(action: str, inner: str) -> httpx.Response:
        return httpx.Response(200, content=(
            '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            f'<u:{action}Response xmlns:u="urn:dslforum-org:service:X">{inner}</u:{action}Response>'
            "</s:Body></s:Envelope>").encode())


class FakeAdGuard:
    def __init__(self, disallowed: Optional[List[str]] = None, allowed: Optional[List[str]] = None) -> None:
        self.access = {"allowed_clients": allowed or [], "disallowed_clients": disallowed or [],
                       "blocked_hosts": ["version.bind", "id.server"]}
        self.sets: List[dict] = []
        self.ignore_set = False
        self.fail = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(502)
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401)
        path = request.url.path
        if path == "/control/status":
            return httpx.Response(200, json={"version": "v0.107.52", "running": True})
        if path == "/control/access/list":
            return httpx.Response(200, json=self.access)
        if path == "/control/access/set" and request.method == "POST":
            data = json.loads(request.content)
            self.sets.append(data)
            if not self.ignore_set:
                self.access = data
            return httpx.Response(200)
        return httpx.Response(404)


class FakePiHole:
    """Pi-hole v6: POST /api/auth, groups, DELETE /api/auth. At most 2 sessions at once, like the real one's limit."""

    def __init__(self, groups: Dict[str, bool]) -> None:
        self.groups = {name: {"name": name, "comment": "kids", "enabled": enabled, "id": i}
                       for i, (name, enabled) in enumerate(groups.items(), 1)}
        self.sessions: set = set()
        self.logins = 0
        self.fail = False
        self.ignore_put = False
        self.put_paths: List[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(500)
        path, method = request.url.path, request.method
        if path == "/api/auth" and method == "POST":
            if json.loads(request.content).get("password") != PASSWORD:
                return httpx.Response(401, json={"session": {"valid": False, "sid": None}})
            if len(self.sessions) >= 2:
                return httpx.Response(429)
            self.logins += 1
            sid = f"sid{self.logins}"
            self.sessions.add(sid)
            return httpx.Response(200, json={"session": {"valid": True, "sid": sid, "validity": 300}})
        sid = request.headers.get("x-ftl-sid")
        if sid not in self.sessions:
            return httpx.Response(401, json={"error": {"key": "unauthorized"}})
        if path == "/api/auth" and method == "DELETE":
            self.sessions.discard(sid)
            return httpx.Response(204)
        if path == "/api/info/version":
            return httpx.Response(200, json={"version": {"core": {"local": {"version": "v6.1"}}}})
        if path == "/api/groups" and method == "GET":
            return httpx.Response(200, json={"groups": list(self.groups.values())})
        if path.startswith("/api/groups/") and method == "PUT":
            self.put_paths.append(request.url.raw_path.decode())
            data = json.loads(request.content)
            name = httpx.URL(str(request.url)).path[len("/api/groups/"):]
            from urllib.parse import unquote

            name = unquote(name)
            if name not in self.groups:
                return httpx.Response(404)
            if not self.ignore_put:
                self.groups[name]["enabled"] = data["enabled"]
            return httpx.Response(200, json={"groups": [self.groups[name]], "processed": {"errors": [], "success": []}})
        return httpx.Response(404)
