import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from src import guest_email, hotel_summary as hs, ledger
from src.hotel_models import (
    CancellationStep, HotelOffer, HotelRate, HotelSearchQuery, Location, fmt_with_offset,
)
from src.hotels import save_hotels_csv
from src.travelers import TravelerFile

ROOT = Path(__file__).parent.parent
CIN = date.today() + timedelta(days=40)
COUT = CIN + timedelta(days=3)
NOW = datetime.now(timezone.utc)
PH = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def test_key(monkeypatch):
    monkeypatch.setenv("DUFFEL_API_KEY", "duffel_test_x")


def ts(d: date, h=23, m=59):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=PH).isoformat()


def rate(rid, total, payment="pay_now", board="breakfast", timeline=None, methods=("balance",), loyalty=False,
         expires="2099-01-01T00:00:00Z"):
    return {"id": rid, "total_amount": total, "total_currency": "PHP", "board_type": board, "payment_type": payment,
            "due_at_accommodation_amount": None, "due_at_accommodation_currency": "PHP",
            "cancellation_timeline": timeline or [], "conditions": [{"title": "Smoking", "description": "No smoking"}],
            "available_payment_methods": list(methods), "loyalty_programme_required": loyalty, "expires_at": expires}


ACC = {"id": "acc_makati", "name": "Makati Business Hotel", "rating": 4, "review_score": 8.6,
       "email": "reservations@makati-hotel.example", "phone_number": "+6321234567",
       "check_in_information": {"check_in_after_time": "14:00", "check_out_before_time": "12:00"},
       "location": {"address": {"line_one": "1 Ayala Ave", "city_name": "Makati", "country_code": "PH", "postal_code": "1226"}}}


def rates_response():
    free_until = ts(CIN - timedelta(days=2))
    return {"data": {"id": "srr_1", "accommodation": {**ACC, "rooms": [
        {"name": "Deluxe King", "rates": [
            rate("rat_free", "16500.00", timeline=[{"before": free_until, "refund_amount": "16500.00", "currency": "PHP"}]),
            rate("rat_nonref", "15000.00", board="room_only"),
        ]},
        {"name": "Superior Twin", "rates": [
            rate("rat_guarantee", "15000.00", payment="guarantee", methods=("card",), loyalty=True,
                 timeline=[{"before": free_until, "refund_amount": "15000.00", "currency": "PHP"}]),
            {"id": "rat_broken"},
        ]},
    ]}}}


def quote_response(rate_id="rat_free", total="16500.00", rooms=2, cin=CIN, cout=COUT, with_rate=True):
    rr = rates_response()["data"]["accommodation"]["rooms"]
    chosen = [r for room in rr for r in room["rates"] if r.get("id") == rate_id]
    rooms_payload = [{"name": "Deluxe King", "rates": chosen}] if with_rate else []
    return {"data": {"id": "quo_1", "check_in_date": cin.isoformat(), "check_out_date": cout.isoformat(),
                     "rooms": rooms, "guests": [{"type": "adult"}] * 2, "total_amount": total, "total_currency": "PHP",
                     "due_at_accommodation_amount": "300.00", "due_at_accommodation_currency": "PHP",
                     "deposit_amount": None, "deposit_currency": "PHP",
                     "accommodation": {**ACC, "rooms": rooms_payload}}}


class FakeDuffel:
    def __init__(self, quote=None, rates=None):
        self.quote = quote or quote_response()
        self.rates = rates or rates_response()
        self.calls = []

    def client(self):
        def handler(req):
            body = json.loads(req.content) if req.content else None
            self.calls.append((req.method, req.url.path, body))
            if req.url.path == "/stays/search_results/srr_1/actions/fetch_all_rates":
                return httpx.Response(200, json=self.rates)
            if req.url.path == "/stays/quotes":
                return httpx.Response(201, json=self.quote)
            return httpx.Response(404, json={"errors": [{"title": "Not found", "message": req.url.path}]})
        return httpx.Client(base_url="https://api.duffel.com", transport=httpx.MockTransport(handler))


@pytest.fixture
def search_csv(tmp_path):
    q = HotelSearchQuery(location=Location(key="manila-makati", label="Makati", latitude=14.55, longitude=121.02),
                         check_in=CIN, check_out=COUT, rooms=2, adults=2)
    offer = HotelOffer(provider="duffel", search_result_id="srr_1", accommodation_id="acc_makati",
                       name="Makati Business Hotel", nights=3, total_amount="15000.00", total_currency="PHP")
    return save_hotels_csv(q, [offer], datetime(2026, 11, 11, tzinfo=timezone.utc), tmp_path)


