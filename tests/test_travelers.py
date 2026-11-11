import json
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from src.travelers import (
    Traveler,
    TravelerFileError,
    age_on,
    check_travelers_for_flight,
    load_travelers,
)

BASE = dict(title="mr", given_name="GILDONG", family_name="HONG", gender="m",
            born_on="1985-03-02", email="hong@example.com", phone_number="+821012345678")


def make(**over):
    return Traveler(**{**BASE, **over})


def test_names_normalized_to_passport_upper():
    t = make(given_name="  gil   dong ", family_name="hong")
    assert t.given_name == "GIL DONG" and t.passport_name == "HONG GIL DONG"


@pytest.mark.parametrize("name", ["홍길동", "HONG1", "O'NEIL", "-HONG", ""])
def test_rejects_non_passport_names(name):
    with pytest.raises(ValidationError):
        make(family_name=name)


def test_accepts_hyphenated_name():
    assert make(family_name="NAM-GUNG").family_name == "NAM-GUNG"


@pytest.mark.parametrize("phone,expected", [("+82 10-1234-5678", "+821012345678"), ("+63 (917) 123 4567", "+639171234567")])
def test_phone_normalized(phone, expected):
    assert make(phone_number=phone).phone_number == expected


@pytest.mark.parametrize("phone", ["010-1234-5678", "+0101234567", "+82"])
def test_rejects_non_international_phone(phone):
    with pytest.raises(ValidationError, match="국제 형식"):
        make(phone_number=phone)


@pytest.mark.parametrize("field,value", [("email", "hong@"), ("gender", "x"), ("title", "sir")])
def test_rejects_bad_fields(field, value):
    with pytest.raises(ValidationError):
        make(**{field: value})


def test_rejects_future_birthdate():
    with pytest.raises(ValidationError, match="오늘 이후"):
        make(born_on=(date.today() + timedelta(days=1)).isoformat())


@pytest.mark.parametrize("born,on,age", [
    (date(2008, 12, 20), date(2026, 12, 20), 18),   # 생일 당일 만 18세
    (date(2008, 12, 21), date(2026, 12, 20), 17),   # 하루 전
    (date(2000, 2, 29), date(2026, 2, 28), 25),     # 윤일생, 평년
])
def test_age_on(born, on, age):
    assert age_on(born, on) == age


def test_seat_count_mismatch():
    with pytest.raises(TravelerFileError, match="2석.*1명"):
        check_travelers_for_flight([make()], seats=2, departure=date(2026, 12, 20))


def test_minor_rejected_on_departure_date():
    teen = make(given_name="JUNIOR", born_on="2008-12-21")
    with pytest.raises(TravelerFileError, match="미만.*HONG JUNIOR"):
        check_travelers_for_flight([teen], seats=1, departure=date(2026, 12, 20))


def test_turns_18_on_departure_ok():
    check_travelers_for_flight([make(born_on="2008-12-20")], seats=1, departure=date(2026, 12, 20))


def test_duplicate_names_rejected():
    with pytest.raises(TravelerFileError, match="중복"):
        check_travelers_for_flight([make(), make()], seats=2, departure=date(2026, 12, 20))


def test_load_example_file(tmp_path, travelers_raw):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(travelers_raw, ensure_ascii=False), encoding="utf-8")
    tf = load_travelers(p)
    assert len(tf.travelers) == 2 and tf.trip.cost_center == "영업1팀"


def test_load_reports_field_errors(tmp_path, travelers_raw):
    travelers_raw["travelers"][1]["family_name"] = "김"
    p = tmp_path / "t.json"
    p.write_text(json.dumps(travelers_raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(TravelerFileError, match=r"travelers\.1\.family_name"):
        load_travelers(p)


def test_load_missing_and_broken_json(tmp_path):
    with pytest.raises(TravelerFileError, match="없습니다"):
        load_travelers(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{ broken", encoding="utf-8")
    with pytest.raises(TravelerFileError, match="JSON 형식 오류"):
        load_travelers(bad)


def test_trip_is_optional(tmp_path, travelers_raw):
    del travelers_raw["trip"]
    p = tmp_path / "t.json"
    p.write_text(json.dumps(travelers_raw), encoding="utf-8")
    assert load_travelers(p).trip.purpose is None
