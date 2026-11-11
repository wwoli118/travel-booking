"""회계용 예약 내역 (CLAUDE.md: 날짜, 출장자, 금액, 통화, 영수증 여부를 CSV로 남긴다).

data/ledger.csv 에 주문 1건당 1행. 상태가 바뀌면 같은 행을 갱신한다.
- 항공: 보류 주문 생성 시 자동 추가 (상태: 결제대기)
- 호텔: add-hotel 로 예약 정보 파일(hotel_booking.json)을 확인(yes) 후 추가
        (선결제면 결제완료, 아니면 결제대기 / 구간 열에 숙소명)
- 보류 주문 취소 시 자동 갱신 (상태: 취소)
- 사람이 Duffel 대시보드에서 결제한 뒤 sync 로 Duffel 주문 상태를 조회해 반영 (조회 전용)
  또는 mark-paid 로 직접 표시 (상태: 결제완료)

실행:
    python -m src.ledger list
    python -m src.ledger sync                      # 취소 외 전체 주문의 결제 여부 조회·반영
    python -m src.ledger sync --order ord_... --dry-run
    python -m src.ledger add-hotel --booking data/hotel_booking.json
    python -m src.ledger mark-cancelled --order HOTEL-... --refund 0 --currency PHP
    python -m src.ledger mark-paid --order ord_0000... --receipt
    python -m src.ledger receipt --order ord_0000... --yes
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from .config import DATA_DIR, ConfigError, load_duffel_key
from .duffel import DuffelError
from .guest_email import GuestEmailError, HotelBooking, load_booking
from .order_status import (
    PAYMENT_AWAITING,
    PAYMENT_CANCELLED,
    PAYMENT_PAID,
    OrderStatus,
    fetch_order_status,
)

LEDGER_PATH = DATA_DIR / "ledger.csv"

REQUIRED_COLUMNS = [
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
# 나중에 추가된 열: 예전 파일에 없어도 빈 값으로 읽는다
OPTIONAL_COLUMNS = ["항공권번호", "최종조회일시"]
COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

STATUS_HELD = "결제대기"
STATUS_PAID = "결제완료"
STATUS_CANCELLED = "취소"
KIND_FLIGHT = "항공"
KIND_HOTEL = "호텔"


class LedgerError(Exception):
    """내역 파일을 읽거나 갱신하지 못했을 때. 원인을 담는다."""


def now_local() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_rows(path: Path = LEDGER_PATH) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise LedgerError(f"{path} 열 구성이 다릅니다. 없는 열: {', '.join(missing)}")
        return [{**{c: "" for c in OPTIONAL_COLUMNS}, **row} for row in reader]


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


def hotel_order_id(b: HotelBooking) -> str:
    """호텔 예약의 내역 키. 예약 사이트가 다르면 같은 번호도 다른 예약으로 본다."""
    platform = re.sub(r"[^A-Za-z0-9]", "", b.booking.platform or "DIRECT").upper()
    return f"HOTEL-{platform}-{b.booking.reference}"


def hotel_row(b: HotelBooking) -> dict:
    """호텔 예약을 내역 행으로 바꾼다 (파일에 쓰지 않음)."""
    if b.price is None:
        raise LedgerError(
            "예약 정보 파일에 price(total_amount, currency)가 없습니다. 회계 내역에는 금액과 통화가 필요합니다."
        )
    bk = b.booking
    paid = b.payment.prepaid is True
    detail = f"{bk.check_in.isoformat()}~{bk.check_out.isoformat()} {b.nights}박 {bk.rooms}실, {bk.platform or '직접 예약'}"
    if b.payment.prepaid is False:
        detail += ", 현장 결제"
    elif b.payment.prepaid is None:
        detail += ", 결제 시점 미기재"
    return {
        "기록일시": now_local(),
        "출발일": bk.check_in.isoformat(),
        "구분": KIND_HOTEL,
        "출장자": "; ".join(g.passport_name for g in b.guests),
        "인원": str(len(b.guests)),
        "출장목적": b.trip.purpose or "",
        "비용부서": b.trip.cost_center or "",
        "구간": b.hotel.name,
        "항공사": "",
        "편명": "",
        "예약번호": bk.reference,
        "주문ID": hotel_order_id(b),
        "금액": f"{b.price.total_amount:.2f}",
        "통화": b.price.currency,
        "상태": STATUS_PAID if paid else STATUS_HELD,
        "결제마감": "",
        "결제확인일시": now_local() if paid else "",
        "영수증": "N",
        "환불액": "",
        "환불통화": "",
        "비고": detail,
        "테스트": "N",
        "항공권번호": "",
        "최종조회일시": "",
    }


def add_hotel_booking(b: HotelBooking, path: Path = LEDGER_PATH) -> dict:
    rows = read_rows(path)
    row = hotel_row(b)
    if any(r["주문ID"] == row["주문ID"] for r in rows):
        raise LedgerError(f"이미 기록된 호텔 예약입니다: {row['주문ID']}")
    rows.append(row)
    write_rows(rows, path)
    return row


def format_hotel_preview(row: dict) -> str:
    return "\n".join(
        [
            "회계 내역에 추가할 호텔 예약:",
            f"  숙소     {row['구간']}",
            f"  일정     {row['비고']}",
            f"  투숙객   {row['출장자']} ({row['인원']}명)",
            f"  예약번호 {row['예약번호']}  (내역 키 {row['주문ID']})",
            f"  금액     {row['금액']} {row['통화']}",
            f"  상태     {row['상태']}  / 영수증 {row['영수증']}",
            f"  출장     목적: {row['출장목적'] or '-'} / 비용 부서: {row['비용부서'] or '-'}",
        ]
    )


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
    lines = [f"{'출발일':<11}{'구분':<4}{'상태':<7}{'예약번호':<11}{'출장자':<28}{'금액':>16}  영수증  주문ID"]
    for r in rows:
        lines.append(
            f"{r['출발일']:<11}{r['구분']:<4}{r['상태']:<7}{r['예약번호']:<11}{r['출장자'][:28]:<28}"
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


# ---------------------------------------------------------------- 결제 여부 조회

def _fmt(dt: datetime | None) -> str:
    return "정보 없음" if dt is None else dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def decide_update(row: dict, st: OrderStatus, now: datetime | None = None) -> tuple[dict, list[str]]:
    """Duffel 상태를 보고 내역에 반영할 변경과 알릴 문구를 정한다 (파일은 건드리지 않음).

    상태가 명확할 때만 바꾸고, 내역과 Duffel이 어긋나면 덮어쓰지 않고 경고만 한다.
    """
    now = now or datetime.now(timezone.utc)
    changes: dict = {}
    msgs: list[str] = []
    cur = row["상태"]

    if st.total_amount is not None and (
        f"{st.total_amount:.2f}" != row["금액"] or (st.total_currency or "") != row["통화"]
    ):
        msgs.append(
            f"[주의] 금액이 다릅니다: 내역 {row['금액']} {row['통화']} / Duffel {st.total_amount:.2f} {st.total_currency}"
        )
    if st.ticket_numbers:
        tickets = "; ".join(st.ticket_numbers)
        if row.get("항공권번호", "") != tickets:
            changes["항공권번호"] = tickets

    if st.state == PAYMENT_CANCELLED:
        if cur != STATUS_CANCELLED:
            changes.update({"상태": STATUS_CANCELLED, "비고": f"Duffel 조회: {_fmt(st.cancelled_at)} 취소됨"})
            if cur == STATUS_PAID:
                msgs.append("[주의] 결제완료였던 주문이 취소됐습니다. Duffel에서 환불 여부를 확인하고 환불액을 기록하세요.")
            else:
                msgs.append("결제 마감 경과 등으로 취소된 것으로 보입니다.")
    elif st.state == PAYMENT_PAID:
        if cur == STATUS_HELD:
            changes.update(
                {
                    "상태": STATUS_PAID,
                    "결제확인일시": (st.paid_at or now).astimezone().isoformat(timespec="seconds"),
                    "비고": "Duffel 조회로 결제 확인",
                }
            )
            if row["영수증"] != "Y":
                msgs.append("영수증을 받으면 receipt --yes 로 표시하세요.")
        elif cur == STATUS_CANCELLED:
            msgs.append("[주의] 내역은 '취소'인데 Duffel은 결제완료입니다. 직접 확인하세요 (내역은 바꾸지 않음).")
        elif not row["결제확인일시"] and st.paid_at:
            changes["결제확인일시"] = st.paid_at.astimezone().isoformat(timespec="seconds")
    elif st.state == PAYMENT_AWAITING:
        if cur == STATUS_PAID:
            msgs.append(
                "[주의] 내역은 '결제완료'인데 Duffel은 아직 결제대기입니다. 실제 결제 여부를 확인하세요 (내역은 바꾸지 않음)."
            )
        elif cur == STATUS_CANCELLED:
            msgs.append("[주의] 내역은 '취소'인데 Duffel은 결제대기입니다. 직접 확인하세요 (내역은 바꾸지 않음).")
        if st.deadline_passed(now):
            msgs.append(f"[주의] 결제 마감({_fmt(st.payment_required_by)})이 지났습니다. 곧 자동 취소될 수 있습니다.")
        else:
            msgs.append(f"결제 마감: {_fmt(st.payment_required_by)}")
    return changes, msgs


def sync(
    api_key: str,
    path: Path = LEDGER_PATH,
    order_id: str | None = None,
    dry_run: bool = False,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> tuple[list[str], int]:
    """Duffel에서 주문 상태를 조회해 내역에 반영한다. (출력 문구, 조회 실패 건수)."""
    rows = read_rows(path)
    if order_id:
        target = _find(rows, order_id)
        if target["구분"] not in ("", KIND_FLIGHT):
            raise LedgerError(f"{order_id}는 {target['구분']} 내역이라 Duffel에서 조회할 수 없습니다.")
        targets: Iterable[dict] = [target]
    else:
        # Duffel 조회는 항공 주문만 (호텔은 Duffel 주문이 아님)
        targets = [r for r in rows if r["상태"] != STATUS_CANCELLED and r["구분"] in ("", KIND_FLIGHT)]
    out: list[str] = []
    failures = 0
    changed_any = False
    checked_at = now_local()
    for row in targets:
        head = f"{row['예약번호'] or '-'} {row['주문ID']}"
        try:
            st = fetch_order_status(row["주문ID"], api_key, client=client)
        except (DuffelError, ValueError, KeyError) as e:
            failures += 1
            out.append(f"{head}: [조회 실패] {e}")
            continue
        changes, msgs = decide_update(row, st, now)
        if "상태" in changes:
            out.append(f"{head}: {row['상태']} → {changes['상태']} (Duffel: {st.label})")
        else:
            out.append(f"{head}: {row['상태']} 유지 (Duffel: {st.label})")
        if "항공권번호" in changes:
            out.append(f"    항공권번호: {changes['항공권번호']}")
        out += [f"    {m}" for m in msgs]
        if not dry_run:
            row.update(changes)
            row["최종조회일시"] = checked_at
            changed_any = True
    if not targets:
        out.append("조회할 주문이 없습니다 (취소된 주문은 --order로 지정할 때만 조회).")
    if changed_any:
        write_rows(rows, path)
    if dry_run:
        out.append("(--dry-run: 내역 파일은 바꾸지 않았습니다)")
    return out, failures


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="회계용 예약 내역")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="내역과 통화별 합계 보기")
    mp = sub.add_parser("mark-paid", help="사람이 결제한 주문을 결제완료로 표시")
    mp.add_argument("--order", required=True)
    mp.add_argument("--receipt", action="store_true", help="영수증을 받았으면 표시")
    sy = sub.add_parser("sync", help="Duffel에서 결제 여부를 조회해 내역에 반영 (조회 전용)")
    sy.add_argument("--order", help="특정 주문만 조회 (생략 시 취소 외 전체)")
    sy.add_argument("--dry-run", action="store_true", help="조회 결과만 보고 내역은 바꾸지 않음")
    ah = sub.add_parser("add-hotel", help="호텔 예약 정보 파일을 확인 후 내역에 추가")
    ah.add_argument("--booking", type=Path, default=DATA_DIR / "hotel_booking.json")
    mc = sub.add_parser("mark-cancelled", help="취소된 예약을 내역에 표시 (기록만 바꿈, 예약을 취소하지 않음)")
    mc.add_argument("--order", required=True, help="주문ID (호텔은 HOTEL-...)")
    mc.add_argument("--refund", help="환불액 (생략 시 0.00)")
    mc.add_argument("--currency", help="환불 통화 (생략 시 원래 통화)")
    rc = sub.add_parser("receipt", help="영수증 여부 변경")
    rc.add_argument("--order", required=True)
    g = rc.add_mutually_exclusive_group(required=True)
    g.add_argument("--yes", dest="receipt", action="store_true")
    g.add_argument("--no", dest="receipt", action="store_false")
    return p.parse_args(argv)


def main(
    argv: list[str] | None = None,
    path: Path = LEDGER_PATH,
    client: httpx.Client | None = None,
    input_fn: Callable[[str], str] = input,
) -> int:
    args = parse_args(argv)
    try:
        if args.command == "add-hotel":
            booking = load_booking(args.booking, allow_past=True)  # 다녀온 출장도 기록할 수 있게
            row = hotel_row(booking)
            print(format_hotel_preview(row))
            answer = input_fn("이 내용으로 회계 내역에 추가할까요? (yes/no): ").strip().lower()
            if answer not in {"yes", "y"}:
                print("추가하지 않았습니다.")
                return 0
            add_hotel_booking(booking, path)
            print(f"회계 내역에 추가했습니다: {path}")
            return 0
        if args.command == "mark-cancelled":
            if args.refund is not None:
                try:
                    refund = f"{Decimal(args.refund):.2f}"
                except InvalidOperation as e:
                    raise LedgerError(f"환불액을 숫자로 입력하세요: {args.refund!r}") from e
            else:
                refund = None
            currency = args.currency.upper() if args.currency else None
            r = mark_cancelled(args.order, refund, currency, path)
            print(f"내역을 '취소'로 표시했습니다: {r['예약번호']} (환불 {r['환불액']} {r['환불통화']})")
            print("※ 내역만 바꿨습니다. 실제 예약 취소는 예약한 곳에서 직접 하세요.")
            return 0
        if args.command == "sync":
            api_key = load_duffel_key()
            lines, failures = sync(api_key, path, args.order, args.dry_run, client)
            print("\n".join(lines))
            if failures:
                print(f"[오류] {failures}건 조회에 실패했습니다. 위 원인을 확인하세요.", file=sys.stderr)
                return 1
            return 0
        if args.command == "list":
            print(format_list(read_rows(path)))
        elif args.command == "mark-paid":
            r = mark_paid(args.order, args.receipt, path)
            print(f"결제완료로 표시했습니다: {r['예약번호']} {r['금액']} {r['통화']} (영수증 {r['영수증']})")
        else:
            r = set_receipt(args.order, args.receipt, path)
            print(f"영수증 여부를 {r['영수증']}(으)로 바꿨습니다: {r['예약번호']}")
    except (LedgerError, ConfigError, GuestEmailError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
