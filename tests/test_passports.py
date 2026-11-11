from datetime import date

import pytest
from pydantic import ValidationError

from src.travelers import Passport, Traveler, TravelerFileError, check_passports

DEP = date(2026, 12, 20)
BASE = dict(title="mr", given_name="GILDONG", family_name="HONG", gender="m",
            born_on="1985-03-02", email="hong@example.com", phone_number="+821012345678")


def t(passport=None, name="GILDONG"):
    return Traveler(**{**BASE, "given_name": name, "passport": passport})


def pp(expires="2031-05-01", number="M12345678", country="KR"):
    return {"number": number, "expires_on": expires, "issuing_country": country}


def test_passport_normalized_and_masked():
    p = Passport(**pp(number="m 1234 5678", country="kr"))
    assert p.number == "M12345678" and p.issuing_country == "KR"
    assert p.masked_number == "M12******"


@pytest.mark.parametrize("bad", [pp(number="M-123"), pp(number="M12"), pp(country="KOR"), pp(country="1A")])
def test_bad_passport_rejected(bad):
    with pytest.raises(ValidationError):
        t(bad)


def test_missing_passport_ok_when_not_required():
    assert check_passports([t()], DEP, required=False) == []


def test_missing_passport_error_when_required():
    with pytest.raises(TravelerFileError, match="HONG GILDONG.*여권 정보가 필요"):
        check_passports([t()], DEP, required=True)


@pytest.mark.parametrize("expires", ["2026-12-20", "2026-12-19"])
def test_passport_expiring_by_departure_blocks(expires):
    with pytest.raises(TravelerFileError, match="만료"):
        check_passports([t(pp(expires))], DEP, required=False)


def test_passport_under_six_months_warns():
    w = check_passports([t(pp("2027-05-01"))], DEP, required=True)
    assert len(w) == 1 and "6개월 미만" in w[0]


def test_passport_six_months_plus_no_warning():
    assert check_passports([t(pp("2027-06-21"))], DEP, required=True) == []


def test_all_errors_reported_together():
    with pytest.raises(TravelerFileError) as e:
        check_passports([t(name="A"), t(pp("2026-01-01"), name="B")], DEP, required=True)
    assert "HONG A" in str(e.value) and "HONG B" in str(e.value)
