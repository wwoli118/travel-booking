"""호텔 검색 조건과 결과 모델 (공급자 공통)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

LOCATIONS_FILE = Path(__file__).parent / "locations.json"
MAX_NIGHTS = 99  # Duffel Stays 제한
MAX_DAYS_AHEAD = 330  # Duffel Stays: 체크인은 330일 이내
MAX_ROOMS = 9
MAX_ADULTS = 9


class LocationError(ValueError):
    """등록되지 않은 지명 등 위치를 정할 수 없을 때."""


class Location(BaseModel):
    key: str
    label: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_km: int = Field(default=3, ge=1, le=20)


def load_locations(path: Path = LOCATIONS_FILE) -> dict[str, Location]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: Location(key=k, **v) for k, v in raw.items() if not k.startswith("_")}


def resolve_location(
    name: str | None,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: int | None = None,
    path: Path = LOCATIONS_FILE,
) -> Location:
    """등록 지명 또는 좌표로 위치를 정한다. 반경을 주면 그 값을 쓴다."""
    if name and (lat is not None or lng is not None):
        raise LocationError("--location과 --lat/--lng는 함께 쓸 수 없습니다.")
    if name:
        locations = load_locations(path)
        if name not in locations:
            raise LocationError(
                f"등록되지 않은 지명입니다: {name!r}. 등록된 지명: {', '.join(sorted(locations))} "
                "(src/locations.json에 추가하거나 --lat/--lng를 쓰세요)"
            )
        loc = locations[name]
    elif lat is not None and lng is not None:
        loc = Location(key="custom", label=f"좌표 {lat}, {lng}", latitude=lat, longitude=lng)
    else:
        raise LocationError("--location 또는 --lat와 --lng를 함께 지정하세요.")
    if radius_km is not None:
        if not 1 <= radius_km <= 20:
            raise LocationError(f"검색 반경은 1~20km로 지정하세요: {radius_km}")
        loc = loc.model_copy(update={"radius_km": radius_km})
    return loc


class HotelSearchQuery(BaseModel):
    location: Location
    check_in: date
    check_out: date
    rooms: int = Field(default=1, ge=1, le=MAX_ROOMS)
    adults: int = Field(default=1, ge=1, le=MAX_ADULTS)
    free_cancellation_only: bool = False

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @field_validator("check_in")
    @classmethod
    def _check_in(cls, v: date) -> date:
        today = date.today()
        if v < today:
            raise ValueError(f"체크인 날짜가 이미 지났습니다: {v.isoformat()}")
        if v > today + timedelta(days=MAX_DAYS_AHEAD):
            raise ValueError(f"체크인은 오늘부터 {MAX_DAYS_AHEAD}일 이내여야 합니다: {v.isoformat()}")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "HotelSearchQuery":
        if self.check_out <= self.check_in:
            raise ValueError(
                f"체크아웃({self.check_out.isoformat()})은 체크인({self.check_in.isoformat()}) 다음 날 이후여야 합니다."
            )
        if self.nights > MAX_NIGHTS:
            raise ValueError(f"최대 {MAX_NIGHTS}박까지 검색할 수 있습니다 (요청 {self.nights}박).")
        if self.adults < self.rooms:
            raise ValueError(f"객실 수({self.rooms})가 인원({self.adults}명)보다 많습니다. 객실마다 1명 이상이어야 합니다.")
        return self


def _money(v) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


class HotelOffer(BaseModel):
    """숙소별 최저 요금 한 건. 금액은 항상 통화와 함께."""

    provider: str
    search_result_id: str
    accommodation_id: str
    name: str
    rating: int | None = None  # 성급 1–5
    review_score: float | None = None  # 1.0–10.0
    city: str | None = None
    address: str | None = None
    nights: int = Field(ge=1)
    total_amount: Decimal
    total_currency: str = Field(min_length=3, max_length=3)
    due_at_accommodation_amount: Decimal | None = None  # 현장에서 따로 내는 요금 (총액에 미포함)
    due_at_accommodation_currency: str | None = None
    expires_at: datetime | None = None

    @property
    def nightly_average(self) -> Decimal:
        return (self.total_amount / self.nights).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def price_label(self) -> str:
        return f"{self.total_amount:,.2f} {self.total_currency}"

    @property
    def nightly_label(self) -> str:
        return f"{self.nightly_average:,.2f} {self.total_currency}"

    @property
    def due_label(self) -> str:
        if not self.due_at_accommodation_amount:
            return "-"
        return f"{self.due_at_accommodation_amount:,.2f} {self.due_at_accommodation_currency or '(통화 정보 없음)'}"


# ---------------------------------------------------------------- 요금·견적 (예약 전 확인용)

BOARD_LABELS = {
    "room_only": "객실만",
    "breakfast": "조식 포함",
    "half_board": "조식·석식 포함",
    "full_board": "3식 포함",
    "all_inclusive": "올인클루시브",
}
PAYMENT_LABELS = {
    "pay_now": "선결제 (예약 시 전액)",
    "deposit": "보증금 선결제, 잔액은 숙소에서",
    "guarantee": "숙소에서 결제 (카드 보증)",
}


def fmt_with_offset(dt: datetime) -> str:
    """시각을 받은 시간대 그대로 표시한다 (예: 2026-12-18 23:59 (UTC+08:00)). 시간대가 없으면 그대로."""
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M")
    off = dt.strftime("%z")
    return f"{dt:%Y-%m-%d %H:%M} (UTC{off[:3]}:{off[3:]})"


class CancellationStep(BaseModel):
    """이 시각 전에 취소하면 refund_amount를 돌려받는다."""

    before: datetime
    refund_amount: Decimal
    currency: str


class HotelRate(BaseModel):
    rate_id: str
    room_name: str
    board_type: str | None = None
    payment_type: str | None = None
    total_amount: Decimal
    total_currency: str
    due_at_accommodation_amount: Decimal | None = None
    due_at_accommodation_currency: str | None = None
    cancellation: list[CancellationStep] = []
    conditions: list[tuple[str, str]] = []  # (제목, 내용)
    available_payment_methods: list[str] = []
    loyalty_programme_required: bool = False
    expires_at: datetime | None = None

    @property
    def board_label(self) -> str:
        return BOARD_LABELS.get(self.board_type or "", self.board_type or "정보 없음")

    @property
    def payment_label(self) -> str:
        return PAYMENT_LABELS.get(self.payment_type or "", self.payment_type or "정보 없음")

    @property
    def price_label(self) -> str:
        return f"{self.total_amount:,.2f} {self.total_currency}"

    def free_cancellation_until(self, now: datetime) -> datetime | None:
        """전액 환불되는 마지막 시각 (지금 이후일 때만)."""
        full = [s for s in self.cancellation if s.refund_amount >= self.total_amount and s.before > now]
        return max((s.before for s in full), default=None)

    def cancellation_lines(self, now: datetime) -> list[str]:
        """사람이 읽을 취소 규정. 정보가 없으면 환불 불가로 본다 (Duffel: 타임라인이 비면 환불 없음)."""
        steps = sorted((s for s in self.cancellation if s.before > now), key=lambda s: s.before)
        if not steps:
            return ["환불 불가 (지금 취소하면 환불 없음)"]
        lines = []
        for s in steps:
            when = fmt_with_offset(s.before)
            if s.refund_amount >= self.total_amount:
                lines.append(f"{when} 전까지 취소 시 전액 환불 (무료 취소)")
            elif s.refund_amount == 0:
                lines.append(f"{when} 전까지 취소해도 환불 없음")
            else:
                lines.append(f"{when} 전까지 취소 시 {s.refund_amount:,.2f} {s.currency} 환불")
        lines.append(f"{fmt_with_offset(steps[-1].before)} 이후 취소 시 환불 없음")
        return lines


class HotelProperty(BaseModel):
    accommodation_id: str
    name: str
    rating: int | None = None
    review_score: float | None = None
    address: str | None = None
    email: str | None = None
    phone: str | None = None
    check_in_after: str | None = None
    check_out_before: str | None = None


class HotelQuote(BaseModel):
    """견적: 선택한 요금의 가용성·총액 재확인 결과. 예약·결제가 아니다."""

    quote_id: str
    property: HotelProperty
    check_in: date
    check_out: date
    rooms: int
    total_amount: Decimal
    total_currency: str
    due_at_accommodation_amount: Decimal | None = None
    due_at_accommodation_currency: str | None = None
    deposit_amount: Decimal | None = None
    deposit_currency: str | None = None
    rate: HotelRate | None = None  # 견적에 실린 요금 상세 (없으면 선택 시 요금 정보 사용)

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def nightly_average(self) -> Decimal:
        return (self.total_amount / self.nights).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
