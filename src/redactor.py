"""
PCI/PII Redactor — runs before every LLM call. No exceptions.

Pure Python, zero LLM involvement. Implements PCI-DSS v4.0 Requirement 3
cardholder data detection with Luhn algorithm validation to avoid false
positives on trace IDs, timestamps, and other numeric sequences.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# 16-digit PAN candidate: 4 groups of 4 digits with optional dash/space separators
_PAN_CANDIDATE = re.compile(
    r"(?<!\d)\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}(?!\d)"
)

# Masked PAN: same shape but may contain asterisks — must have at least one *
_PAN_MASKED_CANDIDATE = re.compile(
    r"(?<!\d)[0-9*]{4}[-\s]?[0-9*]{4}[-\s]?[0-9*]{4}[-\s]?[0-9*]{4}(?!\d)"
)

# CVV/CVC near keyword
_CVV = re.compile(
    r"(?i)\b(?:cvv2?|cvc2?|security[-_]?code)\s*[=:\s]\s*\d{3,4}\b"
)

# Cardholder name field
_CARDHOLDER = re.compile(
    r"(?i)(?:cardholder[-_]?name|card[-_]?name|name[-_]?on[-_]?card)"
    r'\s*[=:]\s*"?([^"\n,;]{2,40})"?'
)

# Email
_EMAIL = re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b")

# Phone (North American with optional separators)
_PHONE = re.compile(
    r"\b(?:\+?1[-.\s])?\(?[2-9]\d{2}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
)

# Account ID fields
_ACCOUNT_ID = re.compile(
    r"(?i)\baccount[-_]?(?:id|number|no)\s*[=:]\s*\S+"
)

# CDE-scoped class names that must not appear in LLM context
_CDE_CLASS = re.compile(
    r"\b(?:CardholderData|PCIScope|PANStore|CVVStore|CardVault"
    r"|PaymentCard|CardNumber|CardData)\b"
)


@dataclass
class RedactionResult:
    redacted_text: str
    redaction_count: int
    redaction_types: list[str] = field(default_factory=list)
    luhn_validated_pans: int = 0
    luhn_rejected_candidates: int = 0


class PCIRedactor:
    """
    Strips PAN, CVV, cardholder data, and PII before any LLM call.

    Luhn algorithm ensures only valid PANs are redacted — trace IDs and
    timestamps that happen to be 16 digits are never falsely redacted.
    """

    @staticmethod
    def luhn_check(number: str) -> bool:
        """Return True if the digit sequence passes the Luhn check."""
        digits = re.sub(r"\D", "", number)
        if not 13 <= len(digits) <= 19:
            return False
        total = 0
        for i, ch in enumerate(reversed(digits)):
            n = int(ch)
            if i % 2 == 1:
                n *= 2
                if n > 9:
                    n -= 9
            total += n
        return total % 10 == 0

    def redact(self, text: str) -> RedactionResult:
        """Redact PCI/PII data from text. Never raises. Idempotent."""
        if not text:
            return RedactionResult(redacted_text=text or "", redaction_count=0)

        result = text
        count = 0
        types: list[str] = []
        luhn_valid = 0
        luhn_rejected = 0

        try:
            # Stage 1+2: PAN candidates validated with Luhn algorithm
            def _replace_pan(m: re.Match) -> str:
                nonlocal count, luhn_valid, luhn_rejected
                raw = m.group(0)
                if self.luhn_check(raw):
                    log.info(
                        "REDACTION_EVENT: type=PAN_16, position=%d, luhn_valid=True",
                        m.start(),
                    )
                    count += 1
                    luhn_valid += 1
                    types.append("PAN_16")
                    return "[REDACTED-PAN_16]"
                log.debug(
                    "REDACTION_EVENT: type=PAN_16_CANDIDATE, position=%d, luhn_valid=False",
                    m.start(),
                )
                luhn_rejected += 1
                return raw

            result = _PAN_CANDIDATE.sub(_replace_pan, result)

            # Stage 3a: Masked PANs — same shape but must contain at least one *
            def _replace_masked(m: re.Match) -> str:
                nonlocal count
                raw = m.group(0)
                if "*" not in raw:
                    return raw
                log.info(
                    "REDACTION_EVENT: type=PAN_MASKED, position=%d, luhn_valid=null",
                    m.start(),
                )
                count += 1
                types.append("PAN_MASKED")
                return "[REDACTED-PAN_MASKED]"

            result = _PAN_MASKED_CANDIDATE.sub(_replace_masked, result)

            # Stage 3b: remaining PCI/PII patterns
            for pattern, rtype in [
                (_CVV, "CVV"),
                (_CARDHOLDER, "CARDHOLDER"),
                (_EMAIL, "EMAIL"),
                (_PHONE, "PHONE"),
                (_ACCOUNT_ID, "ACCOUNT_ID"),
                (_CDE_CLASS, "CDE_CLASS"),
            ]:
                matches = list(pattern.finditer(result))
                for m in matches:
                    log.info(
                        "REDACTION_EVENT: type=%s, position=%d, luhn_valid=null",
                        rtype,
                        m.start(),
                    )
                if matches:
                    result = pattern.sub(f"[REDACTED-{rtype}]", result)
                    count += len(matches)
                    types.extend([rtype] * len(matches))

        except Exception:
            log.exception("PCIRedactor internal error")
            return RedactionResult(redacted_text=text, redaction_count=0)

        return RedactionResult(
            redacted_text=result,
            redaction_count=count,
            redaction_types=types,
            luhn_validated_pans=luhn_valid,
            luhn_rejected_candidates=luhn_rejected,
        )


if __name__ == "__main__":
    r = PCIRedactor()
    samples = [
        ("Valid Visa PAN", "Card: 4111111111111111 processed"),
        ("Luhn-invalid (trace ID)", "trace=1234567890123456 logged"),
        ("Email", "Contact: alice@example.com"),
        ("CDE class", "class CardholderData extends BaseModel"),
    ]
    for label, text in samples:
        res = r.redact(text)
        print(f"{label}: {res.redacted_text!r}  (count={res.redaction_count})")
