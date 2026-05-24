"""
Prompt-Injection Sanitizer — runs on every diff body before LLM ingestion.

Detects and neutralises common prompt-injection techniques embedded in commit
messages, PR descriptions, and diff hunks. Pure Python — zero LLM involvement.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SanitizationResult:
    sanitized_text: str
    injection_attempts: int
    patterns_detected: list[str] = field(default_factory=list)


_REPLACEMENT = "[SANITIZED-INJECTION-ATTEMPT]"

# Each entry: (pattern_name, compiled_regex)
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # System-role markers
    ("SYSTEM_ROLE", re.compile(r"(?i)\bSYSTEM\s*:", re.MULTILINE)),
    ("YOU_ARE", re.compile(r"(?i)\bYou\s+are\s+a\b")),
    ("NEW_INSTRUCTIONS", re.compile(r"(?i)New\s+instructions\s*:", re.MULTILINE)),
    ("IGNORE_PREVIOUS", re.compile(r"(?i)IGNORE\s+PREVIOUS")),
    # Role-switching tokens
    ("INST_CLOSE", re.compile(r"\[/INST\]")),
    ("IM_START", re.compile(r"<\|im_start\|>")),
    ("SYS_TAG", re.compile(r"<\|system\|>")),
    ("ASST_TAG", re.compile(r"<\|assistant\|>")),
    # Override attempts
    ("FORGET_RULES", re.compile(r"(?i)forget\s+all\s+rules")),
    ("DEVELOPER_MODE", re.compile(r"(?i)developer\s+mode")),
    ("PRETEND_TO_BE", re.compile(r"(?i)pretend\s+to\s+be")),
    ("IGNORE_ABOVE", re.compile(r"(?i)ignore\s+the\s+above")),
    ("DISREGARD_INSTRUCTIONS", re.compile(r"(?i)disregard\s+your\s+instructions")),
    # Unicode tricks: zero-width + RTL marks
    ("ZERO_WIDTH", re.compile(r"[​‌‍﻿]")),
    ("RTL_MARK", re.compile(r"[‎‏‪-‮]")),
]


class PromptInjectionSanitizer:
    """
    Detects and strips prompt-injection attempts from untrusted text.

    All patterns are applied sequentially. The method is idempotent (already-
    sanitized markers are not re-detected). Never raises.
    """

    def sanitize(self, text: str) -> SanitizationResult:
        if not text:
            return SanitizationResult(sanitized_text=text or "", injection_attempts=0)

        result = text
        total = 0
        detected: list[str] = []

        try:
            for name, pattern in _PATTERNS:
                matches = list(pattern.finditer(result))
                if matches:
                    for m in matches:
                        log.info(
                            "SANITIZATION_EVENT: pattern=%s, position=%d",
                            name,
                            m.start(),
                        )
                    result = pattern.sub(_REPLACEMENT, result)
                    count = len(matches)
                    total += count
                    detected.extend([name] * count)
        except Exception:
            log.exception("PromptInjectionSanitizer internal error")
            return SanitizationResult(sanitized_text=text, injection_attempts=0)

        return SanitizationResult(
            sanitized_text=result,
            injection_attempts=total,
            patterns_detected=detected,
        )
