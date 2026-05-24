"""15 unit tests for PCIRedactor — Luhn validation, false-positive guards,
performance SLO, and idempotency."""

import time

import pytest

from src.redactor import PCIRedactor, RedactionResult


@pytest.fixture
def redactor():
    return PCIRedactor()


# ── Luhn algorithm ───────────────────────────────────────────────────────────

def test_luhn_check_valid():
    """4111 1111 1111 1111 is the canonical Luhn-valid Visa test PAN."""
    assert PCIRedactor.luhn_check("4111111111111111") is True


def test_luhn_check_invalid():
    """Luhn-invalid 16-digit number returns False."""
    assert PCIRedactor.luhn_check("1234567890123456") is False


# ── PAN_16 detection ─────────────────────────────────────────────────────────

def test_valid_pan_is_redacted(redactor):
    """Luhn-valid PAN is replaced with [REDACTED-PAN_16]."""
    res = redactor.redact("Card: 4111111111111111 processed")
    assert "[REDACTED-PAN_16]" in res.redacted_text
    assert "4111111111111111" not in res.redacted_text
    assert res.redaction_count == 1
    assert res.luhn_validated_pans == 1


def test_luhn_invalid_not_redacted(redactor):
    """Luhn-invalid 16-digit number must NOT be redacted (false-positive guard)."""
    res = redactor.redact("trace=1234567890123456 logged")
    assert "1234567890123456" in res.redacted_text
    assert "[REDACTED-PAN_16]" not in res.redacted_text
    assert res.luhn_rejected_candidates == 1
    assert res.redaction_count == 0


def test_pan_with_dashes_redacted(redactor):
    """PAN formatted as 4111-1111-1111-1111 is detected and redacted."""
    res = redactor.redact("4111-1111-1111-1111")
    assert "[REDACTED-PAN_16]" in res.redacted_text
    assert res.luhn_validated_pans == 1


def test_pan_with_spaces_redacted(redactor):
    """PAN formatted as 4111 1111 1111 1111 is detected and redacted."""
    res = redactor.redact("4111 1111 1111 1111")
    assert "[REDACTED-PAN_16]" in res.redacted_text
    assert res.luhn_validated_pans == 1


# ── PAN_MASKED ───────────────────────────────────────────────────────────────

def test_masked_pan_redacted(redactor):
    """Masked PAN with asterisks is replaced with [REDACTED-PAN_MASKED]."""
    res = redactor.redact("Stored token: 4111-****-****-1234")
    assert "[REDACTED-PAN_MASKED]" in res.redacted_text
    assert "****" not in res.redacted_text


# ── CVV / Email ──────────────────────────────────────────────────────────────

def test_cvv_redacted(redactor):
    """CVV value near keyword is replaced with [REDACTED-CVV]."""
    res = redactor.redact("cvv=123 submitted")
    assert "[REDACTED-CVV]" in res.redacted_text
    assert "cvv=123" not in res.redacted_text


def test_email_redacted(redactor):
    """Email addresses are replaced with [REDACTED-EMAIL]."""
    res = redactor.redact("Contact alice@example.com for billing")
    assert "[REDACTED-EMAIL]" in res.redacted_text
    assert "alice@example.com" not in res.redacted_text


# ── CDE class names ──────────────────────────────────────────────────────────

def test_cde_class_redacted(redactor):
    """CDE class names are replaced with [REDACTED-CDE_CLASS]."""
    res = redactor.redact("class CardholderData extends BaseModel: pass")
    assert "[REDACTED-CDE_CLASS]" in res.redacted_text
    assert "CardholderData" not in res.redacted_text


# ── RedactionResult ──────────────────────────────────────────────────────────

def test_redaction_result_has_all_fields(redactor):
    """RedactionResult exposes all required fields with correct types."""
    res = redactor.redact("4111111111111111")
    assert isinstance(res, RedactionResult)
    assert isinstance(res.redacted_text, str)
    assert isinstance(res.redaction_count, int)
    assert isinstance(res.redaction_types, list)
    assert isinstance(res.luhn_validated_pans, int)
    assert isinstance(res.luhn_rejected_candidates, int)


def test_multiple_items_redaction_count(redactor):
    """redaction_count reflects total redactions across all pattern types."""
    res = redactor.redact("Card 4111111111111111, email user@example.com")
    assert res.redaction_count >= 2
    assert "PAN_16" in res.redaction_types
    assert "EMAIL" in res.redaction_types


# ── Safety properties ────────────────────────────────────────────────────────

def test_clean_text_passes_unchanged(redactor):
    """Text without sensitive data is returned unchanged with count zero."""
    text = "Deploy v2.3.1 to prod cluster, region us-east-1"
    res = redactor.redact(text)
    assert res.redacted_text == text
    assert res.redaction_count == 0


def test_idempotent(redactor):
    """Redacting already-redacted text produces identical output."""
    text = "Card: 4111111111111111, user@example.com"
    res1 = redactor.redact(text)
    res2 = redactor.redact(res1.redacted_text)
    assert res1.redacted_text == res2.redacted_text


def test_performance_slo(redactor):
    """Redaction of 15k characters completes in under 100ms."""
    chunk = "Processing card 4111111111111111 for user@example.com. "
    text = chunk * 280  # ~15 400 chars
    start = time.perf_counter()
    redactor.redact(text)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms < 100, f"SLO breach: {elapsed_ms:.1f}ms > 100ms"
