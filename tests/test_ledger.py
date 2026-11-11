import csv
from decimal import Decimal

import pytest

from src import ledger

BASE = dict(booking_reference="RZPVXK", departure_date="2026-12-20", travelers=["HONG GILDONG", "KIM YOUNGHEE"],
            purpose="미팅", cost_center="영업1팀", route="ICN-MNL", airline="Duffel Airways",
            flight_numbers="ZZ2002 / ZZ2003", total_amount="289.00", total_currency="USD",
            payment_required_by="2026-11-12T09:00:00Z", live_mode=False)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "ledger.csv"


def add(path, order_id="ord_1", **over):
    return ledger.add_flight_order(order_id=order_id, path=path, **{**BASE, **over})


def test_add_creates_excel_friendly_csv(path):
    add(path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM: 엑셀 한글 깨짐 방지
    with path.open(encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f))
    assert header == ledger.COLUMNS


def test_amount_normalized_with_currency(path):
    r = add(path, total_amount="1500", total_currency="PHP")
    assert (r["금액"], r["통화"]) == ("1500.00", "PHP")


@pytest.mark.parametrize("amount,cur,msg", [("abc", "USD", "금액"), ("10.00", "", "통화")])
def test_rejects_bad_amount_or_currency(path, amount, cur, msg):
    with pytest.raises(ledger.LedgerError, match=msg):
        add(path, total_amount=amount, total_currency=cur)
    assert not path.exists()


def test_duplicate_order_rejected(path):
    add(path)
    with pytest.raises(ledger.LedgerError, match="이미 기록된"):
        add(path)


def test_status_flow_paid_and_receipt(path):
    add(path)
    r = ledger.mark_paid("ord_1", receipt=False, path=path)
    assert r["상태"] == "결제완료" and r["영수증"] == "N" and r["결제확인일시"]
    ledger.set_receipt("ord_1", True, path)
    assert ledger.read_rows(path)[0]["영수증"] == "Y"


def test_cancelled_cannot_be_paid(path):
    add(path)
    ledger.mark_cancelled("ord_1", "0.00", "USD", path)
    with pytest.raises(ledger.LedgerError, match="취소된 주문"):
        ledger.mark_paid("ord_1", True, path)


def test_unknown_order(path):
    add(path)
    with pytest.raises(ledger.LedgerError, match="없는 주문"):
        ledger.set_receipt("ord_x", True, path)


def test_update_keeps_other_rows(path):
    add(path, "ord_1")
    add(path, "ord_2", total_amount="100.00")
    ledger.mark_cancelled("ord_1", None, None, path)
    rows = ledger.read_rows(path)
    assert [r["상태"] for r in rows] == ["취소", "결제대기"]
    assert rows[0]["환불액"] == "0.00" and rows[0]["환불통화"] == "USD"


def test_totals_by_status_and_currency_no_mixing(path):
    add(path, "ord_1", total_amount="100.10")
    add(path, "ord_2", total_amount="200.20")
    add(path, "ord_3", total_amount="5000", total_currency="PHP")
    ledger.mark_paid("ord_2", True, path)
    t = ledger.totals(ledger.read_rows(path))
    assert t == {("결제대기", "USD"): Decimal("100.10"), ("결제완료", "USD"): Decimal("200.20"),
                 ("결제대기", "PHP"): Decimal("5000.00")}


def test_list_warns_missing_receipt(path, capsys):
    add(path)
    ledger.mark_paid("ord_1", receipt=False, path=path)
    assert ledger.main(["list"], path=path) == 0
    out = capsys.readouterr().out
    assert "영수증이 없는 건: 1건" in out and "289.00 USD" in out


def test_list_empty(path, capsys):
    ledger.main(["list"], path=path)
    assert "기록된 예약이 없습니다" in capsys.readouterr().out


def test_cli_mark_paid_and_receipt(path, capsys):
    add(path)
    assert ledger.main(["mark-paid", "--order", "ord_1", "--receipt"], path=path) == 0
    assert ledger.read_rows(path)[0]["영수증"] == "Y"
    assert ledger.main(["receipt", "--order", "ord_1", "--no"], path=path) == 0
    assert ledger.read_rows(path)[0]["영수증"] == "N"
    assert ledger.main(["mark-paid", "--order", "ord_x"], path=path) == 1


def test_wrong_columns_detected(path):
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ledger.LedgerError, match="열 구성"):
        ledger.read_rows(path)


def test_failed_write_keeps_old_file(path, monkeypatch):
    add(path)
    before = path.read_bytes()

    def boom(*a, **k):
        raise RuntimeError("disk error")
    monkeypatch.setattr(ledger.csv, "DictWriter", boom)
    with pytest.raises(RuntimeError):
        ledger.mark_paid("ord_1", True, path)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".ledger_*"))


