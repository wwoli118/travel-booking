"""호텔 예약 전 확인 (CLAUDE.md 원칙 5). 예약·결제는 하지 않는다 (원칙 1).

1. 호텔 검색 결과 CSV에서 숙소를 고른다.
2. 그 숙소의 객실 요금을 모두 조회해 취소 규정·식사·결제 방식과 함께 보여주고 하나를 고른다.
3. 견적(quote)으로 가격·가용성을 다시 확인한다 (예약·결제 아님).
4. 숙소·일정·객실·객실별 투숙객(여권 영문 이름)·총액·취소 규정을 요약하고 yes 확인을 받는다.
5. yes면 준비 내용(data/drafts/hotel_draft_*.json)과 hotel_booking.json 초안을 저장한다.
   사람이 Duffel 대시보드에서 예약·결제한 뒤 초안에 예약번호 등을 채우면
   안내 이메일(src.guest_email)과 회계 내역(src.ledger add-hotel)으로 이어진다.

실행:
    python -m src.hotel_summary --csv data/hotels_manila-makati_2026-12-20_xxx.csv --rank 1
    python -m src.hotel_summary --csv ... --rank 2 --rate 1
    python -m src.hotel_summary --csv ... --room-of 1,1,2      # 3명이 2실: 1·2번째 사람은 1번 객실
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from .compare import MixedCurrencyError, sort_by_amount
from .config import DATA_DIR, ConfigError, load_duffel_key
from .duffel import BlockedEndpointError, DuffelError
from .hotel_models import HotelQuote, HotelRate, fmt_with_offset
from .hotel_providers.duffel_stays import create_quote, fetch_rates
from .summary import DRAFTS_DIR, LINE, ask_confirmation
from .travelers import ADULT_AGE, Traveler, TravelerFile, TravelerFileError, age_on, load_travelers

DEFAULT_TRAVELERS = DATA_DIR / "travelers.json"


class HotelSummaryError(Exception):
    """예약 전 확인을 계속할 수 없을 때. 원인을 담는다."""


# ---------------------------------------------------------------- 선택

@dataclass(frozen=True)
class HotelSelection:
    search_result_id: str
    hotel_name: str
    check_in: date
    check_out: date
    rooms: int
    adults: int
    searched_amount: Decimal
    searched_currency: str


def select_from_csv(csv_path: Path, rank: int) -> HotelSelection:
    if not csv_path.exists():
        raise HotelSummaryError(f"호텔 검색 결과 파일이 없습니다: {csv_path}")
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if r.get("rank") == str(rank):
            try:
                return HotelSelection(
                    search_result_id=r["search_result_id"],
                    hotel_name=r["hotel_name"],
                    check_in=date.fromisoformat(r["check_in"]),
                    check_out=date.fromisoformat(r["check_out"]),
                    rooms=int(r["rooms"]),
                    adults=int(r["adults"]),
                    searched_amount=Decimal(r["total_amount"]),
                    searched_currency=r["total_currency"],
                )
            except (KeyError, ValueError) as e:
                raise HotelSummaryError(f"{csv_path.name}이 호텔 검색 결과 형식이 아닙니다: {e}") from e
    raise HotelSummaryError(f"{csv_path.name}에 순위 {rank}번이 없습니다 (1~{len(rows)}번 중 선택).")


# ---------------------------------------------------------------- 객실 배정

def assign_rooms(
    travelers: list[Traveler], rooms: int, check_in: date, room_of: list[int] | None = None
) -> list[tuple[int, Traveler]]:
    """투숙객을 객실에 배정한다. 기본은 1인 1실(인원 = 객실 수일 때 순서대로)."""
    if not travelers:
        raise HotelSummaryError("투숙객이 없습니다.")
    if room_of is None:
        if len(travelers) != rooms:
            raise HotelSummaryError(
                f"투숙객 {len(travelers)}명, 객실 {rooms}실입니다. 1인 1실이 아니면 --room-of로 배정하세요 "
                "(예: 3명 2실 → --room-of 1,1,2)."
            )
        room_of = list(range(1, rooms + 1))
    if len(room_of) != len(travelers):
        raise HotelSummaryError(f"--room-of 개수({len(room_of)})가 투숙객 수({len(travelers)})와 다릅니다.")
    bad = sorted({r for r in room_of if not 1 <= r <= rooms})
    if bad:
        raise HotelSummaryError(f"없는 객실 번호입니다: {bad} (객실 1~{rooms})")
    empty = [r for r in range(1, rooms + 1) if r not in room_of]
    if empty:
        raise HotelSummaryError(f"투숙객이 없는 객실이 있습니다: {', '.join(f'{r}번' for r in empty)}")
    minors = [t.passport_name for t in travelers if age_on(t.born_on, check_in) < ADULT_AGE]
    if minors:
        raise HotelSummaryError(
            f"체크인({check_in.isoformat()}) 기준 만 {ADULT_AGE}세 미만이 있습니다: {', '.join(minors)}. "
            "현재는 성인만 지원합니다."
        )
    names = [t.passport_name for t in travelers]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise HotelSummaryError(f"같은 이름의 투숙객이 중복되어 있습니다: {', '.join(dupes)}")
    return sorted(zip(room_of, travelers), key=lambda x: x[0])


def parse_room_of(text: str | None) -> list[int] | None:
    if not text:
        return None
    try:
        return [int(x) for x in text.split(",")]
    except ValueError as e:
        raise HotelSummaryError(f"--room-of는 쉼표로 구분한 객실 번호로 입력하세요 (예: 1,1,2): {text!r}") from e


# ---------------------------------------------------------------- 요금 고르기

def sort_rates(rates: list[HotelRate], now: datetime) -> list[HotelRate]:
    """총액 오름차순. 같은 금액이면 무료 취소 가능한 요금 먼저. 통화가 섞이면 오류."""
    return sort_by_amount(rates, tiebreak=lambda r: (r.free_cancellation_until(now) is None, r.room_name))


def rate_warnings(rate: HotelRate) -> list[str]:
    out = []
    if rate.available_payment_methods and "balance" not in rate.available_payment_methods:
        out.append("Duffel 잔액으로 결제할 수 없는 요금입니다 (카드 결제·보증 필요). Duffel 대시보드에서 예약 가능한지 확인하세요.")
    if rate.loyalty_programme_required:
        out.append("이 요금은 호텔 회원 번호가 있어야 예약할 수 있습니다.")
    return out


def format_rates(rates: list[HotelRate], now: datetime) -> str:
    lines = [f"{'#':>3}  {'객실':<30} {'식사':<10} {'결제':<20} {'취소':<22} {'총액':>18}"]
    for i, r in enumerate(rates, start=1):
        free = r.free_cancellation_until(now)
        cancel = f"무료 취소 ~{free:%m-%d %H:%M}" if free else ("환불 불가" if not r.cancellation else "부분 환불")
        mark = " *" if rate_warnings(r) else ""
        lines.append(
            f"{i:>3}  {r.room_name[:30]:<30} {r.board_label:<10} {r.payment_label[:20]:<20} {cancel:<22} {r.price_label:>18}{mark}"
        )
    if any(rate_warnings(r) for r in rates):
        lines.append("  * 예약 제약이 있는 요금 (선택하면 자세히 표시)")
    return "\n".join(lines)


def choose_rate(
    rates: list[HotelRate], input_fn: Callable[[str], str], preset: int | None, attempts: int = 3
) -> HotelRate | None:
    if preset is not None:
        if not 1 <= preset <= len(rates):
            raise HotelSummaryError(f"--rate는 1~{len(rates)} 사이로 지정하세요: {preset}")
        return rates[preset - 1]
    for _ in range(attempts):
        answer = input_fn(f"요금 번호를 고르세요 (1~{len(rates)}, 그만두려면 엔터): ").strip()
        if answer == "":
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(rates):
            return rates[int(answer) - 1]
        print(f"1~{len(rates)} 사이 숫자로 입력해 주세요.")
    return None


# ---------------------------------------------------------------- 요약

def price_notes(quote: HotelQuote, rate: HotelRate) -> tuple[str, bool]:
    """(설명, 주의 필요). 고른 요금과 견적 총액 비교."""
    if quote.total_currency != rate.total_currency:
        return f"(요금 조회 시 {rate.price_label} — 통화가 바뀌어 비교할 수 없음)", True
    diff = quote.total_amount - rate.total_amount
    if diff == 0:
        return f"(요금 조회 시 {rate.price_label}, 변동 없음)", False
    if diff > 0:
        return f"(요금 조회 시 {rate.price_label}, {diff:,.2f} {quote.total_currency} 인상)", True
    return f"(요금 조회 시 {rate.price_label}, {-diff:,.2f} {quote.total_currency} 인하)", False


def build_summary(
    quote: HotelQuote,
    selected: HotelRate,
    assignments: list[tuple[int, Traveler]],
    trip_purpose: str | None,
    cost_center: str | None,
    now: datetime,
) -> tuple[str, bool]:
    """(요약 문자열, 가격 주의 여부).

    가격 비교는 고른 요금(selected) 기준, 취소·식사·결제 규정은 견적에 실린 최신 요금을 우선한다.
    """
    p = quote.property
    rate = quote.rate or selected
    note, warn = price_notes(quote, selected)
    weekdays = "월화수목금토일"

    def d(x: date) -> str:
        return f"{x.isoformat()}({weekdays[x.weekday()]})"

    stars = f" (★{p.rating})" if p.rating else ""
    guests = "\n          ".join(f"{room}번 객실: {t.passport_name}" for room, t in assignments)
    lines = [
        LINE,
        "호텔 예약 전 확인".center(40),
        LINE,
        f"숙소      {p.name}{stars}",
        f"주소      {p.address or '정보 없음'}",
        f"일정      {d(quote.check_in)} ~ {d(quote.check_out)}, {quote.nights}박",
    ]
    if p.check_in_after or p.check_out_before:
        lines.append(f"          체크인 {p.check_in_after or '-'} 이후 / 체크아웃 {p.check_out_before or '-'} 이전")
    lines += [
        f"객실      {quote.rooms}실 / {rate.room_name}, {rate.board_label}",
        f"투숙객    {guests}",
        f"          (여권 영문 표기)",
        f"총액      {quote.total_amount:,.2f} {quote.total_currency} (1박 평균 {quote.nightly_average:,.2f} {quote.total_currency})  {note}",
    ]
    if quote.due_at_accommodation_amount:
        lines.append(
            f"현장 추가 {quote.due_at_accommodation_amount:,.2f} {quote.due_at_accommodation_currency or ''} "
            "(총액에 미포함, 숙소에서 따로 냄)"
        )
    lines.append(f"결제      {rate.payment_label}")
    if quote.deposit_amount:
        lines.append(f"          보증금 {quote.deposit_amount:,.2f} {quote.deposit_currency or ''}")
    cancel = rate.cancellation_lines(now)
    lines.append(f"취소      {cancel[0]}")
    lines += [f"          {c}" for c in cancel[1:]]
    if rate.conditions:
        lines.append("요금 조건 " + " / ".join(t for t, _ in rate.conditions if t))
    if rate.expires_at:
        lines.append(f"요금 유효 {fmt_with_offset(rate.expires_at.astimezone())} 까지")
    if trip_purpose or cost_center:
        lines.append(f"출장      목적: {trip_purpose or '-'} / 비용 부서: {cost_center or '-'}")
    for w in rate_warnings(rate):
        lines.append(f"[주의] {w}")
    if warn:
        lines += ["", "!!! 주의: 요금 조회 때보다 가격이 올랐거나 비교할 수 없습니다. 총액을 다시 확인하세요. !!!"]
    lines += [
        "※ 이 도구는 예약·결제를 하지 않습니다. 확인 후 Duffel 대시보드에서 사람이 직접 예약합니다.",
        LINE,
    ]
    return "\n".join(lines), warn


# ---------------------------------------------------------------- 저장

def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", text)


def save_outputs(
    quote: HotelQuote,
    rate: HotelRate,
    selection: HotelSelection,
    assignments: list[tuple[int, Traveler]],
    travelers: TravelerFile,
    confirmed_at: datetime,
    data_dir: Path = DATA_DIR,
    drafts_dir: Path = DRAFTS_DIR,
) -> tuple[Path, Path]:
    """준비 내용 JSON과 hotel_booking.json 초안을 저장한다."""
    stamp = confirmed_at.strftime("%Y%m%d-%H%M%S")
    p = quote.property
    guests = [
        {"room": room, "given_name": t.given_name, "family_name": t.family_name} for room, t in assignments
    ]
    draft = {
        "status": "confirmed_for_preparation",
        "kind": "hotel",
        "note": "호텔 예약 준비용 확인 기록입니다. 예약·결제는 하지 않았습니다.",
        "confirmed_at": confirmed_at.isoformat(timespec="seconds"),
        "provider": "duffel",
        "search_result_id": selection.search_result_id,
        "rate_id": rate.rate_id,
        "quote_id": quote.quote_id,
        "accommodation": p.model_dump(mode="json"),
        "check_in": quote.check_in.isoformat(),
        "check_out": quote.check_out.isoformat(),
        "nights": quote.nights,
        "rooms": quote.rooms,
        "room_name": rate.room_name,
        "board_type": rate.board_type,
        "payment_type": rate.payment_type,
        "price": {"total_amount": f"{quote.total_amount:.2f}", "total_currency": quote.total_currency},
        "due_at_accommodation": (
            None
            if not quote.due_at_accommodation_amount
            else {"amount": f"{quote.due_at_accommodation_amount:.2f}", "currency": quote.due_at_accommodation_currency}
        ),
        "cancellation_timeline": [s.model_dump(mode="json") for s in rate.cancellation],
        "conditions": [{"title": t, "description": d} for t, d in rate.conditions],
        "guests": guests,
        "trip": travelers.trip.model_dump(mode="json"),
    }
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft_path = drafts_dir / f"hotel_draft_{_safe(p.accommodation_id)}_{quote.check_in.isoformat()}_{stamp}.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")

    prepaid = {"pay_now": True, "guarantee": False}.get(rate.payment_type or "")  # deposit 등은 미기재
    booking = {
        "_comment": "예약 후 비어 있는 booking.reference, booker.name, payment.card_holder를 채우세요.",
        "hotel": {"name": p.name, "email": p.email},
        "booking": {
            "reference": "",
            "platform": "Duffel",
            "check_in": quote.check_in.isoformat(),
            "check_out": quote.check_out.isoformat(),
            "rooms": quote.rooms,
        },
        "booker": {"name": "", "company": None, "email": None, "phone": None},
        "payment": {"card_holder": "", "card_holder_stays": False, "prepaid": prepaid},
        "guests": guests,
        "price": {"total_amount": f"{quote.total_amount:.2f}", "currency": quote.total_currency},
        "trip": travelers.trip.model_dump(mode="json"),
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    booking_path = data_dir / f"hotel_booking_{_safe(p.accommodation_id)}_{quote.check_in.isoformat()}.json"
    booking_path.write_text(json.dumps(booking, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft_path, booking_path


# ---------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="호텔 예약 전 확인 (예약·결제 없음)")
    p.add_argument("--csv", type=Path, required=True, help="호텔 검색 결과 CSV (python -m src.hotels 결과)")
    p.add_argument("--rank", type=int, default=1, help="CSV에서 고를 숙소 순위 (기본 1)")
    p.add_argument("--rate", type=int, help="요금 번호를 미리 지정 (생략 시 목록을 보고 고름)")
    p.add_argument("--travelers", type=Path, default=DEFAULT_TRAVELERS, help="투숙객 정보 JSON")
    p.add_argument("--room-of", help="투숙객별 객실 번호, travelers.json 순서대로 (예: 1,1,2)")
    return p.parse_args(argv)


def main(
    argv: list[str] | None = None,
    input_fn: Callable[[str], str] = input,
    client: httpx.Client | None = None,
    data_dir: Path = DATA_DIR,
    drafts_dir: Path = DRAFTS_DIR,
    now: datetime | None = None,
) -> int:
    args = parse_args(argv)
    now = now or datetime.now(timezone.utc)
    try:
        selection = select_from_csv(args.csv, args.rank)
        travelers = load_travelers(args.travelers)
        assignments = assign_rooms(
            travelers.travelers, selection.rooms, selection.check_in, parse_room_of(args.room_of)
        )
        if len(travelers.travelers) != selection.adults:
            raise HotelSummaryError(
                f"검색 인원({selection.adults}명)과 투숙객 수({len(travelers.travelers)}명)가 다릅니다. 같은 인원으로 다시 검색하세요."
            )
        api_key = load_duffel_key()
    except (HotelSummaryError, TravelerFileError, ConfigError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 2

    print(f"요금 조회 중: {selection.hotel_name} (Duffel 테스트 환경)")
    try:
        prop, rates = fetch_rates(selection.search_result_id, api_key, client=client)
        rates = sort_rates(rates, now)
    except (DuffelError, BlockedEndpointError, MixedCurrencyError, KeyError) as e:
        print(f"[오류] 요금을 조회하지 못했습니다: {e}", file=sys.stderr)
        return 1
    if not rates:
        print("예약 가능한 요금이 없습니다. 다른 숙소를 고르거나 다시 검색하세요.")
        return 0

    print(f"\n{prop.name} 객실 요금 ({len(rates)}건, 총액 순)")
    print(format_rates(rates, now))
    try:
        rate = choose_rate(rates, input_fn, args.rate)
    except HotelSummaryError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 2
    if rate is None:
        print("요금을 고르지 않아 종료합니다.")
        return 0
    if rate.expires_at and rate.expires_at <= now:
        print("[중단] 이 요금의 유효 시간이 지났습니다. 다시 검색해 주세요.", file=sys.stderr)
        return 1

    print(f"\n견적 요청 중 (가격·가용성 재확인, 예약·결제 아님): {rate.rate_id}")
    try:
        quote = create_quote(rate.rate_id, api_key, client=client)
    except (DuffelError, BlockedEndpointError, KeyError) as e:
        print(f"[오류] 견적을 받지 못했습니다 (요금이 마감됐을 수 있습니다): {e}", file=sys.stderr)
        return 1
    if quote.rooms != selection.rooms or quote.check_in != selection.check_in or quote.check_out != selection.check_out:
        print("[중단] 견적의 일정·객실 수가 검색 조건과 다릅니다. 다시 검색해 주세요.", file=sys.stderr)
        return 1

    summary, _ = build_summary(
        quote, rate, assignments, travelers.trip.purpose, travelers.trip.cost_center, now
    )
    print(summary)
    if not ask_confirmation(input_fn, question="이 내용으로 호텔 예약을 준비할까요?"):
        print("확인되지 않아 아무것도 저장하지 않았습니다.")
        return 0

    draft_path, booking_path = save_outputs(
        quote, quote.rate or rate, selection, assignments, travelers,
        datetime.now(timezone.utc).astimezone(), data_dir, drafts_dir,
    )
    print(f"준비 내용을 저장했습니다: {draft_path}")
    print(f"예약 정보 초안을 만들었습니다: {booking_path}")
    print(
        "다음 순서:\n"
        "  1) Duffel 대시보드에서 이 숙소·요금으로 직접 예약·결제\n"
        "  2) 초안의 booking.reference(예약번호), booker.name, payment.card_holder 채우기\n"
        f"  3) python -m src.guest_email --booking {booking_path}\n"
        f"  4) python -m src.ledger add-hotel --booking {booking_path}\n"
        "※ 아직 예약되지 않았습니다."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
