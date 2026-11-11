"""회계용 예약 내역 (CLAUDE.md: 날짜, 출장자, 금액, 통화, 영수증 여부를 CSV로 남긴다).

data/ledger.csv 에 주문 1건당 1행. 상태가 바뀌면 같은 행을 갱신한다.
- 보류 주문 생성 시 자동 추가 (상태: 결제대기)
- 보류 주문 취소 시 자동 갱신 (상태: 취소)
- 사람이 Duffel 대시보드에서 결제한 뒤 mark-paid 로 직접 표시 (상태: 결제완료)

실행:
    python -m src.ledger list
    python -m src.ledger mark-paid --order ord_0000... --receipt
    python -m src.ledger receipt --order ord_0000... --yes
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import DATA_DIR

LEDGER_PATH = DATA_DIR / "ledger.csv"

COLUMNS = [
    "기록일시",
    "출발일",
    "구분",
    "출장자",
    "인원",
    "출장목적",
    "비용부서",
    "구간",
    "항공사",
    "편명",
    "예약번호",
    "주문ID",
    "금액",
    "통화",
    "상태",
    "결제마감",
    "결제확인일시",
    "영수증",
    "환불액",
    "환불통화",
    "비고",
    "테스트",
]

STATUS_HELD = "결제대기"
STATUS_PAID = "결제완료"
STATUS_CANCELLED = "취소"


class LedgerError(Exception):
    """내역 파일을 읽거나 갱신하지 못했을 때. 원인을 담는다."""


def now_local() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_rows(path: Path = LEDGER_PATH) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise LedgerError(f"{path} 열 구성이 다릅니다. 없는 열: {', '.join(missing)}")
        return list(reader)


def write_rows(rows: list[dict], path: Path = LEDGER_PATH) -> None:
    """임시 파일에 쓴 뒤 교체한다 (쓰다가 실패해도 기존 내역이 깨지지 않게)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ledger_", suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            for r in rows:
                writer.writerow({c: r.get(c, "") for c in COLUMNS})
        os.replace(tmp, path)
    except PermissionError as e:
        os.unlink(tmp)
        raise LedgerError(f"{path}에 쓸 수 없습니다. 엑셀에서 열려 있으면 닫고 다시 시도하세요. ({e})") from e
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def add_flight_order(
    *,
    order_id: str,
    booking_reference: str | None,
    departure_date: str,
    travelers: list[str],
    purpose: str | None,
    cost_center: str | None,
    route: str,
    airline: str,
    flight_numbers: str,
    total_amount: str,
    total_currency: str,
    payment_required_by: str | None,
    live_mode: bool,
    path: Path = LEDGER_PATH,
) -> dict:
    rows = read_rows(path)
    if any(r["주문ID"] == order_id for r in rows):
        raise LedgerError(f"이미 기록된 주문입니다: {order_id}")
    try:
        amount = Decimal(total_amount)
    except (InvalidOperation, TypeError) as e:
        raise LedgerError(f"금액을 해석할 수 없습니다: {total_amount!r}") from e
    if not total_currency:
        raise LedgerError("통화 정보가 없어 기록할 수 없습니다 (금액은 항상 통화와 함께 기록).")
    row = {
        "기록일시": now_local(),
        "출발일": departure_date,
        "구분": "항공",
        "출장자": "; ".join(travelers),
        "인원": str(len(travelers)),
        "출장목적": purpose or "",
        "비용부서": cost_center or "",
        "구간": route,
        "항공사": airline,
        "편명": flight_numbers,
        "예약번호": booking_reference or "",
        "주문ID": order_id,
        "금액": f"{amount:.2f}",
        "통화": total_currency,
        "상태": STATUS_HELD,
        "결제마감": payment_required_by or "",
        "결제확인일시": "",
        "영수증": "N",
        "환불액": "",
        "환불통화": "",
        "비고": "보류 주문 (결제 전)",
        "테스트": "N" if live_mode else "Y",
    }
    rows.append(row)
    write_rows(rows, path)
    return row


