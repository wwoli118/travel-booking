"""검색 조건과 항공편 결과 모델 (pydantic)."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

IATA_RE = re.compile(r"^[A-Z]{3}$")
MAX_PASSENGERS = 9  # Duffel 한 요청당 최대 승객 수


class SearchQuery(BaseModel):
    """편도 항공권 검색 조건."""

    origin: str
    destination: str
    departure_date: date
    adults: int = Field(ge=1, le=MAX_PASSENGERS)
    cabin_class: str = "economy"

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def _iata(cls, v: str) -> str:
        v = str(v).strip().upper()
        if not IATA_RE.match(v):
            raise ValueError(f"공항 코드는 영문 3글자 IATA 코드여야 합니다: {v!r}")
        return v

    @field_validator("departure_date")
    @classmethod
    def _not_past(cls, v: date) -> date:
        if v < date.today():
            raise ValueError(f"출발일이 이미 지났습니다: {v.isoformat()}")
        return v

    @field_validator("cabin_class")
    @classmethod
    def _cabin(cls, v: str) -> str:
        allowed = {"economy", "premium_economy", "business", "first"}
        if v not in allowed:
            raise ValueError(f"좌석 등급은 {sorted(allowed)} 중 하나여야 합니다: {v!r}")
        return v

    def model_post_init(self, __context) -> None:
        if self.origin == self.destination:
            raise ValueError("출발지와 도착지가 같습니다.")


class FlightOffer(BaseModel):
    """가격순 비교에 쓰는 항공편 한 건. 금액은 항상 통화와 함께 다룬다."""

    offer_id: str
    airline: str
    flight_numbers: str  # 예: "PR467" 또는 경유 시 "KE623 / KE631"
    origin: str
    destination: str
    departing_at: datetime  # 출발 공항 현지 시각
    arriving_at: datetime  # 도착 공항 현지 시각
    stops: int = Field(ge=0)
    total_amount: Decimal
    total_currency: str = Field(min_length=3, max_length=3)

    @property
    def stops_label(self) -> str:
        return "직항" if self.stops == 0 else f"경유 {self.stops}회"

    @property
    def price_label(self) -> str:
        return f"{self.total_amount:,.2f} {self.total_currency}"

    @classmethod
    def from_duffel(cls, offer: dict) -> "FlightOffer":
        """Duffel offer 객체(편도, slice 1개)를 변환한다. 필드가 없으면 KeyError/ValueError."""
        slices = offer["slices"]
        if len(slices) != 1:
            raise ValueError(f"편도 offer는 slice가 1개여야 합니다 (받은 수: {len(slices)})")
        segments = slices[0]["segments"]
        if not segments:
            raise ValueError("segment가 없는 offer입니다.")
        first, last = segments[0], segments[-1]

        flight_numbers = " / ".join(
            f"{s['marketing_carrier']['iata_code']}{s['marketing_carrier_flight_number']}"
            for s in segments
        )
        return cls(
            offer_id=offer["id"],
            airline=offer["owner"]["name"],
            flight_numbers=flight_numbers,
            origin=first["origin"]["iata_code"],
            destination=last["destination"]["iata_code"],
            departing_at=first["departing_at"],
            arriving_at=last["arriving_at"],
            stops=len(segments) - 1,
            total_amount=Decimal(offer["total_amount"]),
            total_currency=offer["total_currency"],
        )
