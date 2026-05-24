"""8 policy tests for release_guardrails.rego.

Each test runs in both modes:
  live      — OPA REST API at OPA_URL; skipped when server is unreachable.
  embedded  — opa eval subprocess; skipped when OPA binary is not in PATH.

Run:
    python -m pytest tests/test_opa_policies.py -v
"""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from src.opa_client import OPAClient, PolicyViolationError

# ── Availability helpers ──────────────────────────────────────────────────────

_OPA_URL = "http://localhost:8181"


def _server_available() -> bool:
    try:
        resp = httpx.get(f"{_OPA_URL}/health", timeout=1.0)
        return resp.status_code == 200
    except Exception:
        return False


def _binary_available() -> bool:
    if shutil.which("opa"):
        return True
    for p in [Path("bin/opa"), Path(".opa_cache/opa")]:
        if p.exists():
            return True
    return False


def _require(mode: str) -> None:
    """Skip this test if the requested mode is not available."""
    if mode == "live" and not _server_available():
        pytest.skip("OPA server not running — start with: docker-compose up -d opa")
    if mode == "embedded" and not _binary_available():
        pytest.skip("OPA binary not found — install with: brew install opa")


def _client(mode: str) -> OPAClient:
    return OPAClient(opa_url=_OPA_URL, mode=mode)


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["live", "embedded"])
def test_happy_path(mode: str) -> None:
    """LOW-risk, non-PCI, no IAM: every rule should stay silent."""
    _require(mode)
    result = _client(mode).check(
        "harness.deploy",
        {
            "risk_level": "LOW",
            "pci_scope_touched": False,
            "human_approval_token": None,
        },
    )
    assert result.allowed is True
    assert result.denial_reasons == []
    assert result.evaluation_mode == mode


@pytest.mark.parametrize("mode", ["live", "embedded"])
def test_high_risk_no_approval_denied(mode: str) -> None:
    """HIGH risk + null token → HIGH_RISK_NO_APPROVAL."""
    _require(mode)
    with pytest.raises(PolicyViolationError) as exc:
        _client(mode).check(
            "harness.deploy",
            {
                "risk_level": "HIGH",
                "human_approval_token": None,
            },
        )
    assert "HIGH_RISK_NO_APPROVAL" in exc.value.denial_reasons
    assert exc.value.action == "harness.deploy"


@pytest.mark.parametrize("mode", ["live", "embedded"])
def test_pci_no_approval_denied(mode: str) -> None:
    """PCI-scoped operation without approval token → PCI_NO_APPROVAL."""
    _require(mode)
    with pytest.raises(PolicyViolationError) as exc:
        _client(mode).check(
            "harness.deploy",
            {
                "pci_scope_touched": True,
                "human_approval_token": None,
                "risk_level": "LOW",
            },
        )
    assert "PCI_NO_APPROVAL" in exc.value.denial_reasons


@pytest.mark.parametrize("mode", ["live", "embedded"])
def test_iam_single_approval_denied(mode: str) -> None:
    """IAM terraform change with only one approver → IAM_MULTI_APPROVAL."""
    _require(mode)
    with pytest.raises(PolicyViolationError) as exc:
        _client(mode).check(
            "harness.deploy",
            {
                "terraform_diff": {"touches_iam": True},
                "approval_chain": ["alice@example.com"],
                "human_approval_token": "tok",
                "risk_level": "LOW",
                "pci_scope_touched": False,
            },
        )
    assert "IAM_MULTI_APPROVAL" in exc.value.denial_reasons


@pytest.mark.parametrize("mode", ["live", "embedded"])
def test_canary_cap_denied(mode: str) -> None:
    """75 % canary before 30-minute soak → CANARY_CAP_50PCT."""
    _require(mode)
    with pytest.raises(PolicyViolationError) as exc:
        _client(mode).check(
            "harness.deploy",
            {
                "canary_pct": 75,
                "canary_elapsed_minutes": 10,
                "human_approval_token": "tok",
                "risk_level": "LOW",
                "pci_scope_touched": False,
            },
        )
    assert "CANARY_CAP_50PCT" in exc.value.denial_reasons


@pytest.mark.parametrize("mode", ["live", "embedded"])
def test_low_confidence_promote_denied(mode: str) -> None:
    """SLO confidence 0.4 blocks aws.promote → LOW_CONFIDENCE_NO_PROMOTE."""
    _require(mode)
    with pytest.raises(PolicyViolationError) as exc:
        _client(mode).check(
            "aws.promote",
            {
                "confidence": 0.4,
                "metrics_degraded": False,
            },
        )
    assert "LOW_CONFIDENCE_NO_PROMOTE" in exc.value.denial_reasons


@pytest.mark.parametrize("mode", ["live", "embedded"])
def test_degraded_metrics_promote_denied(mode: str) -> None:
    """Degraded metrics block aws.promote → PROMOTE_WITH_DEGRADED_METRICS."""
    _require(mode)
    with pytest.raises(PolicyViolationError) as exc:
        _client(mode).check(
            "aws.promote",
            {
                "confidence": 0.95,
                "metrics_degraded": True,
            },
        )
    assert "PROMOTE_WITH_DEGRADED_METRICS" in exc.value.denial_reasons


@pytest.mark.parametrize("mode", ["live", "embedded"])
def test_multiple_denials(mode: str) -> None:
    """PCI + IAM simultaneously → both deny codes present."""
    _require(mode)
    with pytest.raises(PolicyViolationError) as exc:
        _client(mode).check(
            "harness.deploy",
            {
                "pci_scope_touched": True,
                "human_approval_token": None,
                "terraform_diff": {"touches_iam": True},
                "approval_chain": [],
                "risk_level": "LOW",
            },
        )
    assert "PCI_NO_APPROVAL" in exc.value.denial_reasons
    assert "IAM_MULTI_APPROVAL" in exc.value.denial_reasons


# ── PolicyViolationError display ──────────────────────────────────────────────

def test_policy_violation_error_str() -> None:
    """__str__ returns valid JSON with required fields."""
    import json
    err = PolicyViolationError(
        denial_reasons=["PCI_NO_APPROVAL"],
        action="harness.deploy",
        context={"pci_scope_touched": True},
    )
    payload = json.loads(str(err))
    assert payload["error"] == "PolicyViolationError"
    assert payload["action"] == "harness.deploy"
    assert "PCI_NO_APPROVAL" in payload["denial_reasons"]
