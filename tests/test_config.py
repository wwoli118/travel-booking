import pytest

from src.config import ConfigError, load_duffel_key, validate_duffel_key


def test_accepts_test_key():
    assert validate_duffel_key("  duffel_test_abc123 ") == "duffel_test_abc123"


@pytest.mark.parametrize("key", ["duffel_live_abc123", "abc", "DUFFEL_TEST_abc"])
def test_rejects_non_test_keys(key):
    with pytest.raises(ConfigError, match="테스트 키가 아닙니다"):
        validate_duffel_key(key)


@pytest.mark.parametrize("key", [None, "", "   "])
def test_rejects_missing_key(key):
    with pytest.raises(ConfigError, match="비어 있습니다"):
        validate_duffel_key(key)


def test_load_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DUFFEL_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("DUFFEL_API_KEY=duffel_test_fromfile\n")
    assert load_duffel_key(env) == "duffel_test_fromfile"
