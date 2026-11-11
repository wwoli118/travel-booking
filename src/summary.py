"""예약 전 확인용 요약 (CLAUDE.md 원칙 5).

운임을 다시 조회하고, 탑승객 정보와 함께 요약을 보여준 뒤 사람의 확인(yes)을 받는다.
확인되면 data/drafts/에 예약 준비 내용을 JSON으로 남긴다.
주문 생성과 결제는 하지 않는다 (원칙 1).

실행:
    python -m src.summary --csv data/flights_ICN-MNL_2026-12-20_xxx.csv --rank 1
    python -m src.summary --offer off_0000... --travelers data/travelers.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import DATA_DIR, ConfigError, load_duffel_key
from .duffel import DuffelError
from .models import FareCondition
from .offers import (
    OfferDetails,
    OfferSelectionError,
    SelectedOffer,
    fetch_offer,
    select_from_csv,
)
from .travelers import (
    Traveler,
    TravelerFile,
    TravelerFileError,
    check_passports,
    check_travelers_for_flight,
    load_travelers,
)

DRAFTS_DIR = DATA_DIR / "drafts"
DEFAULT_TRAVELERS = DATA_DIR / "travelers.json"
LINE = "=" * 44


def price_change_note(
    details: OfferDetails, selected: SelectedOffer, baseline: str = "검색 시"
) -> tuple[str, bool]:
    """(설명, 가격이 올랐거나 비교 불가인지). 비교 기준 가격이 없으면 비교하지 않는다."""
    now_amt, now_cur = details.flight.total_amount, details.flight.total_currency
    if selected.searched_amount is None:
        return f"({baseline} 가격 정보 없음)", False
    was = f"{selected.searched_amount:,.2f} {selected.searched_currency}"
    if selected.searched_currency != now_cur:
        return f"({baseline} {was} — 통화가 바뀌어 비교할 수 없음)", True
    diff = now_amt - selected.searched_amount
    if diff == 0:
        return f"({baseline} {was}, 변동 없음)", False
    if diff > 0:
        return f"({baseline} {was}, {diff:,.2f} {now_cur} 인상)", True
    return f"({baseline} {was}, {-diff:,.2f} {now_cur} 인하)", False


def build_summary(
    details: OfferDetails,
    travelers: TravelerFile,
    selected: SelectedOffer,
    baseline: str = "검색 시",
    title: str = "예약 전 확인",
    extra_lines: list[str] | None = None,
    footer: str = "※ 이 단계에서는 예약(주문)과 결제를 하지 않습니다.",
) -> str:
    f = details.flight
    stops = f.stops_label
    if details.connection_airports:
        stops += f": {', '.join(details.connection_airports)}"
    price_note, price_warn = price_change_note(details, selected, baseline)

    lines = [
        LINE,
        title.center(40),
        LINE,
        f"항공편    {f.airline} {f.flight_numbers} ({stops})",
        f"구간      {f.origin} {f.departing_at:%Y-%m-%d %H:%M} → "
        f"{f.destination} {f.arriving_at:%Y-%m-%d %H:%M} (현지 시각)",
        f"좌석      성인 {len(details.passenger_ids)}석, {details.cabin_class or '좌석 등급 정보 없음'}",
        "탑승객    "
        + "\n          ".join(
            f"{i}) {t.passport_name} ({t.born_on.isoformat()}, {t.gender.upper()}"
            + (f", 여권 {t.passport.masked_number} ~{t.passport.expires_on.isoformat()}" if t.passport else "")
            + ")"
            for i, t in enumerate(travelers.travelers, start=1)
        ),
        f"총액      {f.price_label}  {price_note}",
        f"취소      출발 전 환불: {FareCondition.label(details.refund)}",
        f"변경      출발 전 변경: {FareCondition.label(details.change)}",
        f"수하물    {details.baggage}",
    ]
    if details.expires_at:
        local = details.expires_at.astimezone()
        lines.append(f"운임 유효 {local:%Y-%m-%d %H:%M %Z} 까지 (이후 가격이 바뀔 수 있음)")
    if travelers.trip.purpose or travelers.trip.cost_center:
        lines.append(
            f"출장      목적: {travelers.trip.purpose or '-'} / 비용 부서: {travelers.trip.cost_center or '-'}"
        )
    lines += extra_lines or []
    if price_warn:
        lines += ["", f"!!! 주의: {baseline}보다 가격이 올랐거나 비교할 수 없습니다. 총액을 다시 확인하세요. !!!"]
    if not details.live_mode:
        lines.append("※ 샌드박스 테스트 데이터입니다.")
    lines += [footer, LINE]
    return "\n".join(lines)


def ask_confirmation(
    input_fn: Callable[[str], str] = input,
    attempts: int = 3,
    question: str = "이 내용으로 예약을 준비할까요?",
) -> bool:
    """yes/no로 확인받는다. 명확한 yes가 아니면 진행하지 않는다."""
    for _ in range(attempts):
        answer = input_fn(f"{question} (yes/no): ").strip().lower()
        if answer in {"yes", "y"}:
            return True
        if answer in {"no", "n", ""}:
            return False
        print("yes 또는 no로 입력해 주세요.")
    return False


def save_draft(
    details: OfferDetails,
    travelers: TravelerFile,
    selected: SelectedOffer,
    confirmed_at: datetime,
    drafts_dir: Path = DRAFTS_DIR,
) -> Path:
    """확인된 예약 준비 내용을 JSON으로 저장한다. 탑승객은 Duffel 승객 ID와 순서대로 짝짓는다."""
    drafts_dir.mkdir(parents=True, exist_ok=True)
    f = details.flight

    def cond(c: FareCondition | None):
        return None if c is None else c.model_dump(mode="json")

    def pax(pid: str, t: Traveler) -> dict:
        return {"duffel_passenger_id": pid, "type": "adult", **t.model_dump(mode="json")}

    draft = {
        "status": "confirmed_for_preparation",
        "note": "예약 준비용 확인 기록입니다. 주문 생성·결제는 하지 않았습니다.",
        "confirmed_at": confirmed_at.isoformat(timespec="seconds"),
        "live_mode": details.live_mode,
        "offer_id": f.offer_id,
        "offer_expires_at": details.expires_at.isoformat() if details.expires_at else None,
        "flight": {
            "airline": f.airline,
            "flight_numbers": f.flight_numbers,
            "origin": f.origin,
            "destination": f.destination,
            "departing_at": f.departing_at.isoformat(timespec="minutes"),
            "arriving_at": f.arriving_at.isoformat(timespec="minutes"),
            "stops": f.stops,
            "connection_airports": details.connection_airports,
            "cabin_class": details.cabin_class,
        },
        "price": {"total_amount": f"{f.total_amount:.2f}", "total_currency": f.total_currency},
        "searched_price": (
            None
            if selected.searched_amount is None
            else {
                "total_amount": f"{selected.searched_amount:.2f}",
                "total_currency": selected.searched_currency,
            }
        ),
        "conditions": {"refund_before_departure": cond(details.refund), "change_before_departure": cond(details.change)},
        "baggage": details.baggage,
        "trip": travelers.trip.model_dump(mode="json"),
        "passengers": [pax(pid, t) for pid, t in zip(details.passenger_ids, travelers.travelers)],
    }
    stamp = confirmed_at.strftime("%Y%m%d-%H%M%S")
    path = drafts_dir / f"draft_{f.origin}-{f.destination}_{f.departing_at:%Y-%m-%d}_{stamp}.json"
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="예약 전 확인 요약 (주문·결제 없음)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="1단계 검색 결과 CSV 경로 (--rank와 함께)")
    src.add_argument("--offer", help="Duffel offer ID (off_...)")
    p.add_argument("--rank", type=int, default=1, help="CSV에서 고를 순위 (기본 1)")
    p.add_argument("--travelers", type=Path, default=DEFAULT_TRAVELERS, help="탑승객 정보 JSON")
    return p.parse_args(argv)


def main(
    argv: list[str] | None = None,
    input_fn: Callable[[str], str] = input,
    client: httpx.Client | None = None,
    drafts_dir: Path = DRAFTS_DIR,
) -> int:
    args = parse_args(argv)
    try:
        selected = (
            select_from_csv(args.csv, args.rank) if args.csv else SelectedOffer(offer_id=args.offer)
        )
        travelers = load_travelers(args.travelers)
        api_key = load_duffel_key()
    except (OfferSelectionError, TravelerFileError, ConfigError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 2

    print(f"운임 다시 조회 중: {selected.offer_id} (Duffel 테스트 환경)")
    try:
        details = fetch_offer(selected.offer_id, api_key, client=client)
        departure = details.flight.departing_at.date()
        check_travelers_for_flight(travelers.travelers, len(details.passenger_ids), departure)
        passport_warnings = check_passports(
            travelers.travelers, departure, details.identity_documents_required
        )
    except (DuffelError, TravelerFileError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1

    print(build_summary(details, travelers, selected))
    for w in passport_warnings:
        print(f"[주의] {w}")

    if details.is_expired():
        print(
            "[중단] 운임 유효 시간이 지났습니다. 가격이 바뀌었을 수 있으니 다시 검색해 주세요.",
            file=sys.stderr,
        )
        return 1

    if not ask_confirmation(input_fn):
        print("확인되지 않아 아무것도 저장하지 않았습니다.")
        return 0

    path = save_draft(details, travelers, selected, datetime.now(timezone.utc).astimezone(), drafts_dir)
    print(f"예약 준비 내용을 저장했습니다: {path}")
    print("※ 아직 예약되지 않았습니다. 주문 생성은 다음 단계에서 별도 확인 후 진행합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
