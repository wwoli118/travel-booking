"""환경변수 로드. 테스트(샌드박스) 키만 허용한다 (CLAUDE.md 원칙 3·4)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DUFFEL_TEST_KEY_PREFIX = "duffel_test_"


class ConfigError(Exception):
    """설정 오류. 원인을 사람이 읽을 수 있는 문장으로 담는다."""


def validate_duffel_key(key: str | None) -> str:
    """키가 있고 테스트 키인지 확인한다. 아니면 ConfigError."""
    if not key or not key.strip():
        raise ConfigError(
            "DUFFEL_API_KEY가 비어 있습니다. .env.example을 .env로 복사하고 "
            "Duffel 테스트 키(duffel_test_...)를 넣어주세요."
        )
    key = key.strip()
    if not key.startswith(DUFFEL_TEST_KEY_PREFIX):
        raise ConfigError(
            "테스트 키가 아닙니다. 이 프로젝트는 duffel_test_ 로 시작하는 샌드박스 키로만 "
            "동작합니다. 운영(live) 키 사용은 사람의 명시적 승인이 필요합니다 (CLAUDE.md 원칙 4)."
        )
    return key


def load_duffel_key(env_file: Path | None = None) -> str:
    """프로젝트 루트의 .env에서 DUFFEL_API_KEY를 읽어 검증 후 반환한다."""
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    return validate_duffel_key(os.getenv("DUFFEL_API_KEY"))
