"""조회 결과 CSV 저장."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR
from .models import FlightOffer, SearchQuery

CSV_COLUMNS = [
    "searched_at",
    "origin",
    "destination",
    "departure_date",
    "adults",
    "rank",
    "airline",
    "flight_numbers",
    "departing_at",
    "arriving_at",
    "stops",
    "total_amount",
    "total_currency",
    "offer_id",
]


def save_flights_csv(
    query: SearchQuery,
    offers: list[FlightOffer],
    searched_at: datetime,
    data_dir: Path = DATA_DIR,
) -> Path:
    """정렬된 결과를 data/flights_<출발>-<도착>_<날짜>_<조회시각>.csv 로 저장한다."""
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = searched_at.strftime("%Y%m%d-%H%M%S")
    path = data_dir / (
        f"flights_{query.origin}-{query.destination}_{query.departure_date.isoformat()}_{stamp}.csv"
    )
    # utf-8-sig: 엑셀에서 한글이 깨지지 않게 BOM 포함
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for rank, o in enumerate(offers, start=1):
            writer.writerow(
                {
                    "searched_at": searched_at.isoformat(timespec="seconds"),
                    "origin": query.origin,
                    "destination": query.destination,
                    "departure_date": query.departure_date.isoformat(),
                    "adults": query.adults,
                    "rank": rank,
                    "airline": o.airline,
                    "flight_numbers": o.flight_numbers,
                    "departing_at": o.departing_at.isoformat(timespec="minutes"),
                    "arriving_at": o.arriving_at.isoformat(timespec="minutes"),
                    "stops": o.stops,
                    "total_amount": f"{o.total_amount:.2f}",
                    "total_currency": o.total_currency,
                    "offer_id": o.offer_id,
                }
            )
    return path
