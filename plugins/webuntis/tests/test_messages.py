import json
from datetime import date

from fh_plugin_webuntis.messages import State, changes, compose, lesson_key, ordinal, when_word

TODAY = date(2026, 10, 20)  # a Tuesday


def L(start, end, title, status="cancelled", period=None, **extra):
    return {"date": "2026-10-21", "start": start, "end": end, "title": title, "status": status, "period": period, **extra}


def test_ordinals():
    assert [ordinal(n, "en") for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)] == ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"]
    assert ordinal(3, "de") == "3."


def test_day_words():
    assert when_word(TODAY, TODAY, "en") == "today" and when_word(date(2026, 10, 21), TODAY, "de") == "morgen"
    assert when_word(date(2026, 10, 23), TODAY, "en") == "Fri 23/10" and when_word(date(2026, 10, 23), TODAY, "de") == "Fr. 23.10."


def test_two_cancelled_periods_become_one_range():
    day = {date(2026, 10, 21): [L("09:55", "10:40", "Maths", period=3), L("10:45", "11:30", "Maths", period=4)]}
    assert compose("Mia", day, TODAY, "en") == ("Mia: 3rd–4th period Maths cancelled tomorrow", "")
    assert compose("Mia", day, TODAY, "de") == ("Mia: 3.–4. Stunde Maths ausgefallen morgen", "")


def test_substitution_with_room():
    day = {date(2026, 10, 21): [L("08:50", "09:35", "Maths", "substitution", period=2, location="204", teacher="Smith")]}
    assert compose("Mia", day, TODAY, "en")[0] == "Mia: 2nd period substitution (Maths): Room 204, Smith tomorrow"
    assert compose("Mia", day, TODAY, "de")[0] == "Mia: 2. Stunde Vertretung (Maths): Raum 204, Smith morgen"


def test_without_period_numbers_the_time_is_used_and_long_rooms_stay():
    day = {date(2026, 10, 21): [L("08:50", "09:35", "Art", "changed", location="Art studio B")]}
    assert compose("Mia", day, TODAY, "en")[0] == "Mia: 08:50 change (Art): Art studio B tomorrow"


def test_several_changes_make_one_notification():
    day = {date(2026, 10, 20): [L("08:00", "08:45", "Maths", period=1)],
           date(2026, 10, 21): [L("08:50", "09:35", "Art", "substitution", period=2, location="R12")]}
    title, body = compose("Mia", day, TODAY, "en")
    assert title == "Mia: 2 changes"
    assert body == "1st period Maths cancelled today; 2nd period substitution (Art): R12 tomorrow" or "Room R12" in body
    assert compose("Mia", {}, TODAY, "en") is None


def test_only_cancelled_and_changed_lessons_count():
    lessons = [{**L("08:00", "08:45", "A", "normal"), "date": "2026-10-20"}, {**L("09:00", "09:45", "B"), "date": "2026-10-20"}]
    assert list(changes(lessons)) == ["2026-10-20"] and len(changes(lessons)["2026-10-20"]) == 1


def test_the_key_changes_with_the_note_the_room_and_the_status():
    base = L("08:00", "08:45", "A", "substitution", location="204", note="x")
    assert lesson_key(base) == lesson_key(dict(base))
    for field, value in (("note", "y"), ("location", "205"), ("status", "cancelled"), ("teacher", "Z")):
        assert lesson_key({**base, field: value}) != lesson_key(base)
    assert lesson_key({**base, "period": 9}) == lesson_key(base)


def test_state_round_trip_and_pruning(tmp_path):
    path = tmp_path / "plugins" / "webuntis" / "state.json"
    state = State(path)
    assert state.child(1, "101") is None
    state.set_child(1, "101", {"2026-10-18": ["old"], "2026-10-20": ["a"], "2026-10-21": ["b"]}, TODAY)
    assert state.save()
    again = State(path)
    assert again.child(1, "101") == {"2026-10-20": ["a"], "2026-10-21": ["b"]}
    assert again.child(1, "999") is None, "another person mapped to the child starts over"
    again.forget_others([2])
    assert again.child(1, "101") is None


def test_broken_state_file_starts_over(tmp_path):
    (tmp_path / "s.json").write_text("{not json")
    assert State(tmp_path / "s.json").data == {"version": 1, "children": {}}
    (tmp_path / "s.json").write_text(json.dumps([1, 2]))
    assert State(tmp_path / "s.json").data["children"] == {}


def test_unwritable_state_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert State(blocker / "sub" / "state.json").save() is False
