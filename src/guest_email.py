"""호텔에 보낼 "투숙객 안내 이메일" 초안 생성 (CLAUDE.md: 예약번호, 투숙객 이름, 결제 카드 소지자 미투숙 안내).

예약자(회사·대표)의 카드로 결제했지만 실제로는 출장자가 투숙하는 경우, 체크인 때
카드 소지자 본인 확인 문제로 막히지 않도록 호텔에 미리 알리는 이메일이다.

- 이메일을 보내지 않는다. 초안 파일만 만든다 (사람이 확인 후 직접 발송).
- 카드 번호는 받지 않는다. 마지막 4자리(card_last4)만 선택 입력.
- 예약 사이트의 PIN·비밀번호는 넣지 않는다.

실행:
    python -m src.guest_email --booking data/hotel_booking.json            # 영어 (기본)
    python -m src.guest_email --booking data/hotel_booking.json --lang ko  # 한국어
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import DATA_DIR
from .travelers import EMAIL_RE, E164_RE, normalize_passport_name

EMAILS_DIR = DATA_DIR / "emails"
DEFAULT_BOOKING = DATA_DIR / "hotel_booking.json"
ALLOWED_PAYMENT_KEYS = {"card_holder", "card_holder_stays", "card_last4", "prepaid"}

WEEKDAYS_KO = "월화수목금토일"


class GuestEmailError(Exception):
    """예약 정보 파일을 읽거나 검증하지 못했을 때. 원인을 담는다."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HotelInfo(_Strict):
    name: str = Field(min_length=1)
    email: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v and not EMAIL_RE.match(v):
            raise ValueError(f"호텔 이메일 형식이 아닙니다: {v!r}")
        return v or None


class BookingInfo(_Strict):
    reference: str = Field(min_length=1)
    platform: str | None = None  # 예: Booking.com, Agoda, 호텔 직접
    check_in: date
    check_out: date
    rooms: int = Field(ge=1, le=9)


class Booker(_Strict):
    name: str = Field(min_length=1)
    company: str | None = None
    email: str | None = None
    phone: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v and not EMAIL_RE.match(v):
            raise ValueError(f"예약자 이메일 형식이 아닙니다: {v!r}")
        return v or None

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = re.sub(r"[\s\-()]", "", str(v))
        if not E164_RE.match(v):
            raise ValueError(f"전화번호는 국가번호 포함 국제 형식으로 입력하세요 (예: +821012345678): {v!r}")
        return v


class PaymentInfo(_Strict):
    card_holder: str
    card_holder_stays: bool = False
    card_last4: str | None = None
    prepaid: bool | None = None  # True: 선결제 완료 / False: 현장 결제 / None: 언급 안 함

    @field_validator("card_holder", mode="before")
    @classmethod
    def _holder(cls, v: str) -> str:
        return normalize_passport_name(v)

    @field_validator("card_last4", mode="before")
    @classmethod
    def _last4(cls, v) -> str | None:
        if v in (None, ""):
            return None
        v = str(v).strip()
        if not re.fullmatch(r"\d{4}", v):
            raise ValueError("card_last4에는 카드 번호 마지막 4자리 숫자만 넣으세요 (전체 카드 번호 금지).")
        return v


class Guest(_Strict):
    room: int = Field(ge=1)
    given_name: str
    family_name: str

    @field_validator("given_name", "family_name", mode="before")
    @classmethod
    def _name(cls, v: str) -> str:
        return normalize_passport_name(v)

    @property
    def passport_name(self) -> str:
        return f"{self.family_name} {self.given_name}"


