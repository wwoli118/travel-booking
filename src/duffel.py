"""Duffel API 공용 호출부. 조회(GET/검색)만 하고 주문·결제 엔드포인트는 두지 않는다."""

from __future__ import annotations

import httpx

from .config import validate_duffel_key

DUFFEL_BASE_URL = "https://api.duffel.com"
DUFFEL_VERSION = "v2"
TIMEOUT_SECONDS = 60.0  # 항공사 응답을 기다리므로 넉넉히


class DuffelError(Exception):
    """Duffel API 호출 실패. 원인을 담는다."""


def describe_error(resp: httpx.Response) -> str:
    try:
        errors = resp.json().get("errors", [])
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    parts = [
        f"{e.get('title', '')}: {e.get('message', '')} (code={e.get('code', '?')})" for e in errors
    ]
    return f"HTTP {resp.status_code} - " + ("; ".join(parts) or resp.text[:300])


def duffel_request(
    method: str,
    path: str,
    api_key: str,
    client: httpx.Client | None = None,
    **kwargs,
) -> dict:
    """Duffel에 요청하고 JSON 본문을 돌려준다. 실패 시 원인을 담은 DuffelError."""
    api_key = validate_duffel_key(api_key)  # 호출 직전에도 테스트 키인지 재확인
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Duffel-Version": DUFFEL_VERSION,
        "Accept": "application/json",
    }
    if "json" in kwargs:
        headers["Content-Type"] = "application/json"

    own_client = client is None
    client = client or httpx.Client(base_url=DUFFEL_BASE_URL, timeout=TIMEOUT_SECONDS)
    try:
        resp = client.request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as e:
        raise DuffelError(f"Duffel API에 연결하지 못했습니다: {type(e).__name__}: {e}") from e
    finally:
        if own_client:
            client.close()

    if resp.status_code >= 400:
        raise DuffelError(describe_error(resp))
    try:
        return resp.json()
    except ValueError as e:
        raise DuffelError(f"Duffel 응답이 JSON이 아닙니다: {resp.text[:300]}") from e
