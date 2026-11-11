import httpx
import pytest

from src.duffel import BlockedEndpointError, duffel_request


def _client():
    def handler(req):
        raise AssertionError(f"차단되어야 할 요청이 전송됨: {req.method} {req.url.path}")
    return httpx.Client(base_url="https://api.duffel.com", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("path", ["/air/payments", "/payments/payment_intents", "/AIR/PAYMENTS"])
def test_payment_paths_blocked(path):
    with pytest.raises(BlockedEndpointError):
        duffel_request("POST", path, "duffel_test_x", client=_client(), json={"data": {}})


@pytest.mark.parametrize("data", [
    {"type": "instant", "selected_offers": ["off_x"]},
    {"type": "hold", "payments": [{"type": "balance", "amount": "1", "currency": "USD"}]},
])
def test_payment_bodies_blocked(data):
    with pytest.raises(BlockedEndpointError):
        duffel_request("POST", "/air/orders", "duffel_test_x", client=_client(), json={"data": data})
