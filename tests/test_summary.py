import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from src import summary
from src.offers import OfferDetails, SelectedOffer
from src.travelers import TravelerFile


@pytest.fixture
def details(single_offer):
    return OfferDetails.from_duffel(single_offer)


@pytest.fixture
def tf(travelers_raw):
    return TravelerFile.model_validate(travelers_raw)


@pytest.fixture(autouse=True)
def test_key(monkeypatch):
    monkeypatch.setenv("DUFFEL_API_KEY", "duffel_test_x")


def sel(amount="289.00", cur="USD"):
    return SelectedOffer("off_cheap_one_stop", Decimal(amount), cur)


@pytest.mark.parametrize("amount,cur,text,warn", [
    ("289.00", "USD", "변동 없음", False),
    ("250.00", "USD", "39.00 USD 인상", True),
    ("300.00", "USD", "11.00 USD 인하", False),
    ("16000", "PHP", "통화가 바뀌어", True),
])
def test_price_change(details, amount, cur, text, warn):
    note, w = summary.price_change_note(details, sel(amount, cur))
    assert text in note and w is warn


def test_no_searched_price(details):
    note, w = summary.price_change_note(details, SelectedOffer("off_x"))
    assert "정보 없음" in note and not w


def test_summary_contains_required_items(details, tf):
    text = summary.build_summary(details, tf, sel())
    for item in ["ZZ2002 / ZZ2003", "경유 1회: TPE", "성인 2석, economy", "HONG GILDONG", "KIM YOUNGHEE",
                 "289.00 USD", "환불: 가능, 수수료 50.00 USD", "변경: 불가", "위탁 1개 / 기내 1개",
                 "운임 유효", "영업1팀", "결제를 하지 않습니다"]:
        assert item in text, item
    assert "주의" not in text


def test_summary_warns_on_price_increase(details, tf):
    assert "주의" in summary.build_summary(details, tf, sel("200.00"))


@pytest.mark.parametrize("answers,expected", [
    (["yes"], True), (["Y"], True), (["no"], False), ([""], False),
    (["네", "yes"], True), (["?", "?", "?"], False),
])
def test_confirmation(answers, expected):
    it = iter(answers)
    assert summary.ask_confirmation(lambda _: next(it)) is expected


def test_save_draft_maps_passenger_ids(details, tf, tmp_path):
    path = summary.save_draft(details, tf, sel(), datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc), tmp_path)
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["status"] == "confirmed_for_preparation"
    assert [p["duffel_passenger_id"] for p in d["passengers"]] == ["pas_001", "pas_002"]
    assert d["passengers"][0]["family_name"] == "HONG"
    assert d["price"] == {"total_amount": "289.00", "total_currency": "USD"}
    assert d["conditions"]["refund_before_departure"]["penalty_amount"] == "50.00"


def _run(tmp_path, single_offer, travelers_raw, answers, capsys):
    tpath = tmp_path / "travelers.json"
    tpath.write_text(json.dumps(travelers_raw, ensure_ascii=False), encoding="utf-8")
    client = httpx.Client(base_url="https://api.duffel.com",
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": single_offer})))
    it = iter(answers)
    drafts = tmp_path / "drafts"
    code = summary.main(["--offer", "off_cheap_one_stop", "--travelers", str(tpath)],
                        input_fn=lambda _: next(it), client=client, drafts_dir=drafts)
    return code, drafts, capsys.readouterr()


def test_main_yes_saves_draft(tmp_path, single_offer, travelers_raw, capsys):
    code, drafts, out = _run(tmp_path, single_offer, travelers_raw, ["yes"], capsys)
    assert code == 0 and len(list(drafts.glob("*.json"))) == 1
    assert "아직 예약되지 않았습니다" in out.out


def test_main_no_saves_nothing(tmp_path, single_offer, travelers_raw, capsys):
    code, drafts, out = _run(tmp_path, single_offer, travelers_raw, ["no"], capsys)
    assert code == 0 and not drafts.exists()


def test_main_expired_stops_without_asking(tmp_path, single_offer, travelers_raw, capsys):
    single_offer["expires_at"] = "2020-01-01T00:00:00Z"
    code, drafts, out = _run(tmp_path, single_offer, travelers_raw, [], capsys)  # 입력 요청 시 StopIteration
    assert code == 1 and not drafts.exists() and "유효 시간이 지났습니다" in out.err


def test_main_traveler_count_mismatch(tmp_path, single_offer, travelers_raw, capsys):
    travelers_raw["travelers"].pop()
    code, drafts, out = _run(tmp_path, single_offer, travelers_raw, [], capsys)
    assert code == 1 and "2석" in out.err and not drafts.exists()
