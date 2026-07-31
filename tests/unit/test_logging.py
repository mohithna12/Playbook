"""Tests for structured logging and PII redaction."""

from app.core.logging import _redact_pii


def test_redact_pii_strips_sensitive_keys() -> None:
    event: dict[str, object] = {
        "event": "user_login",
        "email": "test@example.com",
        "token": "abc123secret",
        "user_id": "u-123",
    }
    result = _redact_pii(None, "", event)
    assert result["email"] == "[REDACTED]"
    assert result["token"] == "[REDACTED]"
    assert result["user_id"] == "u-123"
    assert result["event"] == "user_login"


def test_redact_pii_case_insensitive() -> None:
    event: dict[str, object] = {"Email": "x@y.com", "PASSWORD": "secret"}
    result = _redact_pii(None, "", event)
    # Keys are compared lowercase
    assert result["Email"] == "[REDACTED]"
    assert result["PASSWORD"] == "[REDACTED]"


def test_redact_pii_leaves_safe_keys() -> None:
    event: dict[str, object] = {"status": "ok", "duration_ms": 42}
    result = _redact_pii(None, "", event)
    assert result == {"status": "ok", "duration_ms": 42}