# ---------------------------------------------------------------- 결제 여부 조회 (sync)

import httpx  # noqa: E402

from src.order_status import OrderStatus  # noqa: E402

NOW = __import__("datetime").datetime(2026, 11, 11, 12, 0, tzinfo=__import__("datetime").timezone.utc)


def duffel_order(order_id="ord_1", awaiting=True, paid_at=None, cancelled_at=None, amount="289.00",
                 currency="USD", tickets=(), deadline="2026-11-12T09:00:00Z"):
    return {"id": order_id, "booking_reference": "RZPVXK", "live_mode": False,
            "total_amount": amount, "total_currency": currency, "cancelled_at": cancelled_at,
            "payment_status": {"awaiting_payment": awaiting, "paid_at": paid_at, "payment_required_by": deadline},
            "documents": [{"type": "electronic_ticket", "unique_identifier": t} for t in tickets]}


def st(**kw):
    return OrderStatus.from_duffel(duffel_order(**kw))


def row(path, order_id="ord_1"):
    return next(r for r in ledger.read_rows(path) if r["주문ID"] == order_id)


def test_decide_paid_updates_held(path):
    add(path)
    ch, msgs = ledger.decide_update(row(path), st(awaiting=False, paid_at="2026-11-11T05:00:00Z", tickets=["125111"]), NOW)
    assert ch["상태"] == "결제완료" and ch["결제확인일시"].startswith("2026-11-11") and ch["항공권번호"] == "125111"
    assert any("receipt --yes" in m for m in msgs)


def test_decide_awaiting_keeps_and_shows_deadline(path):
    add(path)
    ch, msgs = ledger.decide_update(row(path), st(), NOW)
    assert "상태" not in ch and any("결제 마감" in m for m in msgs)


def test_decide_deadline_passed_warns(path):
    add(path)
    _, msgs = ledger.decide_update(row(path), st(deadline="2026-11-11T11:00:00Z"), NOW)
    assert any("지났습니다" in m for m in msgs)


def test_decide_cancelled_by_duffel(path):
    add(path)
    ch, msgs = ledger.decide_update(row(path), st(cancelled_at="2026-11-12T09:01:00Z"), NOW)
    assert ch["상태"] == "취소"


def test_decide_paid_then_cancelled_asks_refund_check(path):
    add(path)
    ledger.mark_paid("ord_1", True, path)
    ch, msgs = ledger.decide_update(row(path), st(cancelled_at="2026-11-12T09:01:00Z"), NOW)
    assert ch["상태"] == "취소" and any("환불" in m for m in msgs)


@pytest.mark.parametrize("ledger_state,duffel_kw", [
    ("결제완료", {}),                                   # 수동 표시했는데 Duffel은 미결제
    ("취소", {"awaiting": False, "paid_at": "2026-11-11T05:00:00Z"}),
    ("취소", {}),
])
def test_decide_mismatch_does_not_overwrite(path, ledger_state, duffel_kw):
    add(path)
    if ledger_state == "결제완료":
        ledger.mark_paid("ord_1", False, path)
    else:
        ledger.mark_cancelled("ord_1", None, None, path)
    ch, msgs = ledger.decide_update(row(path), st(**duffel_kw), NOW)
    assert "상태" not in ch and any("[주의]" in m and "바꾸지 않음" in m for m in msgs)


def test_decide_amount_mismatch_warns(path):
    add(path)
    _, msgs = ledger.decide_update(row(path), st(amount="300.00"), NOW)
    assert any("금액이 다릅니다" in m for m in msgs)


class FakeDuffel:
    def __init__(self, orders):
        self.orders, self.calls = orders, []

    def client(self):
        def handler(req):
            self.calls.append((req.method, req.url.path))
            oid = req.url.path.rsplit("/", 1)[-1]
            if oid in self.orders:
                return httpx.Response(200, json={"data": self.orders[oid]})
            return httpx.Response(404, json={"errors": [{"title": "Not found", "message": f"{oid} 없음", "code": "not_found"}]})
        return httpx.Client(base_url="https://api.duffel.com", transport=httpx.MockTransport(handler))


def test_sync_updates_file_and_skips_cancelled(path):
    add(path, "ord_1")
    add(path, "ord_2")
    add(path, "ord_3")
    ledger.mark_cancelled("ord_3", None, None, path)
    fake = FakeDuffel({"ord_1": duffel_order("ord_1", awaiting=False, paid_at="2026-11-11T05:00:00Z"),
                       "ord_2": duffel_order("ord_2")})
    lines, failures = ledger.sync("duffel_test_x", path, client=fake.client(), now=NOW)
    assert failures == 0
    assert [c[1] for c in fake.calls] == ["/air/orders/ord_1", "/air/orders/ord_2"]  # 취소 건은 조회 안 함
    assert all(m == "GET" for m, _ in fake.calls)
    assert row(path, "ord_1")["상태"] == "결제완료" and row(path, "ord_2")["상태"] == "결제대기"
    assert row(path, "ord_1")["최종조회일시"] and row(path, "ord_3")["최종조회일시"] == ""
    assert any("결제대기 → 결제완료" in line for line in lines)


