"""Reading and validating the plugin's settings. No network, no hub imports."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

BACKENDS = ("fritzbox", "adguard", "pihole")
MIN_MINUTES, MAX_MINUTES, DEFAULT_MINUTES = 5, 240, 30

KEYS = ("SCREENTIME_BACKEND", "SCREENTIME_URL", "SCREENTIME_USERNAME", "SCREENTIME_PASSWORD",
        "SCREENTIME_DEVICES", "SCREENTIME_DEFAULT_LOCKED", "SCREENTIME_VERIFY_TLS")

_NAME_RE = re.compile(r"^[^=,;\x00-\x1f\x7f]{1,40}$")
_CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_GROUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")
_URL_RE = re.compile(r"^https?://[^\s/?#@]+(/\S*)?$")


@dataclass(frozen=True)
class Config:
    backend: str
    url: str
    username: str
    password: str
    devices: Dict[str, str]  # display name -> identifier in the backend
    default_locked: bool = True
    verify_tls: bool = True


def _text(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    return value.strip() if isinstance(value, str) else ""


def _flag(values: Mapping[str, Any], key: str, default: bool) -> bool:
    value = values.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return default


def normalise_url(backend: str, raw: str) -> Optional[str]:
    """Base address without trailing slash. Fritz!Box: only scheme, host and port (default TR-064 ports added);
    Pi-hole: a trailing ``/admin`` is dropped. None if the address is unusable."""
    raw = raw.strip()
    if not _URL_RE.match(raw):
        return None
    parts = urlsplit(raw)
    if not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if backend == "fritzbox":
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        return f"{parts.scheme}://{host}:{port or (49443 if parts.scheme == 'https' else 49000)}"
    path = parts.path.rstrip("/")
    if backend == "pihole" and path.lower().endswith("/admin"):
        path = path[: -len("/admin")]
    return f"{parts.scheme}://{parts.netloc}{path}"


def _identifier(backend: str, raw: str) -> Optional[str]:
    raw = raw.strip()
    if backend == "fritzbox":
        try:
            ip = ipaddress.IPv4Address(raw)
        except ValueError:
            return None
        return None if ip.is_unspecified or ip.is_loopback or ip.is_multicast else str(ip)
    if backend == "adguard":
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            pass
        return raw.lower() if _CLIENT_ID_RE.match(raw.lower()) else None
    return raw if _GROUP_RE.match(raw) else None  # pihole: name of a group


def parse_devices(backend: str, raw: str) -> Tuple[Dict[str, str], List[str]]:
    devices: Dict[str, str] = {}
    errors: List[str] = []
    seen = set()
    for chunk in re.split(r"[,;]", raw):
        if not chunk.strip():
            continue
        name, sep, ident = chunk.partition("=")
        name = " ".join(name.split())
        if not sep or not _NAME_RE.match(name):
            errors.append(f"Device entry '{chunk.strip()[:40]}' must look like name=identifier")
            continue
        value = _identifier(backend, ident)
        if value is None:
            hint = {"fritzbox": "an IPv4 address", "adguard": "an IP address or a ClientID (a-z, 0-9, -)",
                    "pihole": "the name of a Pi-hole group"}.get(backend, "an identifier")
            errors.append(f"Device '{name}': the identifier must be {hint}")
            continue
        if name.lower() in seen:
            errors.append(f"Device name '{name}' is used twice")
            continue
        seen.add(name.lower())
        devices[name] = value
    if not devices and not errors:
        errors.append("No devices configured")
    if len(devices) > 20:
        errors.append("At most 20 devices")
    return devices, errors


def parse(values: Mapping[str, Any]) -> Tuple[Optional[Config], List[str]]:
    """The validated configuration, or None plus human-readable problems (never containing the password)."""
    errors: List[str] = []
    backend = _text(values, "SCREENTIME_BACKEND").lower()
    if backend not in BACKENDS:
        errors.append("System must be one of: " + ", ".join(BACKENDS))
        backend = ""
    url = normalise_url(backend or "pihole", _text(values, "SCREENTIME_URL"))
    if url is None:
        errors.append("Address must start with http:// or https://")
    username = _text(values, "SCREENTIME_USERNAME")
    password = values.get("SCREENTIME_PASSWORD")
    password = password if isinstance(password, str) else ""
    if not password:
        errors.append("Password is missing")
    if backend in ("fritzbox", "adguard") and not username:
        errors.append("Username is required for this system")
    devices: Dict[str, str] = {}
    if backend:
        devices, dev_errors = parse_devices(backend, _text(values, "SCREENTIME_DEVICES"))
        errors += dev_errors
    elif not _text(values, "SCREENTIME_DEVICES"):
        errors.append("No devices configured")
    if errors or not backend or url is None:
        return None, errors
    return Config(backend, url, username, password, devices, _flag(values, "SCREENTIME_DEFAULT_LOCKED", True),
                  _flag(values, "SCREENTIME_VERIFY_TLS", True)), []


def parse_minutes(raw: Any) -> Optional[int]:
    """A whole number of minutes between 5 and 240, else None."""
    try:
        number = float(str(raw).strip())
    except ValueError:
        return None
    if not number.is_integer():
        return None
    return int(number) if MIN_MINUTES <= number <= MAX_MINUTES else None
