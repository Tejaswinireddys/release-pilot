"""
OPA Policy Engine client — runs before every tool call. A denial raises
PolicyViolationError and halts execution immediately.

Auto-detects evaluation mode:
  live     — POST to OPA REST API (OPA_URL, 2-second timeout)
  embedded — opa eval subprocess against the same .rego file; used when the
             live server is unreachable. No duplicate logic: the Rego file is
             the single source of truth for all deny rules.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_POLICY_ID = "release_pilot.guardrails"
_REGO_FILE = Path(__file__).parent.parent / "policies" / "release_guardrails.rego"
_REST_PATH = "release_pilot/guardrails"


# ── Public data types ─────────────────────────────────────────────────────────

@dataclass
class PolicyResult:
    allowed: bool
    denial_reasons: list[str]
    policy_id: str
    evaluated_at: str
    evaluation_mode: str          # "live" | "embedded"


class PolicyViolationError(Exception):
    """Raised when OPA returns one or more deny rules.

    __str__ returns formatted JSON so the denial reasons display clearly
    on screen / in logs.
    """

    def __init__(
        self,
        denial_reasons: list[str],
        action: str,
        context: dict,
    ) -> None:
        self.denial_reasons = denial_reasons
        self.action = action
        self.context = context
        super().__init__(str(self))

    def __str__(self) -> str:
        return json.dumps(
            {
                "error": "PolicyViolationError",
                "action": self.action,
                "policy_id": _POLICY_ID,
                "denial_reasons": self.denial_reasons,
            },
            indent=2,
        )


# ── Client ────────────────────────────────────────────────────────────────────

class OPAClient:
    """
    Evaluates ``policies/release_guardrails.rego`` before every tool call.

    Parameters
    ----------
    opa_url:
        Base URL of the live OPA server (default: ``OPA_URL`` env var or
        ``http://localhost:8181``).
    mode:
        ``"auto"`` (default) — try live, fall back to embedded.
        ``"live"``           — require live server; raise if unreachable.
        ``"embedded"``       — always use in-process ``opa eval``.
    """

    def __init__(
        self,
        opa_url: str | None = None,
        mode: str = "auto",
    ) -> None:
        self._opa_url = (
            opa_url or os.getenv("OPA_URL", "http://localhost:8181")
        ).rstrip("/")
        self._mode = mode  # "auto" | "live" | "embedded"

    def check(self, action: str, context: dict) -> PolicyResult:
        """Evaluate the guardrails policy. Raises PolicyViolationError if denied."""
        if self._mode == "embedded":
            result = self._eval_embedded(action, context)
        elif self._mode == "live":
            result = self._eval_live(action, context)
            if result is None:
                raise ConnectionError(
                    f"OPA server unreachable at {self._opa_url} and mode is 'live'"
                )
        else:  # auto
            result = self._eval_live(action, context)
            if result is None:
                log.warning("opa.live_unavailable — falling back to embedded evaluation")
                result = self._eval_embedded(action, context)

        log.info(
            "POLICY_CHECK: action=%s, allowed=%s, reasons=%s, mode=%s",
            action,
            result.allowed,
            result.denial_reasons,
            result.evaluation_mode,
        )

        if not result.allowed:
            raise PolicyViolationError(
                denial_reasons=result.denial_reasons,
                action=action,
                context=context,
            )
        return result

    # ── Evaluation back-ends ──────────────────────────────────────────────────

    def _eval_live(self, action: str, context: dict) -> PolicyResult | None:
        """POST to OPA REST API. Returns None if unreachable or timed out."""
        input_data = {"action": action, **context}
        url = f"{self._opa_url}/v1/data/{_REST_PATH}"
        try:
            resp = httpx.post(url, json={"input": input_data}, timeout=2.0)
            resp.raise_for_status()
            result = resp.json().get("result", {})
            return _build_result(result, "live")
        except (httpx.TimeoutException, httpx.ConnectError):
            return None
        except httpx.HTTPStatusError as exc:
            log.warning("opa.live_http_error: %s", exc)
            return None

    def _eval_embedded(self, action: str, context: dict) -> PolicyResult:
        """Evaluate the Rego file in-process via ``opa eval`` subprocess."""
        opa_bin = _find_opa_binary()
        if not opa_bin:
            raise RuntimeError(
                "OPA binary not found. Install with: brew install opa  "
                "or download from https://github.com/open-policy-agent/opa/releases"
            )

        input_data = {"action": action, **context}
        proc = subprocess.run(
            [
                opa_bin,
                "eval",
                "--data", str(_REGO_FILE),
                "--stdin-input",
                "--format", "json",
                "data.release_pilot.guardrails",
            ],
            input=json.dumps(input_data),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"opa eval failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )

        data = json.loads(proc.stdout)
        try:
            value = data["result"][0]["expressions"][0]["value"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected opa eval output: {data}") from exc

        return _build_result(value, "embedded")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_result(raw: dict, mode: str) -> PolicyResult:
    allow = raw.get("allow", False)
    deny_val = raw.get("deny", [])
    # Rego sets are JSON arrays; guard against unexpected dict shape
    if isinstance(deny_val, list):
        reasons = deny_val
    elif isinstance(deny_val, dict):
        reasons = list(deny_val.keys())
    else:
        reasons = []
    return PolicyResult(
        allowed=bool(allow),
        denial_reasons=reasons,
        policy_id=_POLICY_ID,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        evaluation_mode=mode,
    )


def _find_opa_binary() -> str | None:
    """Return path to ``opa`` binary or None if not found."""
    if found := shutil.which("opa"):
        return found
    for candidate in [
        Path(__file__).parent.parent / "bin" / "opa",
        Path(".opa_cache") / "opa",
    ]:
        if candidate.exists():
            return str(candidate)
    return None
