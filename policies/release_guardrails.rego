# Release Pilot — deployment guardrails
#
# Evaluated BEFORE every tool call. A non-empty deny set halts execution.
# Written once in Rego; the Python client evaluates this same file in embedded
# mode — no duplicate logic, zero drift risk.
#
# OPA 1.x / Rego v1 syntax required.

package release_pilot.guardrails

import rego.v1

default allow := false

allow if count(deny) == 0

# ── 1. High-risk deploy without human approval ────────────────────────────────
deny contains "HIGH_RISK_NO_APPROVAL" if {
    input.action == "harness.deploy"
    input.risk_level == "HIGH"
    input.human_approval_token == null
}

# ── 2. PCI-scoped operation without human approval ───────────────────────────
deny contains "PCI_NO_APPROVAL" if {
    input.pci_scope_touched == true
    input.human_approval_token == null
}

# ── 3. IAM change needs at least two approvers ───────────────────────────────
deny contains "IAM_MULTI_APPROVAL" if {
    input.terraform_diff.touches_iam == true
    count(input.approval_chain) < 2
}

# ── 4. Canary cannot jump past 50 % before 30-minute soak ───────────────────
deny contains "CANARY_CAP_50PCT" if {
    input.canary_pct > 50
    input.canary_elapsed_minutes < 30
}

# ── 5. Low-confidence SLO verdict must not promote ───────────────────────────
deny contains "LOW_CONFIDENCE_NO_PROMOTE" if {
    input.confidence < 0.6
    input.action == "aws.promote"
}

# ── 6. Degraded metrics block promotion ──────────────────────────────────────
deny contains "PROMOTE_WITH_DEGRADED_METRICS" if {
    input.action == "aws.promote"
    input.metrics_degraded == true
}
