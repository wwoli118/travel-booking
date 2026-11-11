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
