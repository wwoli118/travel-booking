import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def duffel_response() -> dict:
    return json.loads((FIXTURES / "duffel_offers_sample.json").read_text(encoding="utf-8"))


@pytest.fixture
def raw_offers(duffel_response) -> list[dict]:
    return duffel_response["data"]["offers"]


@pytest.fixture
def single_offer() -> dict:
    return json.loads((FIXTURES / "duffel_offer_single.json").read_text(encoding="utf-8"))["data"]


@pytest.fixture
def travelers_raw() -> dict:
    return json.loads((Path(__file__).parent.parent / "travelers.example.json").read_text(encoding="utf-8"))
