"""주문 준비 — 결제 직전까지만 (CLAUDE.md 원칙 1·4·5).

- prepare : 2단계에서 확인한 준비 내용(draft)으로 운임을 다시 조회하고 주문 요청서를 만든다.
            기본은 점검 모드(요청서 파일만 저장, API로 주문하지 않음).
            --hold 를 주면 최종 요약 + yes 확인 후 샌드박스에서 "보류(hold)" 주문을 만든다.
            보류 주문은 결제 없이 좌석·가격을 잡아두는 것이며, 결제는 사람이 Duffel 대시보드에서 직접 한다.
- cancel  : 보류 주문을 취소한다 (취소 견적 확인 → yes → 확정).

즉시 발권(instant) 주문과 결제 API는 이 프로젝트에서 호출하지 않는다 (src/duffel.py에서 차단).

실행:
    python -m src.orders prepare                 # 가장 최근 draft로 점검 모드
    python -m src.orders prepare --draft data/drafts/draft_xxx.json --hold
    python -m src.orders cancel --order ord_0000...
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic import ValidationError

from .config import DATA_DIR, ConfigError, load_duffel_key
from .duffel import BlockedEndpointError, DuffelError, duffel_request
from .offers import OfferDetails, SelectedOffer, fetch_offer, parse_ts
from .summary import DRAFTS_DIR, ask_confirmation, build_summary
from .travelers import (
    Traveler,
    TravelerFile,
    TravelerFileError,
    TripInfo,
    check_passports,
    check_travelers_for_flight,
)

ORDERS_DIR = DATA_DIR / "orders"


class OrderPrepError(Exception):
    """주문 준비를 계속할 수 없을 때. 원인을 담는다."""


def fmt_ts(dt: datetime | None) -> str:
    return "정보 없음" if dt is None else dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


# ---------------------------------------------------------------- draft

@dataclass
class Draft:
    path: Path
    offer_id: str
    price_amount: Decimal
    price_currency: str
    trip: TripInfo
    passengers: list[tuple[str, Traveler]]  # (Duffel 승객 ID, 탑승객)

    @property
    def traveler_file(self) -> TravelerFile:
        return TravelerFile(trip=self.trip, travelers=[t for _, t in self.passengers])


def latest_draft(drafts_dir: Path = DRAFTS_DIR) -> Path:
    files = sorted(drafts_dir.glob("draft_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise OrderPrepError(
            f"{drafts_dir}에 준비 내용이 없습니다. 먼저 python -m src.summary 로 확인을 받으세요."
        )
    return files[-1]


def load_draft(path: Path) -> Draft:
    if not path.exists():
        raise OrderPrepError(f"준비 내용 파일이 없습니다: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise OrderPrepError(f"{path} JSON 형식 오류: {e.msg}") from e
    if raw.get("status") != "confirmed_for_preparation":
        raise OrderPrepError(f"{path.name}은 확인(yes)된 준비 내용이 아닙니다.")
    try:
        passengers = []
        for p in raw["passengers"]:
            fields = {k: v for k, v in p.items() if k not in {"duffel_passenger_id", "type"}}
            passengers.append((p["duffel_passenger_id"], Traveler.model_validate(fields)))
        return Draft(
            path=path,
            offer_id=raw["offer_id"],
            price_amount=Decimal(raw["price"]["total_amount"]),
            price_currency=raw["price"]["total_currency"],
            trip=TripInfo.model_validate(raw.get("trip") or {}),
            passengers=passengers,
        )
    except (KeyError, ValidationError) as e:
        raise OrderPrepError(f"{path.name} 내용이 올바르지 않습니다: {e}") from e


# ---------------------------------------------------------------- 요청서

def build_order_body(offer_id: str, passengers: list[tuple[str, Traveler]], include_documents: bool) -> dict:
    """보류(hold) 주문 요청서. payments 항목은 넣지 않는다.

    여권 정보는 운임이 요구할 때만 보낸다 (불필요한 개인정보 전송을 줄이기 위해).
    """
    pax = []
    for pid, t in passengers:
        item = {
            "id": pid,
            "title": t.title,
            "gender": t.gender,
            "given_name": t.given_name,
            "family_name": t.family_name,
            "born_on": t.born_on.isoformat(),
            "email": t.email,
            "phone_number": t.phone_number,
        }
        if include_documents and t.passport:
            item["identity_documents"] = [
                {
                    "type": "passport",
                    "unique_identifier": t.passport.number,
                    "issuing_country_code": t.passport.issuing_country,
                    "expires_on": t.passport.expires_on.isoformat(),
                }
            ]
        pax.append(item)
    body = {"data": {"type": "hold", "selected_offers": [offer_id], "passengers": pax}}
    assert "payments" not in body["data"] and body["data"]["type"] == "hold"
    return body


def check_offer_against_draft(details: OfferDetails, draft: Draft) -> list[str]:
    """운임이 주문 가능한 상태인지 확인. 막아야 할 문제는 OrderPrepError, 주의점은 경고로 돌려준다."""
    if details.live_mode:
        raise OrderPrepError("운영(live) 운임입니다. 이 프로젝트는 샌드박스에서만 주문을 준비합니다.")
    draft_ids = sorted(pid for pid, _ in draft.passengers)
    if sorted(details.passenger_ids) != draft_ids:
        raise OrderPrepError(
            "운임의 승객 구성이 확인 당시와 다릅니다. 검색부터 다시 진행해 주세요."
        )
    departure = details.flight.departing_at.date()
    travelers = [t for _, t in draft.passengers]
    try:
        check_travelers_for_flight(travelers, len(details.passenger_ids), departure)
        return check_passports(travelers, departure, details.identity_documents_required)
    except TravelerFileError as e:
        raise OrderPrepError(
            f"{e}\n→ data/travelers.json을 고친 뒤 python -m src.summary 로 다시 확인받으세요."
        ) from e


def save_json(data: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------- prepare

def prepare(
    draft_path: Path | None,
    hold: bool,
    api_key: str,
    input_fn: Callable[[str], str] = input,
    client: httpx.Client | None = None,
    orders_dir: Path = ORDERS_DIR,
    drafts_dir: Path = DRAFTS_DIR,
) -> int:
    draft = load_draft(draft_path or latest_draft(drafts_dir))
    print(f"준비 내용: {draft.path.name}")
    print(f"운임 다시 조회 중: {draft.offer_id} (Duffel 테스트 환경)")
    details = fetch_offer(draft.offer_id, api_key, client=client)
    warnings = check_offer_against_draft(details, draft)

    body = build_order_body(draft.offer_id, draft.passengers, details.identity_documents_required)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    req_path = save_json(
        {"created_at": stamp, "draft": draft.path.name, "request": body},
        orders_dir / f"order_request_{draft.offer_id}_{stamp}.json",
    )

    order_lines = [
        "주문 방식 보류(hold) — 결제 없음"
        if not details.requires_instant_payment
        else "주문 방식 보류 불가 — 이 운임은 즉시 결제가 필요합니다",
        f"결제 마감 {fmt_ts(details.payment_required_by)} (보류 주문 시, 이후 자동 취소)",
        f"가격 보장 {fmt_ts(details.price_guarantee_expires_at)} 까지",
        "여권 정보 " + ("필요 — 요청서에 포함" if details.identity_documents_required else "불필요 — 보내지 않음"),
    ]
    print(
        build_summary(
            details,
            draft.traveler_file,
            SelectedOffer(draft.offer_id, draft.price_amount, draft.price_currency),
            baseline="2단계 확인 시",
            title="주문 전 최종 확인",
            extra_lines=order_lines,
            footer=(
                "※ 확인하면 결제 없이 보류 주문만 생성합니다. 결제는 사람이 직접 합니다."
                if hold
                else "※ 점검 모드: 주문을 만들지 않고 요청서만 저장합니다. 결제는 하지 않습니다."
            ),
        )
    )
    for w in warnings:
        print(f"[주의] {w}")

    if details.is_expired():
        print("[중단] 운임 유효 시간이 지났습니다. 검색부터 다시 진행해 주세요.", file=sys.stderr)
        return 1

    if not hold:
        print(f"점검 모드: 주문 요청서만 저장했습니다 (API로 주문하지 않음): {req_path}")
        if details.requires_instant_payment:
            print("※ 이 운임은 보류가 불가능해 --hold로도 주문할 수 없습니다. 다른 운임을 고르거나 사람이 직접 처리하세요.")
        else:
            print("보류 주문을 만들려면 --hold 옵션을 붙여 다시 실행하세요.")
        return 0

    if details.requires_instant_payment:
        print(
            "[중단] 이 운임은 즉시 결제가 필요해 보류 주문을 만들 수 없습니다. "
            "결제는 자동화하지 않으므로 다른 운임을 고르거나 사람이 직접 처리하세요.",
            file=sys.stderr,
        )
        return 1

    if not ask_confirmation(input_fn, question="보류 주문을 생성할까요? 결제는 되지 않습니다."):
        print("확인되지 않아 주문을 만들지 않았습니다.")
        return 0

    resp = duffel_request("POST", "/air/orders", api_key, client=client, json=body)["data"]
    payment_status = resp.get("payment_status") or {}
    result = {
        "status": "held_awaiting_payment",
        "note": "보류 주문입니다. 결제되지 않았습니다. 결제 마감 전에 사람이 직접 결제해야 발권됩니다.",
        "created_at": stamp,
        "draft": draft.path.name,
        "order_id": resp["id"],
        "booking_reference": resp.get("booking_reference"),
        "live_mode": resp.get("live_mode", False),
        "total_amount": resp.get("total_amount"),
        "total_currency": resp.get("total_currency"),
        "awaiting_payment": payment_status.get("awaiting_payment"),
        "payment_required_by": payment_status.get("payment_required_by"),
        "price_guarantee_expires_at": payment_status.get("price_guarantee_expires_at"),
        "trip": draft.trip.model_dump(mode="json"),
        "passengers": [t.passport_name for _, t in draft.passengers],
    }
    out = save_json(result, orders_dir / f"order_{result['booking_reference'] or resp['id']}_{stamp}.json")
    print(
        f"보류 주문 생성됨: 예약번호 {result['booking_reference']} / 주문 {result['order_id']}\n"
        f"총액 {result['total_amount']} {result['total_currency']}\n"
        f"※ 결제되지 않았습니다. {fmt_ts(parse_ts(result['payment_required_by']))}까지 "
        "Duffel 대시보드에서 직접 결제하세요. 기한이 지나면 자동 취소됩니다.\n"
        f"기록: {out}"
    )
    return 0


# ---------------------------------------------------------------- cancel

def cancel(
    order_id: str,
    api_key: str,
    input_fn: Callable[[str], str] = input,
    client: httpx.Client | None = None,
    orders_dir: Path = ORDERS_DIR,
) -> int:
    print(f"취소 견적 요청 중: {order_id} (견적만으로는 취소되지 않습니다)")
    quote = duffel_request(
        "POST", "/air/order_cancellations", api_key, client=client, json={"data": {"order_id": order_id}}
    )["data"]
    refund = f"{quote.get('refund_amount') or '0'} {quote.get('refund_currency') or ''}".strip()
    print(f"주문 {order_id} 취소 시 환불액: {refund} / 견적 유효: {fmt_ts(parse_ts(quote.get('expires_at')))}")

    if not ask_confirmation(input_fn, question="이 주문을 취소할까요?"):
        print("확인되지 않아 취소하지 않았습니다.")
        return 0

    confirmed = duffel_request(
        "POST", f"/air/order_cancellations/{quote['id']}/actions/confirm", api_key, client=client
    )["data"]
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    out = save_json(
        {
            "order_id": order_id,
            "cancellation_id": quote["id"],
            "refund_amount": quote.get("refund_amount"),
            "refund_currency": quote.get("refund_currency"),
            "confirmed_at": confirmed.get("confirmed_at"),
        },
        orders_dir / f"cancel_{order_id}_{stamp}.json",
    )
    print(f"취소 완료: {order_id} (확정 {confirmed.get('confirmed_at')})\n기록: {out}")
    return 0


# ---------------------------------------------------------------- CLI

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="주문 준비 (결제 없음, 샌드박스 전용)")
    sub = p.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("prepare", help="주문 요청서 생성 (기본 점검 모드)")
    pr.add_argument("--draft", type=Path, help="준비 내용 JSON (생략 시 가장 최근 것)")
    pr.add_argument("--hold", action="store_true", help="확인 후 샌드박스 보류 주문 생성 (결제 없음)")
    ca = sub.add_parser("cancel", help="보류 주문 취소")
    ca.add_argument("--order", required=True, help="Duffel 주문 ID (ord_...)")
    return p.parse_args(argv)


def main(
    argv: list[str] | None = None,
    input_fn: Callable[[str], str] = input,
    client: httpx.Client | None = None,
    orders_dir: Path = ORDERS_DIR,
    drafts_dir: Path = DRAFTS_DIR,
) -> int:
    args = parse_args(argv)
    try:
        api_key = load_duffel_key()
        if args.command == "prepare":
            return prepare(args.draft, args.hold, api_key, input_fn, client, orders_dir, drafts_dir)
        return cancel(args.order, api_key, input_fn, client, orders_dir)
    except (ConfigError, OrderPrepError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 2
    except (DuffelError, BlockedEndpointError) as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
