"""Builders for service answers. Shapes follow the Bright Sky documentation (alerts) and the DWD file (pollen)."""

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
ALERTS_URL = "https://api.brightsky.dev/alerts"
POLLEN_URL = "https://opendata.dwd.de/climate_environment/health/alerts/s31fg.json"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

EVENTS = {  # event_code: (severity, event_en, event_de)
    22: ("minor", "frost", "FROST"),
    51: ("moderate", "wind gusts", "WINDBÖEN"),
    52: ("severe", "storm gusts", "STURMBÖEN"),
    33: ("severe", "severe thunderstorm", "SCHWERES GEWITTER"),
}


def alert(code=51, ident=None, start_h=-1, end_h=6, severity=None, status="actual", **extra):
    sev, en, de = EVENTS[code]
    a = {
        "id": 1000 + code, "alert_id": ident or f"2.49.0.0.276.0.DWD.PVW.{code}.test", "status": status,
        "effective": (NOW - timedelta(hours=2)).isoformat(),
        "onset": (NOW + timedelta(hours=start_h)).isoformat(),
        "expires": (NOW + timedelta(hours=end_h)).isoformat() if end_h is not None else None,
        "category": "met", "response_type": "prepare", "urgency": "immediate", "severity": severity or sev,
        "certainty": "likely", "event_code": code, "event_en": en, "event_de": de,
        "headline_en": f"Official WARNING of {en.upper()}", "headline_de": f"Amtliche WARNUNG vor {de}",
        "description_en": f"There is a risk of {en}. Be careful.", "description_de": f"Es treten {de} auf. Vorsicht.",
        "instruction_en": "Be careful.", "instruction_de": "Vorsicht.",
    }
    a.update(extra)
    return a


def alerts_payload(*alerts, state="Hessen", name="Frankfurt am Main"):
    return {"alerts": list(alerts), "location": {"warn_cell_id": 1, "name": name, "name_short": name,
                                                  "district": "x", "state": state, "state_short": "HE"}}


def pollen_payload(**levels):
    """The recorded DWD file with chosen values: ``pollen_payload(Birke=("3", "2"))`` sets today/tomorrow for the
    given types in every region."""
    data = json.loads((FIXTURES / "s31fg.json").read_text())
    data = copy.deepcopy(data)
    for entry in data["content"]:
        for key, (today, tomorrow) in levels.items():
            entry["Pollen"][key]["today"], entry["Pollen"][key]["tomorrow"] = today, tomorrow
    return data


def region_pollen(partregion_id, **levels):
    """Like pollen_payload, but only for one partregion (others stay at 0)."""
    data = json.loads((FIXTURES / "s31fg.json").read_text())
    for entry in data["content"]:
        if entry["partregion_id"] == partregion_id:
            for key, (today, tomorrow) in levels.items():
                entry["Pollen"][key]["today"], entry["Pollen"][key]["tomorrow"] = today, tomorrow
    return data