class HotelBooking(_Strict):
    hotel: HotelInfo
    booking: BookingInfo
    booker: Booker
    payment: PaymentInfo
    guests: list[Guest] = Field(min_length=1)

    @property
    def nights(self) -> int:
        return (self.booking.check_out - self.booking.check_in).days

    def guests_by_room(self) -> dict[int, list[Guest]]:
        out: dict[int, list[Guest]] = {}
        for g in sorted(self.guests, key=lambda g: g.room):
            out.setdefault(g.room, []).append(g)
        return out

    @model_validator(mode="after")
    def _consistency(self) -> "HotelBooking":
        b = self.booking
        if b.check_out <= b.check_in:
            raise ValueError(
                f"체크아웃({b.check_out.isoformat()})은 체크인({b.check_in.isoformat()}) 다음 날 이후여야 합니다."
            )
        over = sorted({g.room for g in self.guests if g.room > b.rooms})
        if over:
            raise ValueError(
                f"객실 수는 {b.rooms}개인데 {', '.join(f'{r}번' for r in over)} 객실에 투숙객이 있습니다."
            )
        empty = [r for r in range(1, b.rooms + 1) if r not in {g.room for g in self.guests}]
        if empty:
            raise ValueError(f"투숙객이 없는 객실이 있습니다: {', '.join(f'{r}번' for r in empty)} 객실")
        names = [g.passport_name for g in self.guests]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"같은 이름의 투숙객이 중복되어 있습니다: {', '.join(dupes)}")
        holder_is_guest = self.payment.card_holder in names or self.payment.card_holder in {
            f"{g.given_name} {g.family_name}" for g in self.guests
        }
        if holder_is_guest and not self.payment.card_holder_stays:
            raise ValueError(
                f"카드 소지자({self.payment.card_holder})가 투숙객 명단에 있는데 card_holder_stays가 false입니다. "
                "미투숙 안내가 잘못 나가지 않도록 값을 확인하세요."
            )
        return self