def test_sync_dry_run_leaves_file(path):
    add(path)
    before = path.read_bytes()
    fake = FakeDuffel({"ord_1": duffel_order(awaiting=False, paid_at="2026-11-11T05:00:00Z")})
    lines, _ = ledger.sync("duffel_test_x", path, dry_run=True, client=fake.client(), now=NOW)
    assert path.read_bytes() == before and any("dry-run" in line for line in lines)


def test_sync_reports_failures_and_continues(path):
    add(path, "ord_missing")
    add(path, "ord_2")
    fake = FakeDuffel({"ord_2": duffel_order("ord_2", awaiting=False)})
    lines, failures = ledger.sync("duffel_test_x", path, client=fake.client(), now=NOW)
    assert failures == 1 and any("조회 실패" in line and "ord_missing" in line for line in lines)
    assert row(path, "ord_2")["상태"] == "결제완료"


def test_sync_specific_cancelled_order(path):
    add(path)
    ledger.mark_cancelled("ord_1", None, None, path)
    fake = FakeDuffel({"ord_1": duffel_order(cancelled_at="2026-11-12T09:01:00Z")})
    lines, failures = ledger.sync("duffel_test_x", path, order_id="ord_1", client=fake.client(), now=NOW)
    assert failures == 0 and fake.calls == [("GET", "/air/orders/ord_1")]


def test_cli_sync_exit_codes(path, monkeypatch, capsys):
    add(path)
    monkeypatch.setenv("DUFFEL_API_KEY", "duffel_test_x")
    ok = FakeDuffel({"ord_1": duffel_order()})
    assert ledger.main(["sync"], path=path, client=ok.client()) == 0
    bad = FakeDuffel({})
    assert ledger.main(["sync"], path=path, client=bad.client()) == 1
    assert "1건 조회에 실패" in capsys.readouterr().err


def test_cli_sync_rejects_live_key(path, monkeypatch, capsys):
    add(path)
    monkeypatch.setenv("DUFFEL_API_KEY", "duffel_live_x")
    assert ledger.main(["sync"], path=path, client=FakeDuffel({}).client()) == 1
    assert "테스트 키가 아닙니다" in capsys.readouterr().err


def test_old_ledger_without_new_columns_still_works(path):
    add(path)
    import csv as _csv
    rows = ledger.read_rows(path)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=ledger.REQUIRED_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    assert ledger.read_rows(path)[0]["항공권번호"] == ""
    fake = FakeDuffel({"ord_1": duffel_order(awaiting=False, tickets=["125111"])})
    ledger.sync("duffel_test_x", path, client=fake.client(), now=NOW)
    assert ledger.read_rows(path)[0]["항공권번호"] == "125111"


# ---------------------------------------------------------------- 호텔 예약

import copy as _copy  # noqa: E402
import json as _json  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

HOTEL_EXAMPLE = _json.loads((_Path(__file__).parent.parent / "hotel_booking.example.json").read_text(encoding="utf-8"))


def hotel_file(tmp_path, **over):
    d = _copy.deepcopy(HOTEL_EXAMPLE)
    for k, v in over.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = {**d.get(k, {}), **v} if isinstance(v, dict) else v
    f = tmp_path / "hotel.json"
    f.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return f


def run_add_hotel(path, booking_file, answer="yes"):
    return ledger.main(["add-hotel", "--booking", str(booking_file)], path=path, input_fn=lambda _: answer)


