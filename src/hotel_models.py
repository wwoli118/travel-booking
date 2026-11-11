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