def load_booking(path: Path, today: date | None = None) -> HotelBooking:
    if not path.exists():
        raise GuestEmailError(
            f"예약 정보 파일이 없습니다: {path}. hotel_booking.example.json을 복사해서 작성하세요."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise GuestEmailError(f"{path} JSON 형식 오류 ({e.lineno}행 {e.colno}열): {e.msg}") from e

    payment = raw.get("payment") if isinstance(raw, dict) else None
    if isinstance(payment, dict):
        extra = sorted(k for k in payment if k not in ALLOWED_PAYMENT_KEYS)
        if extra:
            raise GuestEmailError(
                f"payment에 허용되지 않은 항목이 있습니다: {', '.join(extra)}. "
                "카드 번호·유효기간·CVC 같은 카드 정보는 넣지 마세요 (마지막 4자리 card_last4만 허용)."
            )
    try:
        booking = HotelBooking.model_validate(raw)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            where = ".".join(str(x) for x in err["loc"]) or "전체"
            lines.append(f"  - {where}: {err['msg'].removeprefix('Value error, ')}")
        raise GuestEmailError(f"{path} 내용 오류:\n" + "\n".join(lines)) from e

    today = today or date.today()
    if booking.booking.check_out <= today:
        raise GuestEmailError(f"이미 체크아웃 날짜({booking.booking.check_out.isoformat()})가 지난 예약입니다.")
    return booking


# ---------------------------------------------------------------- 본문

def _date_en(d: date) -> str:
    return d.strftime("%a, %d %b %Y")


def _date_ko(d: date) -> str:
    return f"{d.year}년 {d.month}월 {d.day}일({WEEKDAYS_KO[d.weekday()]})"


def render_en(b: HotelBooking) -> tuple[str, str]:
    bk, pay = b.booking, b.payment
    subject = f"Guest name notice - Booking No. {bk.reference} (Check-in {bk.check_in:%d %b %Y})"
    via = f" made via {bk.platform}" if bk.platform else ""
    rooms = "\n".join(
        f"  Room {r}: {', '.join(g.passport_name for g in gs)}" for r, gs in b.guests_by_room().items()
    )
    lines = [
        f"Dear {b.hotel.name} Reservations Team,",
        "",
        f"I am writing regarding the reservation below{via}.",
        "",
        f"  Booking number : {bk.reference}",
        f"  Check-in       : {_date_en(bk.check_in)}",
        f"  Check-out      : {_date_en(bk.check_out)} ({b.nights} night{'s' if b.nights > 1 else ''})",
        f"  Rooms          : {bk.rooms}",
        "",
        "Guest name(s), as shown in passport:",
        rooms,
        "",
    ]
    if not pay.card_holder_stays:
        payer = b.booker.company or b.booker.name
        card = f" (card ending in {pay.card_last4})" if pay.card_last4 else ""
        lines += [
            f"This reservation was made by {payer} for business travel. "
            f"The cardholder, {pay.card_holder}{card}, will NOT be staying at the hotel.",
            "Please register the guest(s) above for check-in, and kindly do not require "
            "the cardholder's presence or the physical card at check-in.",
        ]
        if pay.prepaid is True:
            lines.append("The room charge has already been prepaid.")
        elif pay.prepaid is False:
            lines.append(
                "As the cardholder will not be present, please let me know how you would like "
                "the room charge to be settled."
            )
        lines += [
            "If a credit card authorization form or any other document is required, "
            "please let me know and I will send it before arrival.",
        ]
    else:
        lines.append("Please register the guest name(s) above for this reservation.")
    lines += [
        "",
        "Could you kindly confirm receipt of this message?",
        "",
        "Best regards,",
        b.booker.name,
    ]
    lines += [x for x in (b.booker.company, b.booker.email, b.booker.phone) if x]
    return subject, "\n".join(lines)


def render_ko(b: HotelBooking) -> tuple[str, str]:
    bk, pay = b.booking, b.payment
    subject = f"[투숙객 안내] 예약번호 {bk.reference} ({bk.check_in.month}/{bk.check_in.day} 체크인)"
    via = f"({bk.platform})" if bk.platform else ""
    rooms = "\n".join(
        f"  {r}번 객실: {', '.join(g.passport_name for g in gs)}" for r, gs in b.guests_by_room().items()
    )
    lines = [
        f"{b.hotel.name} 예약 담당자님께,",
        "",
        f"아래 예약{via}의 투숙객 안내를 드립니다.",
        "",
        f"  예약번호 : {bk.reference}",
        f"  체크인   : {_date_ko(bk.check_in)}",
        f"  체크아웃 : {_date_ko(bk.check_out)} ({b.nights}박)",
        f"  객실 수  : {bk.rooms}실",
        "",
        "투숙객 (여권 영문 표기):",
        rooms,
        "",
    ]
    if not pay.card_holder_stays:
        payer = b.booker.company or b.booker.name
        card = f"(카드 끝자리 {pay.card_last4})" if pay.card_last4 else ""
        lines += [
            f"본 예약은 {payer}에서 업무 출장용으로 예약했으며, "
            f"결제 카드 소지자 {pay.card_holder}{card}님은 투숙하지 않습니다.",
            "위 투숙객으로 체크인할 수 있도록 등록해 주시고, 체크인 시 카드 소지자 본인이나 "
            "실물 카드를 요구하지 않도록 부탁드립니다.",
        ]
        if pay.prepaid is True:
            lines.append("객실 요금은 이미 선결제되었습니다.")
        elif pay.prepaid is False:
            lines.append("카드 소지자가 현장에 없으므로 객실 요금 결제 방법을 알려주시기 바랍니다.")
        lines.append("카드 승인 동의서(authorization form) 등 필요한 서류가 있으면 알려주세요. 도착 전에 보내드리겠습니다.")
    else:
        lines.append("위 투숙객 이름으로 예약에 등록해 주시기 바랍니다.")
    lines += ["", "이 메일을 받으셨는지 회신 부탁드립니다.", "", "감사합니다.", b.booker.name]
    lines += [x for x in (b.booker.company, b.booker.email, b.booker.phone) if x]
    return subject, "\n".join(lines)


def save_draft(b: HotelBooking, lang: str, out_dir: Path = EMAILS_DIR, now: datetime | None = None) -> tuple[Path, str]:
    subject, body = (render_ko if lang == "ko" else render_en)(b)
    to = b.hotel.email or "(호텔 이메일 주소를 확인해 입력하세요)"
    text = f"To: {to}\nSubject: {subject}\n\n{body}\n"
    now = now or datetime.now(timezone.utc).astimezone()
    safe_ref = re.sub(r"[^A-Za-z0-9_-]", "_", b.booking.reference)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"guest_notice_{safe_ref}_{lang}_{now:%Y%m%d-%H%M%S}.txt"
    path.write_text(text, encoding="utf-8")
    return path, text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="호텔 투숙객 안내 이메일 초안 생성 (발송하지 않음)")
    p.add_argument("--booking", type=Path, default=DEFAULT_BOOKING, help="호텔 예약 정보 JSON")
    p.add_argument("--lang", choices=["en", "ko"], default="en", help="이메일 언어 (기본 en)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, out_dir: Path = EMAILS_DIR) -> int:
    args = parse_args(argv)
    try:
        booking = load_booking(args.booking)
    except GuestEmailError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 2
    path, text = save_draft(booking, args.lang, out_dir)
    print(text)
    print("-" * 44)
    print(f"초안을 저장했습니다: {path}")
    if not booking.hotel.email:
        print("[주의] 호텔 이메일 주소가 없습니다. 예약 확인 메일이나 예약 사이트 메시지 기능에서 확인하세요.")
    print("※ 이메일은 보내지 않았습니다. 내용을 확인한 뒤 직접 보내세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
