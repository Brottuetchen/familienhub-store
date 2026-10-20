from datetime import date

import pytest

from fh_plugin_webuntis.parsing import (normalize_server, parse_lessons, parse_timegrid, parse_weekly, untis_date,
                                        untis_time)

from .conftest import lesson

FIRST, LAST = date(2026, 10, 19), date(2026, 10, 25)


@pytest.mark.parametrize("raw, expected", [
    ("xyz.webuntis.com", "xyz.webuntis.com"),
    ("  XYZ.WebUntis.com ", "xyz.webuntis.com"),
    ("https://xyz.webuntis.com/WebUntis/?school=abc#/basic/main", "xyz.webuntis.com"),
    ("http://untis.example.org:8443/WebUntis", "untis.example.org:8443"),
    ("xyz", None), ("", None), (None, None), (42, None), ("xyz webuntis.com", None), ("user@xyz.webuntis.com", None),
    ("a..b.com", None), ("xyz.webuntis.com:99999", None), ("-bad.example.com", None),
])
def test_server_host_form(raw, expected):
    assert normalize_server(raw) == expected


def test_times_and_dates():
    assert untis_time(745) == "07:45" and untis_time(1305) == "13:05" and untis_time("800") == "08:00"
    assert untis_time(2460) is None and untis_time(None) is None and untis_time(True) is None
    assert untis_date(20261020) == date(2026, 10, 20)
    assert untis_date(20261340) is None and untis_date("x") is None


def test_status_mapping_and_notes():
    rows = [
        lesson(20261020, 800, 845),
        lesson(20261020, 850, 935, subject="English", code="cancelled", substText="Teacher ill"),
        lesson(20261020, 955, 1040, subject="Art", code="irregular", substText="Room change",
               room=None, ro=[{"id": 9, "name": "R12", "longname": "R12", "orgid": 2}]),
        lesson(20261020, 1045, 1130, subject="Music", code="irregular", info="Concert rehearsal"),
        lesson(20261020, 1135, 1220, subject="PE", lstext="Bring trainers"),
        lesson(20261020, 1225, 1310, subject="Bio", code="cancelled", lstext="Excursion"),
    ]
    out = parse_lessons(rows, FIRST, LAST)
    by_title = {x["title"]: x for x in out}
    assert by_title["Maths"]["status"] == "normal" and "note" not in by_title["Maths"]
    assert by_title["English"] | {} == {"date": "2026-10-20", "start": "08:50", "end": "09:35", "title": "English",
                                        "kind": "school", "status": "cancelled", "location": "204",
                                        "teacher": "Smith", "note": "Teacher ill"}
    assert by_title["Art"]["status"] == "substitution" and by_title["Art"]["location"] == "R12"
    assert by_title["Music"]["status"] == "changed" and by_title["Music"]["note"] == "Concert rehearsal"
    assert "note" not in by_title["PE"], "a permanent lesson text on a normal lesson is not a note"
    assert by_title["Bio"]["note"] == "Excursion"


def test_periods_from_the_time_grid():
    grid = parse_timegrid([{"day": 2, "timeUnits": [{"name": "1", "startTime": 800, "endTime": 845},
                                                   {"name": "Pause", "startTime": 845, "endTime": 850},
                                                   {"name": "2", "startTime": 850, "endTime": 935}]}])
    assert grid == {"08:00": 1, "08:50": 2}
    out = parse_lessons([lesson(20261020, 850, 935), lesson(20261020, 1000, 1040)], FIRST, LAST, periods=grid)
    assert out[0]["period"] == 2 and "period" not in out[1]


def test_unusable_rows_are_dropped_not_fatal():
    rows = ["junk", None, {"date": "x"}, lesson(20261020, 900, 845), lesson(20261101, 800, 845),
            lesson(20261020, 99999, 845), {**lesson(20261020, 800, 845), "su": [], "lstext": ""}]
    out = parse_lessons(rows, FIRST, LAST)
    assert [x["title"] for x in out] == ["?"]
    assert parse_lessons({"not": "a list"}, FIRST, LAST) == []


def test_names_fall_back_to_a_lookup_table():
    row = lesson(20261020, 800, 845)
    row["su"], row["ro"] = [{"id": 1}], [{"id": 2}]
    out = parse_lessons([row], FIRST, LAST, names={"su": {1: "Maths"}, "ro": {2: "Room 204"}})
    assert out[0]["title"] == "Maths" and out[0]["location"] == "Room 204"


def test_weekly_data_of_the_web_client():
    payload = {"data": {"result": {"data": {
        "elements": [{"type": 3, "id": 1, "name": "M", "longName": "Maths"}, {"type": 4, "id": 2, "name": "204"},
                     {"type": 2, "id": 3, "name": "SM", "longName": "Smith"}],
        "elementPeriods": {"101": [
            {"date": 20261020, "startTime": 800, "endTime": 845, "cellState": "STANDARD", "substText": "",
             "elements": [{"type": 3, "id": 1}, {"type": 4, "id": 2}, {"type": 2, "id": 3}]},
            {"date": 20261021, "startTime": 850, "endTime": 935, "cellState": "CANCEL", "lessonText": "Ill",
             "elements": [{"type": 3, "id": 1}]},
            {"date": 20261022, "startTime": 850, "endTime": 935, "cellState": "SUBSTITUTION", "substText": "Cover",
             "elements": [{"type": 3, "id": 1}, {"type": 4, "id": 2}]}]}}}}}
    out = parse_weekly(payload, 101, FIRST, LAST, periods={"08:50": 2})
    assert [(x["date"], x["status"]) for x in out] == [("2026-10-20", "normal"), ("2026-10-21", "cancelled"),
                                                     ("2026-10-22", "substitution")]
    assert out[0]["title"] == "Maths" and out[0]["location"] == "204" and out[0]["teacher"] == "Smith"
    assert out[1]["note"] == "Ill" and out[1]["period"] == 2 and out[2]["note"] == "Cover"
    with pytest.raises(ValueError):
        parse_weekly({"data": {"error": {}}}, 101, FIRST, LAST)