def test_add_hotel_prepaid(tmp_path, path, capsys):
    assert run_add_hotel(path, hotel_file(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Example Hotel Manila" in out and "15800.00 PHP" in out
    r = ledger.read_rows(path)[0]
    assert (r["구분"], r["상태"], r["금액"], r["통화"]) == ("호텔", "결제완료", "15800.00", "PHP")
    assert r["주문ID"] == "HOTEL-BOOKINGCOM-1234567890" and r["예약번호"] == "1234567890"
    assert r["출장자"] == "KIM YOUNGHEE; PARK MINSU" and r["인원"] == "2"
    assert r["출발일"] == "2026-12-20" and r["구간"] == "Example Hotel Manila"
    assert "3박 2실" in r["비고"] and r["결제확인일시"] and r["영수증"] == "N" and r["테스트"] == "N"
    assert (r["출장목적"], r["비용부서"]) == ("마닐라 거래처 미팅", "영업1팀")


@pytest.mark.parametrize("prepaid,status,note", [(False, "결제대기", "현장 결제"), (None, "결제대기", "미기재")])
def test_add_hotel_not_prepaid(tmp_path, path, prepaid, status, note):
    f = hotel_file(tmp_path)
    d = _json.loads(f.read_text())
    if prepaid is None:
        d["payment"].pop("prepaid")
    else:
        d["payment"]["prepaid"] = prepaid
    f.write_text(_json.dumps(d), encoding="utf-8")
    run_add_hotel(path, f)
    r = ledger.read_rows(path)[0]
    assert r["상태"] == status and note in r["비고"] and r["결제확인일시"] == ""


def test_add_hotel_declined_writes_nothing(tmp_path, path, capsys):
    assert run_add_hotel(path, hotel_file(tmp_path), answer="no") == 0
    assert not path.exists() and "추가하지 않았습니다" in capsys.readouterr().out


def test_add_hotel_requires_price(tmp_path, path, capsys):
    assert run_add_hotel(path, hotel_file(tmp_path, price=None)) == 1
    assert "price" in capsys.readouterr().err and not path.exists()


@pytest.mark.parametrize("price,msg", [({"total_amount": "0", "currency": "PHP"}, "greater than 0"),
                                       ({"total_amount": "100", "currency": "PESO"}, "3글자")])
def test_add_hotel_bad_price(tmp_path, path, capsys, price, msg):
    assert run_add_hotel(path, hotel_file(tmp_path, price=price)) == 1
    assert msg in capsys.readouterr().err


def test_add_hotel_duplicate(tmp_path, path, capsys):
    f = hotel_file(tmp_path)
    run_add_hotel(path, f)
    assert run_add_hotel(path, f) == 1
    assert "이미 기록된 호텔 예약" in capsys.readouterr().err


def test_same_reference_different_platform_is_separate(tmp_path, path):
    run_add_hotel(path, hotel_file(tmp_path))
    run_add_hotel(path, hotel_file(tmp_path, booking={"platform": "Agoda"}))
    assert [r["주문ID"] for r in ledger.read_rows(path)] == ["HOTEL-BOOKINGCOM-1234567890", "HOTEL-AGODA-1234567890"]


def test_direct_booking_id(tmp_path, path):
    run_add_hotel(path, hotel_file(tmp_path, booking={"platform": None}))
    r = ledger.read_rows(path)[0]
    assert r["주문ID"] == "HOTEL-DIRECT-1234567890" and "직접 예약" in r["비고"]


def test_past_stay_can_still_be_recorded(tmp_path, path):
    f = hotel_file(tmp_path, booking={"check_in": "2026-09-18", "check_out": "2026-09-20"})
    assert run_add_hotel(path, f) == 0


def test_sync_skips_hotel_rows(tmp_path, path):
    add(path, "ord_1")
    run_add_hotel(path, hotel_file(tmp_path, payment={"prepaid": False}))
    fake = FakeDuffel({"ord_1": duffel_order()})
    lines, failures = ledger.sync("duffel_test_x", path, client=fake.client(), now=NOW)
    assert failures == 0 and fake.calls == [("GET", "/air/orders/ord_1")]


def test_sync_specific_hotel_refused(tmp_path, path):
    run_add_hotel(path, hotel_file(tmp_path))
    with pytest.raises(ledger.LedgerError, match="호텔 내역이라 Duffel에서 조회할 수 없습니다"):
        ledger.sync("duffel_test_x", path, order_id="HOTEL-BOOKINGCOM-1234567890", client=FakeDuffel({}).client())


def test_mark_cancelled_cli_for_hotel(tmp_path, path, capsys):
    run_add_hotel(path, hotel_file(tmp_path))
    assert ledger.main(["mark-cancelled", "--order", "HOTEL-BOOKINGCOM-1234567890", "--refund", "15800"], path=path) == 0
    r = ledger.read_rows(path)[0]
    assert (r["상태"], r["환불액"], r["환불통화"]) == ("취소", "15800.00", "PHP")
    assert "실제 예약 취소는 예약한 곳에서" in capsys.readouterr().out
    assert ledger.main(["mark-cancelled", "--order", "HOTEL-BOOKINGCOM-1234567890", "--refund", "abc"], path=path) == 1


def test_list_shows_kind_and_mixed_totals(tmp_path, path, capsys):
    add(path, "ord_1")
    run_add_hotel(path, hotel_file(tmp_path))
    ledger.main(["list"], path=path)
    out = capsys.readouterr().out
    assert "항공" in out and "호텔" in out and "15,800.00 PHP" in out and "289.00 USD" in out
