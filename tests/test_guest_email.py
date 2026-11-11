import copy
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src import guest_email as ge

EXAMPLE = json.loads((Path(__file__).parent.parent / "hotel_booking.example.json").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 11)


@pytest.fixture
def raw():
    return copy.deepcopy(EXAMPLE)


def write(tmp_path, data):
    p = tmp_path / "booking.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def load(tmp_path, data, today=TODAY):
    return ge.load_booking(write(tmp_path, data), today=today)


def test_example_loads(tmp_path, raw):
    b = load(tmp_path, raw)
    assert b.nights == 3 and b.payment.card_holder == "GILDONG HONG"
    assert b.booker.phone == "+82212345678"


@pytest.mark.parametrize("cin,cout,nights", [
    ("2026-12-31", "2027-01-02", 2),   # 해 넘김
    ("2027-02-28", "2027-03-01", 1),   # 평년 2월 말
    ("2028-02-28", "2028-03-01", 2),   # 윤년
])
def test_nights_across_boundaries(tmp_path, raw, cin, cout, nights):
    raw["booking"].update(check_in=cin, check_out=cout)
    assert load(tmp_path, raw).nights == nights


@pytest.mark.parametrize("cin,cout", [("2026-12-20", "2026-12-20"), ("2026-12-21", "2026-12-20")])
def test_checkout_must_be_after_checkin(tmp_path, raw, cin, cout):
    raw["booking"].update(check_in=cin, check_out=cout)
    with pytest.raises(ge.GuestEmailError, match="체크아웃"):
        load(tmp_path, raw)


def test_past_booking_rejected_but_ongoing_stay_allowed(tmp_path, raw):
    raw["booking"].update(check_in="2026-11-09", check_out="2026-11-11")
    with pytest.raises(ge.GuestEmailError, match="지난 예약"):
        load(tmp_path, raw)
    raw["booking"].update(check_out="2026-11-12")
    assert load(tmp_path, raw).nights == 3


def test_room_without_guest(tmp_path, raw):
    raw["guests"] = raw["guests"][:1]
    with pytest.raises(ge.GuestEmailError, match="투숙객이 없는 객실.*2번"):
        load(tmp_path, raw)


def test_guest_in_nonexistent_room(tmp_path, raw):
    raw["guests"][1]["room"] = 3
    with pytest.raises(ge.GuestEmailError, match="2개인데 3번 객실"):
        load(tmp_path, raw)


def test_two_guests_same_room(tmp_path, raw):
    raw["booking"]["rooms"] = 1
    raw["guests"][1]["room"] = 1
    b = load(tmp_path, raw)
    assert [g.passport_name for g in b.guests_by_room()[1]] == ["KIM YOUNGHEE", "PARK MINSU"]


def test_guest_names_must_be_passport_style(tmp_path, raw):
    raw["guests"][0]["family_name"] = "김"
    with pytest.raises(ge.GuestEmailError, match="guests.0.family_name"):
        load(tmp_path, raw)


@pytest.mark.parametrize("key", ["card_number", "cvc", "expiry"])
def test_card_details_refused(tmp_path, raw, key):
    raw["payment"][key] = "4111111111111111"
    with pytest.raises(ge.GuestEmailError, match="카드 정보는 넣지 마세요"):
        load(tmp_path, raw)


@pytest.mark.parametrize("last4", ["4111111111111111", "123", "12a4"])
def test_card_last4_only_four_digits(tmp_path, raw, last4):
    raw["payment"]["card_last4"] = last4
    with pytest.raises(ge.GuestEmailError, match="마지막 4자리"):
        load(tmp_path, raw)


def test_unknown_field_typo_rejected(tmp_path, raw):
    raw["booking"]["chek_in"] = "2026-12-20"
    with pytest.raises(ge.GuestEmailError, match="chek_in"):
        load(tmp_path, raw)


@pytest.mark.parametrize("holder", ["KIM YOUNGHEE", "YOUNGHEE KIM"])
def test_holder_listed_as_guest_but_marked_not_staying(tmp_path, raw, holder):
    raw["payment"]["card_holder"] = holder
    with pytest.raises(ge.GuestEmailError, match="card_holder_stays"):
        load(tmp_path, raw)


def test_render_en_contains_required_items(tmp_path, raw):
    subject, body = ge.render_en(load(tmp_path, raw))
    assert "1234567890" in subject and "20 Dec 2026" in subject
    for item in ["Booking number : 1234567890", "Sun, 20 Dec 2026", "Wed, 23 Dec 2026 (3 nights)",
                 "Room 1: KIM YOUNGHEE", "Room 2: PARK MINSU", "GILDONG HONG (card ending in 1234), will NOT be staying",
                 "Example Co., Ltd.", "already been prepaid", "authorization form", "made via Booking.com"]:
        assert item in body, item


def test_render_ko_contains_required_items(tmp_path, raw):
    subject, body = ge.render_ko(load(tmp_path, raw))
    assert "예약번호 1234567890" in subject
    for item in ["2026년 12월 20일(일)", "2026년 12월 23일(수) (3박)", "1번 객실: KIM YOUNGHEE",
                 "GILDONG HONG(카드 끝자리 1234)님은 투숙하지 않습니다", "선결제", "아래 예약(Booking.com)의 투숙객 안내"]:
        assert item in body, item


def test_one_night_singular_and_no_optional_bits(tmp_path, raw):
    raw["booking"].update(check_out="2026-12-21", platform=None)
    raw["payment"].update(card_last4=None, prepaid=None)
    raw["booker"] = {"name": "GILDONG HONG"}
    subject, body = ge.render_en(load(tmp_path, raw))
    assert "(1 night)" in body and "card ending" not in body and "prepaid" not in body and "made via" not in body
    assert "made by GILDONG HONG" in body


def test_pay_at_hotel_asks_settlement(tmp_path, raw):
    raw["payment"]["prepaid"] = False
    _, body = ge.render_en(load(tmp_path, raw))
    assert "how you would like" in body


def test_holder_stays_skips_non_stay_notice(tmp_path, raw):
    raw["payment"].update(card_holder="KIM YOUNGHEE", card_holder_stays=True)
    _, body = ge.render_en(load(tmp_path, raw))
    assert "will NOT be staying" not in body and "register the guest name(s)" in body


def test_save_and_main(tmp_path, raw, capsys):
    b = load(tmp_path, raw)
    path, text = ge.save_draft(b, "en", tmp_path / "emails", datetime(2026, 11, 11, 15, tzinfo=timezone.utc))
    assert path.name == "guest_notice_1234567890_en_20261111-150000.txt"
    assert text.startswith("To: reservations@example-hotel.com\nSubject: Guest name notice")

    raw["hotel"]["email"] = None
    code = ge.main(["--booking", str(write(tmp_path, raw)), "--lang", "ko"], out_dir=tmp_path / "emails")
    out = capsys.readouterr().out
    assert code == 0 and "To: (호텔 이메일 주소를 확인해 입력하세요)" in out
    assert "보내지 않았습니다" in out and "호텔 이메일 주소가 없습니다" in out


def test_main_reports_errors(tmp_path, capsys):
    assert ge.main(["--booking", str(tmp_path / "none.json")], out_dir=tmp_path) == 2
    assert "hotel_booking.example.json" in capsys.readouterr().err