def _find(rows: list[dict], order_id: str) -> dict:
    for r in rows:
        if r["주문ID"] == order_id:
            return r
    raise LedgerError(f"내역에 없는 주문입니다: {order_id}")


def mark_cancelled(
    order_id: str, refund_amount: str | None, refund_currency: str | None, path: Path = LEDGER_PATH
) -> dict:
    rows = read_rows(path)
    r = _find(rows, order_id)
    r.update(
        {
            "상태": STATUS_CANCELLED,
            "환불액": refund_amount or "0.00",
            "환불통화": refund_currency or r["통화"],
            "비고": f"취소 {now_local()}",
        }
    )
    write_rows(rows, path)
    return r


def mark_paid(order_id: str, receipt: bool, path: Path = LEDGER_PATH) -> dict:
    """사람이 직접 결제한 뒤 표시한다. 이 코드는 결제를 하지 않는다."""
    rows = read_rows(path)
    r = _find(rows, order_id)
    if r["상태"] == STATUS_CANCELLED:
        raise LedgerError(f"취소된 주문은 결제완료로 바꿀 수 없습니다: {order_id}")
    r.update(
        {
            "상태": STATUS_PAID,
            "결제확인일시": now_local(),
            "영수증": "Y" if receipt else "N",
            "비고": "사람이 결제 후 표시",
        }
    )
    write_rows(rows, path)
    return r


def set_receipt(order_id: str, receipt: bool, path: Path = LEDGER_PATH) -> dict:
    rows = read_rows(path)
    r = _find(rows, order_id)
    r["영수증"] = "Y" if receipt else "N"
    write_rows(rows, path)
    return r


def totals(rows: list[dict]) -> dict[tuple[str, str], Decimal]:
    """(상태, 통화)별 합계. 통화가 다르면 합치지 않는다."""
    out: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for r in rows:
        out[(r["상태"], r["통화"])] += Decimal(r["금액"])
    return dict(out)


def format_list(rows: list[dict]) -> str:
    if not rows:
        return "기록된 예약이 없습니다."
    lines = [f"{'출발일':<11}{'상태':<7}{'예약번호':<9}{'출장자':<28}{'금액':>16}  영수증  주문ID"]
    for r in rows:
        lines.append(
            f"{r['출발일']:<11}{r['상태']:<7}{r['예약번호']:<9}{r['출장자'][:28]:<28}"
            f"{r['금액'] + ' ' + r['통화']:>16}  {r['영수증']:^6}  {r['주문ID']}"
        )
    lines.append("")
    lines.append("합계 (통화별, 환율 환산 없음):")
    for (status, cur), amount in sorted(totals(rows).items()):
        lines.append(f"  {status:<6} {amount:>14,.2f} {cur}")
    unreceipted = [r for r in rows if r["상태"] == STATUS_PAID and r["영수증"] != "Y"]
    if unreceipted:
        lines.append(f"\n[주의] 결제완료인데 영수증이 없는 건: {len(unreceipted)}건")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="회계용 예약 내역")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="내역과 통화별 합계 보기")
    mp = sub.add_parser("mark-paid", help="사람이 결제한 주문을 결제완료로 표시")
    mp.add_argument("--order", required=True)
    mp.add_argument("--receipt", action="store_true", help="영수증을 받았으면 표시")
    rc = sub.add_parser("receipt", help="영수증 여부 변경")
    rc.add_argument("--order", required=True)
    g = rc.add_mutually_exclusive_group(required=True)
    g.add_argument("--yes", dest="receipt", action="store_true")
    g.add_argument("--no", dest="receipt", action="store_false")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, path: Path = LEDGER_PATH) -> int:
    args = parse_args(argv)
    try:
        if args.command == "list":
            print(format_list(read_rows(path)))
        elif args.command == "mark-paid":
            r = mark_paid(args.order, args.receipt, path)
            print(f"결제완료로 표시했습니다: {r['예약번호']} {r['금액']} {r['통화']} (영수증 {r['영수증']})")
        else:
            r = set_receipt(args.order, args.receipt, path)
            print(f"영수증 여부를 {r['영수증']}(으)로 바꿨습니다: {r['예약번호']}")
    except LedgerError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