@pytest.fixture
def travelers_file(tmp_path):
    p = tmp_path / "travelers.json"
    p.write_text((ROOT / "travelers.example.json").read_text(encoding="utf-8"), encoding="utf-8")
    return p


def run(tmp_path, capsys, fake, answers, *extra, csv_path=None, travelers=None):
    it = iter(answers)
    argv = ["--csv", str(csv_path), "--travelers", str(travelers), *extra]
    code = hs.main(argv, input_fn=lambda _: next(it), client=fake.client(),
                   data_dir=tmp_path / "data", drafts_dir=tmp_path / "drafts", now=NOW)
    return code, capsys.readouterr()


# ---- 취소 규정

def r(total="100.00", steps=()):
    return HotelRate(rate_id="r", room_name="Room", total_amount=total, total_currency="PHP",
                     cancellation=[CancellationStep(before=b, refund_amount=a, currency="PHP") for b, a in steps])


def test_cancellation_free_then_partial():
    t1 = datetime(2099, 1, 1, 23, 59, tzinfo=PH)
    t2 = datetime(2099, 1, 3, 23, 59, tzinfo=PH)
    rt = r(steps=[(t2, "50.00"), (t1, "100.00")])
    lines = rt.cancellation_lines(NOW)
    assert lines[0] == "2099-01-01 23:59 (UTC+08:00) 전까지 취소 시 전액 환불 (무료 취소)"
    assert lines[1] == "2099-01-03 23:59 (UTC+08:00) 전까지 취소 시 50.00 PHP 환불"
    assert lines[2] == "2099-01-03 23:59 (UTC+08:00) 이후 취소 시 환불 없음"
    assert rt.free_cancellation_until(NOW) == t1


def test_cancellation_empty_or_past_is_nonrefundable():
    assert r().cancellation_lines(NOW) == ["환불 불가 (지금 취소하면 환불 없음)"]
    past = r(steps=[(datetime(2020, 1, 1, tzinfo=PH), "100.00")])
    assert past.cancellation_lines(NOW)[0].startswith("환불 불가") and past.free_cancellation_until(NOW) is None


def test_zero_refund_step():
    rt = r(steps=[(datetime(2099, 1, 1, tzinfo=PH), "0")])
    assert "환불 없음" in rt.cancellation_lines(NOW)[0]


def test_fmt_with_offset_naive():
    assert fmt_with_offset(datetime(2099, 1, 1, 9, 5)) == "2099-01-01 09:05"


# ---- 객실 배정

@pytest.fixture
def tf():
    return TravelerFile.model_validate(json.loads((ROOT / "travelers.example.json").read_text(encoding="utf-8")))


def test_assign_one_per_room(tf):
    a = hs.assign_rooms(tf.travelers, 2, CIN)
    assert [(room, t.passport_name) for room, t in a] == [(1, "HONG GILDONG"), (2, "KIM YOUNGHEE")]


def test_assign_shared_room(tf):
    a = hs.assign_rooms(tf.travelers, 1, CIN, [1, 1])
    assert [room for room, _ in a] == [1, 1]


@pytest.mark.parametrize("rooms,room_of,msg", [
    (1, None, "--room-of"),
    (2, [1], "개수"),
    (2, [1, 3], "없는 객실"),
    (2, [1, 1], "투숙객이 없는 객실이 있습니다: 2번"),
])
def test_assign_errors(tf, rooms, room_of, msg):
    with pytest.raises(hs.HotelSummaryError, match=msg):
        hs.assign_rooms(tf.travelers, rooms, CIN, room_of)


def test_assign_minor_rejected(tf):
    young = tf.travelers[0].model_copy(update={"born_on": CIN - timedelta(days=365 * 17)})
    with pytest.raises(hs.HotelSummaryError, match="미만"):
        hs.assign_rooms([young], 1, CIN)


def test_parse_room_of():
    assert hs.parse_room_of("1, 1,2") == [1, 1, 2] and hs.parse_room_of(None) is None
    with pytest.raises(hs.HotelSummaryError):
        hs.parse_room_of("1,a")


# ---- 요금 목록

def test_rates_sorted_free_cancel_first_on_tie(capsys):
    from src.hotel_providers.duffel_stays import rates_from_accommodation
    rates = hs.sort_rates(rates_from_accommodation(rates_response()["data"]["accommodation"]), NOW)
    assert "rat_broken" in capsys.readouterr().err
    assert [x.rate_id for x in rates] == ["rat_guarantee", "rat_nonref", "rat_free"]  # 15000 동률 → 무료 취소 먼저
    table = hs.format_rates(rates, NOW)
    assert "무료 취소" in table and "환불 불가" in table and "예약 제약" in table


def test_rate_warnings():
    from src.hotel_providers.duffel_stays import parse_rate
    w = hs.rate_warnings(parse_rate(rate("x", "1", methods=("card",), loyalty=True), "R"))
    assert any("잔액" in m for m in w) and any("회원 번호" in m for m in w)


@pytest.mark.parametrize("answers,expected", [(["2"], "b"), (["9", "x", "1"], "a"), ([""], None), (["0", "0", "0"], None)])
def test_choose_rate(answers, expected):
    rs = [r().model_copy(update={"rate_id": "a"}), r().model_copy(update={"rate_id": "b"})]
    it = iter(answers)
    got = hs.choose_rate(rs, lambda _: next(it), None)
    assert (got.rate_id if got else None) == expected


def test_choose_rate_preset_out_of_range():
    with pytest.raises(hs.HotelSummaryError, match="1~1"):
        hs.choose_rate([r()], lambda _: "", 5)


# ---- 전체 흐름

def test_full_flow_yes(tmp_path, capsys, search_csv, travelers_file):
    fake = FakeDuffel()
    code, out = run(tmp_path, capsys, fake, ["3", "yes"], csv_path=search_csv, travelers=travelers_file)
    assert code == 0, out.err
    assert [(m, p) for m, p, _ in fake.calls] == [
        ("POST", "/stays/search_results/srr_1/actions/fetch_all_rates"), ("POST", "/stays/quotes")]
    assert fake.calls[1][2] == {"data": {"rate_id": "rat_free"}}
    assert not any("bookings" in p for _, p, _ in fake.calls)  # 예약 API는 호출하지 않음
    for item in ["Makati Business Hotel (★4)", "3박", "2실 / Deluxe King, 조식 포함", "1번 객실: HONG GILDONG",
                 "2번 객실: KIM YOUNGHEE", "16,500.00 PHP (1박 평균 5,500.00 PHP)", "변동 없음",
                 "현장 추가 300.00 PHP", "선결제", "전액 환불 (무료 취소)", "체크인 14:00 이후",
                 "영업1팀", "예약·결제를 하지 않습니다"]:
        assert item in out.out, item

    draft = json.loads(next((tmp_path / "drafts").glob("hotel_draft_acc_makati_*.json")).read_text())
    assert draft["quote_id"] == "quo_1" and draft["rate_id"] == "rat_free" and draft["kind"] == "hotel"
    assert draft["price"] == {"total_amount": "16500.00", "total_currency": "PHP"}

    booking_path = next((tmp_path / "data").glob("hotel_booking_acc_makati_*.json"))
    booking = json.loads(booking_path.read_text())
    assert booking["booking"]["reference"] == "" and booking["payment"]["prepaid"] is True
    assert booking["guests"] == [{"room": 1, "given_name": "GILDONG", "family_name": "HONG"},
                                 {"room": 2, "given_name": "YOUNGHEE", "family_name": "KIM"}]
    assert booking["hotel"]["email"] == "reservations@makati-hotel.example"

    # 예약 전 초안은 이메일·회계 도구가 빈 항목을 알려주며 거부
    with pytest.raises(guest_email.GuestEmailError, match="booking.reference.*booker.name.*card_holder"):
        guest_email.load_booking(booking_path)

    # 사람이 예약 후 채우면 이메일·회계 내역으로 이어짐
    booking["booking"]["reference"] = "DFL123"
    booking["booker"]["name"] = "GILDONG HONG"
    booking["payment"]["card_holder"] = "CHULSOO LEE"
    booking_path.write_text(json.dumps(booking, ensure_ascii=False), encoding="utf-8")
    b = guest_email.load_booking(booking_path)
    _, body = guest_email.render_en(b)
    assert "DFL123" in body and "Room 2: KIM YOUNGHEE" in body
    row = ledger.hotel_row(b)
    assert (row["주문ID"], row["금액"], row["통화"], row["상태"]) == ("HOTEL-DUFFEL-DFL123", "16500.00", "PHP", "결제완료")


def test_declined_saves_nothing(tmp_path, capsys, search_csv, travelers_file):
    code, out = run(tmp_path, capsys, FakeDuffel(), ["3", "no"], csv_path=search_csv, travelers=travelers_file)
    assert code == 0 and not (tmp_path / "drafts").exists() and not (tmp_path / "data").exists()


def test_no_rate_chosen_skips_quote(tmp_path, capsys, search_csv, travelers_file):
    fake = FakeDuffel()
    code, out = run(tmp_path, capsys, fake, [""], csv_path=search_csv, travelers=travelers_file)
    assert code == 0 and [p for _, p, _ in fake.calls] == ["/stays/search_results/srr_1/actions/fetch_all_rates"]


def test_price_increase_warned(tmp_path, capsys, search_csv, travelers_file):
    fake = FakeDuffel(quote=quote_response(total="17000.00"))
    code, out = run(tmp_path, capsys, fake, ["no"], "--rate", "3", csv_path=search_csv, travelers=travelers_file)
    assert "500.00 PHP 인상" in out.out and "!!! 주의" in out.out


def test_restricted_rate_warned_in_summary(tmp_path, capsys, search_csv, travelers_file):
    fake = FakeDuffel(quote=quote_response(rate_id="rat_guarantee", total="15000.00"))
    code, out = run(tmp_path, capsys, fake, ["no"], "--rate", "1", csv_path=search_csv, travelers=travelers_file)
    assert "[주의] Duffel 잔액으로 결제할 수 없는" in out.out and "회원 번호" in out.out
    assert "숙소에서 결제 (카드 보증)" in out.out


def test_quote_without_rate_uses_selected_policy(tmp_path, capsys, search_csv, travelers_file):
    fake = FakeDuffel(quote=quote_response(with_rate=False))
    code, out = run(tmp_path, capsys, fake, ["no"], "--rate", "3", csv_path=search_csv, travelers=travelers_file)
    assert code == 0 and "무료 취소" in out.out


def test_quote_mismatch_stops(tmp_path, capsys, search_csv, travelers_file):
    fake = FakeDuffel(quote=quote_response(rooms=1))
    code, out = run(tmp_path, capsys, fake, [], "--rate", "3", csv_path=search_csv, travelers=travelers_file)
    assert code == 1 and "검색 조건과 다릅니다" in out.err


def test_expired_rate_stops_before_quote(tmp_path, capsys, search_csv, travelers_file):
    rates = rates_response()
    rates["data"]["accommodation"]["rooms"][0]["rates"][0]["expires_at"] = "2020-01-01T00:00:00Z"
    fake = FakeDuffel(rates=rates)
    code, out = run(tmp_path, capsys, fake, [], "--rate", "3", csv_path=search_csv, travelers=travelers_file)
    assert code == 1 and "유효 시간이 지났습니다" in out.err and len(fake.calls) == 1


def test_quote_failure_reported(tmp_path, capsys, search_csv, travelers_file):
    fake = FakeDuffel()
    fake.quote = None
    def bad_client():
        def handler(req):
            if req.url.path == "/stays/quotes":
                return httpx.Response(422, json={"errors": [{"title": "Rate unavailable", "message": "sold out", "code": "rate_unavailable"}]})
            return httpx.Response(200, json=rates_response())
        return httpx.Client(base_url="https://api.duffel.com", transport=httpx.MockTransport(handler))
    fake.client = bad_client
    code, out = run(tmp_path, capsys, fake, [], "--rate", "3", csv_path=search_csv, travelers=travelers_file)
    assert code == 1 and "sold out" in out.err and "마감" in out.err


def test_traveler_count_must_match_search(tmp_path, capsys, search_csv, travelers_file):
    data = json.loads(travelers_file.read_text())
    data["travelers"].pop()
    travelers_file.write_text(json.dumps(data), encoding="utf-8")
    code, out = run(tmp_path, capsys, FakeDuffel(), [], "--room-of", "1", csv_path=search_csv, travelers=travelers_file)
    assert code == 2


def test_bad_csv_rank(tmp_path, capsys, search_csv, travelers_file):
    code, out = run(tmp_path, capsys, FakeDuffel(), [], "--rank", "5", csv_path=search_csv, travelers=travelers_file)
    assert code == 2 and "순위 5번이 없습니다" in out.err
