"""
Risk Analyst — first agent in the Release Pilot pipeline.

Reads a merged PR, reasons about risk, detects PCI scope, and emits a
structured RiskVerdict consumed by every downstream agent.

Pipeline (analyze):
  1.  Fetch PR diff — GitHub MCP, retry ×3, exponential back-off
  2.  PromptInjectionSanitizer on diff body
  3.  PCIRedactor on sanitized text
  4.  RAGIndex query top_k=5
  5.  DeploymentMemory.get_similar_past_deploys top_k=5
  6.  ServiceGraph.lookup + blast_radius
  7.  Compute feature_signals (deterministic, pre-LLM)
  8.  Compute pci_scope_touched deterministically via ServiceGraph.check_pci_scope
  9.  Build LLM prompt embedding all pre-computed context
  10. Call OpenAI-compatible API (retry ×2 on rate-limit / down)
  11. Parse + validate RiskVerdict; apply post-LLM safety rules
  12. OPAClient.check("risk.assess") for audit trail
  13. Emit OpenTelemetry GenAI span
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Literal

import httpx
from openai import APIConnectionError, OpenAI, RateLimitError
from pydantic import BaseModel, ConfigDict

from config.integrations import github_is_live
from src.knowledge.memory_store import DeploymentMemory
from src.knowledge.rag_index import RAGIndex, RAGResult
from src.knowledge.service_graph import ServiceGraph
from src.opa_client import OPAClient, PolicyViolationError
from src.redactor import PCIRedactor
from src.sanitizer import PromptInjectionSanitizer
from src.telemetry import get_tracer, record_llm_call

log = logging.getLogger(__name__)

# ── Risk band constants ───────────────────────────────────────────────────────

_BAND_LO = {"LOW": 0, "MEDIUM": 31, "HIGH": 61}
_BAND_HI = {"LOW": 30, "MEDIUM": 60, "HIGH": 100}
_MIDPOINT = {"LOW": 15, "MEDIUM": 45, "HIGH": 80}

# ── Pydantic output models ────────────────────────────────────────────────────


class BlastRadius(BaseModel):
    service: str
    direct_consumers: list[str]
    transitive_services: int  # count of hop-2 services


class Strategy(BaseModel):
    canary_pct: int
    bake_minutes: int
    auto_promote: bool


class DiffFeatures(BaseModel):
    added_sloc: int
    deleted_sloc: int
    new_files_only: bool


class DiffusionFeatures(BaseModel):
    files_changed: int
    distinct_authors_last_90d: int


class CriticalityFeatures(BaseModel):
    previous_sevs_in_files: int
    service_is_critical: bool


class ExpertiseFeatures(BaseModel):
    author_is_original_creator: bool
    prior_diffs_landed: int


class FeatureSignals(BaseModel):
    diff_features: DiffFeatures
    diffusion_features: DiffusionFeatures
    criticality_features: CriticalityFeatures
    expertise_features: ExpertiseFeatures


class RiskVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    score: int                        # 0-100; LOW=0-30, MEDIUM=31-60, HIGH=61-100
    rationale: str
    blast_radius: BlastRadius
    pci_scope_touched: bool           # DETERMINISTIC — not set by LLM
    pci_scope_reason: str
    recommended_strategy: Strategy
    guardrails_triggered: list[str]
    historical_references: list[str]  # must be non-empty
    feature_context: str
    feature_signals: FeatureSignals   # PRE-COMPUTED — not set by LLM
    injection_attempts_detected: int
    confidence: float                 # 0.0-1.0


# ── Error types ───────────────────────────────────────────────────────────────


class EscalateToHumanError(Exception):
    """Raised when the pipeline cannot safely produce a verdict."""

    def __init__(self, reason: str, pr_data: dict) -> None:
        self.reason = reason
        self.pr_data = pr_data
        super().__init__(reason)


# ── System prompt (single source of truth) ────────────────────────────────────

_SYSTEM = """You are the Risk Analyst agent in Release Pilot, a deployment-safety system.
Your job is to assess the risk of deploying this pull request and emit a
structured RiskVerdict.

Score from 0 to 100:
- LOW (0-30): pure UI/CSS, isolated bug fixes, well-tested patterns
- MEDIUM (31-60): non-critical service changes, schema additions, new endpoints
- HIGH (61-100): critical service changes, schema migrations, IAM/secrets changes,
  anything touching the cardholder-data flow, PRs similar to past incidents

IMPORTANT:
- pci_scope_touched has ALREADY been computed deterministically. DO NOT change it.
  Read it from the input and pass it through unchanged.
- You MUST cite at least one historical reference from the provided context.
- feature_signals fields are PRE-COMPUTED. DO NOT modify them.
- You decide: risk_level, score, rationale, recommended_strategy, guardrails_triggered.

Return ONLY valid JSON matching the RiskVerdict schema. No prose, no markdown."""


# ── Module-level helpers ──────────────────────────────────────────────────────


def _touches_iam(diff_text: str) -> bool:
    return bool(re.search(
        r"(?i)\b(iam|aws_iam|assume.?role|sts:|IAMPolicy|RolePolicyAttachment|CreateRole|PutRolePolicy)\b",
        diff_text,
    ))


def _touches_secrets(diff_text: str) -> bool:
    return bool(re.search(
        r"(?i)\b(secret|password|api.?key|private.?key|vault|ssm\.get|kms\.decrypt|getSecretValue|SecretManager)\b",
        diff_text,
    ))


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"No valid JSON in LLM response: {text[:200]!r}")


def _compute_feature_signals(
    diff_text: str,
    file_paths: list[str],
    rag_results: list[RAGResult],
    service_info: Any,
    pr_data: dict,
) -> FeatureSignals:
    lines = diff_text.splitlines()
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    deleted = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    new_files_only = "new file mode" in diff_text and deleted == 0

    prev_sevs = sum(
        1 for r in rag_results
        if re.search(r"INC-\d+|incident|sev[12]|severity", r.content_snippet, re.I)
    )
    service_is_critical = (
        getattr(service_info, "criticality", "").lower() == "critical"
        if service_info else False
    )

    return FeatureSignals(
        diff_features=DiffFeatures(
            added_sloc=added,
            deleted_sloc=deleted,
            new_files_only=new_files_only,
        ),
        diffusion_features=DiffusionFeatures(
            files_changed=len(file_paths),
            distinct_authors_last_90d=pr_data.get("distinct_authors_last_90d", 3),
        ),
        criticality_features=CriticalityFeatures(
            previous_sevs_in_files=prev_sevs,
            service_is_critical=service_is_critical,
        ),
        expertise_features=ExpertiseFeatures(
            author_is_original_creator=pr_data.get("author_is_original_creator", False),
            prior_diffs_landed=pr_data.get("prior_diffs_landed", 12),
        ),
    )


def _build_user_message(
    pr_data: dict,
    diff_text: str,
    file_paths: list[str],
    rag_results: list[RAGResult],
    past_deploys: list,
    precomputed: dict,
) -> str:
    pr_num = pr_data.get("pr_number", "?")
    service_id = pr_data.get("service_id", "unknown")

    diff_snippet = diff_text[:3000] + (
        "\n[... diff truncated ...]" if len(diff_text) > 3000 else ""
    )
    rag_text = "\n".join(
        f"  [{r.doc_id}] {r.content_snippet[:300]}" for r in rag_results
    ) or "  (no RAG documents — low confidence)"

    past_text = "\n".join(
        f"  [{r.release_id}] {r.service} {r.outcome} risk_score={r.risk_score}"
        for r in past_deploys[:3]
    ) or "  (no past deploys found)"

    precomputed_json = json.dumps(
        {k: precomputed[k] for k in [
            "pci_scope_touched", "pci_scope_reason", "blast_radius",
            "feature_signals", "feature_context", "injection_attempts_detected",
        ]},
        indent=2,
    )

    return (
        f"PR #{pr_num} — service: {service_id}\n"
        f"Version: {pr_data.get('version', 'unknown')}\n"
        f"Changed files: {', '.join(file_paths) or '(none provided)'}\n"
        "\n=== DIFF (sanitized and PCI-redacted) ===\n"
        f"{diff_snippet}\n"
        "\n=== RAG CONTEXT (cite doc_ids in historical_references) ===\n"
        f"{rag_text}\n"
        "\n=== SIMILAR PAST DEPLOYS ===\n"
        f"{past_text}\n"
        "\n=== PRE-COMPUTED FIELDS — include verbatim in your response ===\n"
        f"{precomputed_json}\n"
        "\n=== OUTPUT SCHEMA ===\n"
        "Return a JSON object with ALL fields:\n"
        "  risk_level              — 'LOW'|'MEDIUM'|'HIGH'  (YOU decide)\n"
        "  score                   — int 0-100 in band for risk_level  (YOU decide)\n"
        "  rationale               — string  (YOU decide)\n"
        "  blast_radius            — copy from PRE-COMPUTED above\n"
        "  pci_scope_touched       — copy from PRE-COMPUTED above\n"
        "  pci_scope_reason        — copy from PRE-COMPUTED above\n"
        "  recommended_strategy    — {canary_pct:int, bake_minutes:int, auto_promote:bool}  (YOU decide)\n"
        "  guardrails_triggered    — list[str]  (YOU decide)\n"
        "  historical_references   — list of doc_ids from RAG CONTEXT  (≥1 required)\n"
        "  feature_context         — copy from PRE-COMPUTED above\n"
        "  feature_signals         — copy from PRE-COMPUTED above\n"
        "  injection_attempts_detected — copy from PRE-COMPUTED above\n"
        "  confidence              — float 0.0-1.0  (YOUR confidence in this assessment)\n"
    )


# ── Agent class ───────────────────────────────────────────────────────────────


class RiskAnalyst:
    """Analyses a merged PR and returns a structured RiskVerdict."""

    def __init__(self) -> None:
        self._llm = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY", "demo"),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
        self._model = os.getenv("AGENT_MODEL", "gpt-4o")
        self._redactor = PCIRedactor()
        self._sanitizer = PromptInjectionSanitizer()
        self._graph = ServiceGraph()
        self._rag = RAGIndex()
        self._memory = DeploymentMemory()
        self._opa = OPAClient()
        self._tracer = get_tracer("risk-analyst")

        # Load service graph and seed demo knowledge at construction time.
        try:
            self._graph.load()
        except Exception as exc:
            log.warning("service_graph.load_failed error=%s", exc)
        try:
            self._rag.seed_demo_data()
        except Exception:
            pass
        try:
            self._memory.seed_demo_history()
        except Exception:
            pass

    # ── Public entry point ────────────────────────────────────────────────────

    def analyze(self, pr_data: dict) -> RiskVerdict:
        """Run the full risk-analysis pipeline and return a RiskVerdict.

        pr_data keys:
          pr_number (int), service_id (str), trace_id (str), version (str),
          diff_body (str, optional), file_paths (list[str], optional),
          author (str, optional), prior_diffs_landed (int, optional),
          author_is_original_creator (bool, optional),
          distinct_authors_last_90d (int, optional).
        """
        with self._tracer.start_as_current_span("risk_analyst.analyze") as span:
            span.set_attribute("gen_ai.agent.name", "risk-analyst")
            span.set_attribute("gen_ai.system", "openai")
            span.set_attribute("gen_ai.operation.name", "agent_invoke")
            span.set_attribute("pr.number", pr_data.get("pr_number", 0))
            span.set_attribute("service.id", pr_data.get("service_id", "unknown"))

            pr_num = pr_data.get("pr_number", 0)
            service_id = pr_data.get("service_id", "unknown")

            # 1. Fetch diff (retry ×3, exponential back-off)
            diff_raw, file_paths = self._fetch_diff(pr_data)

            # 2. Sanitize
            san = self._sanitizer.sanitize(diff_raw)
            injection_count = san.injection_attempts

            # 3. Redact
            red = self._redactor.redact(san.sanitized_text)
            clean_diff = red.redacted_text

            # 4. RAG query
            rag_query = f"deployment risk {service_id} {' '.join(file_paths[:3])}"
            try:
                rag_results = self._rag.query(rag_query, top_k=5)
            except Exception as exc:
                log.warning("rag.query_failed error=%s", exc)
                rag_results = []
            if not rag_results:
                log.warning("rag.zero_results — low_confidence=true")

            # 5. Past deploys
            diff_summary = f"PR#{pr_num} {service_id} {' '.join(file_paths[:3])}"
            try:
                past_deploys = self._memory.get_similar_past_deploys(
                    diff_summary, service_id, top_k=5
                )
            except Exception as exc:
                log.warning("memory.similar_deploys_failed error=%s", exc)
                past_deploys = []

            # 6. Service graph
            service_info = self._graph.lookup(service_id)
            graph_br = self._graph.blast_radius(service_id)
            blast = BlastRadius(
                service=graph_br.service or service_id,
                direct_consumers=graph_br.direct_consumers,
                transitive_services=len(graph_br.transitive_services),
            )

            # 7. Feature signals (deterministic, pre-LLM)
            feature_signals = _compute_feature_signals(
                clean_diff, file_paths, rag_results, service_info, pr_data
            )

            # 8. PCI scope (deterministic — NOT delegated to LLM)
            if file_paths:
                pci_touched, pci_reason = self._graph.check_pci_scope(file_paths)
            else:
                pci_touched, pci_reason = True, "uncertainty-default: no file paths provided"

            # 9. Build prompt
            rag_doc_ids = [r.doc_id for r in rag_results]
            feature_context = (
                "RAG: no results — low confidence"
                if not rag_results
                else (
                    f"RAG: {len(rag_results)} documents; "
                    f"top match: {rag_results[0].doc_id} "
                    f"(score={rag_results[0].similarity_score:.2f})"
                )
            )
            precomputed = {
                "pci_scope_touched": pci_touched,
                "pci_scope_reason": pci_reason,
                "blast_radius": blast.model_dump(),
                "feature_signals": feature_signals.model_dump(),
                "feature_context": feature_context,
                "injection_attempts_detected": injection_count,
            }
            messages: list[dict] = [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": _build_user_message(
                        pr_data, clean_diff, file_paths,
                        rag_results, past_deploys, precomputed,
                    ),
                },
            ]

            # 10. LLM call (retry ×2 on connection/rate errors)
            raw_dict, usage = self._call_llm_with_retry(messages, pr_data)
            if usage:
                record_llm_call(
                    span, self._model,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                )

            # historical_references fallback (pre-_build_verdict)
            hist_refs = raw_dict.get("historical_references") or []
            if not hist_refs:
                if rag_doc_ids:
                    # Append top-1 from RAG without re-prompting
                    raw_dict["historical_references"] = rag_doc_ids[:1]
                else:
                    # RAG also empty — re-prompt LLM once
                    raw_dict = self._reprompt_for_references(messages, raw_dict, pr_data)
                    if not (raw_dict.get("historical_references") or []):
                        raise EscalateToHumanError(
                            "historical_references empty after re-prompt and RAG returned no results",
                            pr_data,
                        )

            # 11. Merge pre-computed, apply deterministic rules, validate schema
            verdict = self._build_verdict(raw_dict, precomputed, clean_diff, pr_data)

            # 12. OPA — audit trail (risk.assess has no deny rules; logged for compliance)
            self._opa_check(verdict)

            # 13. OTel GenAI attributes
            span.set_attribute("release_pilot.risk_level", verdict.risk_level)
            span.set_attribute("release_pilot.pci_scope_touched", verdict.pci_scope_touched)

            log.info(
                "RISK_VERDICT pr=%s service=%s level=%s score=%d pci=%s confidence=%.2f",
                pr_num, service_id, verdict.risk_level,
                verdict.score, verdict.pci_scope_touched, verdict.confidence,
            )
            return verdict

    # ── Step 1: GitHub MCP fetch ──────────────────────────────────────────────

    def _fetch_diff(self, pr_data: dict) -> tuple[str, list[str]]:
        """Fetch PR diff. Retries ×3 with exponential back-off."""
        if pr_data.get("diff_body"):
            return pr_data["diff_body"], pr_data.get("file_paths", [])

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return self._github_mcp_fetch(pr_data.get("pr_number", 0))
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    delay = 2 ** attempt
                    log.warning(
                        "github_mcp.retry attempt=%d/3 backoff=%ds error=%s",
                        attempt + 1, delay, exc,
                    )
                    time.sleep(delay)

        raise EscalateToHumanError(
            f"GitHub MCP unreachable after 3 attempts: {last_exc}", pr_data
        )

    def _github_mcp_fetch(self, pr_number: int) -> tuple[str, list[str]]:
        """Fetch PR diff: real GitHub API, GitHub MCP, or synthetic demo diff.

        Priority:
          1. INTEGRATION_GITHUB_MODE=live  → real GitHub REST API
          2. GITHUB_MCP_URL set            → legacy GitHub MCP endpoint
          3. fallback                      → synthetic PCI-touching demo diff
        """
        if github_is_live():
            result = self._github_live_fetch(pr_number)
            if result is not None:
                return result
            log.warning(
                "github.live.fetch_failed pr=%d — falling back to fixture diff",
                pr_number,
            )

        mcp_url = os.getenv("GITHUB_MCP_URL")
        if mcp_url:
            resp = httpx.get(f"{mcp_url}/pulls/{pr_number}/diff", timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            return data["diff"], data.get("changed_files", [])

        # Demo synthetic diff (PCI-touching to exercise all safety rules)
        return (
            "diff --git a/payment/processor.py b/payment/processor.py\n"
            "--- a/payment/processor.py\n+++ b/payment/processor.py\n"
            "@@ -10,6 +10,9 @@ def charge(amount, card_number):\n"
            "+    if not verify_luhn(card_number):\n"
            "+        raise ValueError('invalid PAN')\n"
            "+    validate_cvv(card_token, cvv)\n"
        ), ["payment/processor.py"]

    def _github_live_fetch(self, pr_number: int) -> tuple[str, list[str]] | None:
        """Call the real GitHub REST API to fetch a PR diff and changed-file list.

        Required env vars:
            GITHUB_TOKEN — personal access token or fine-grained token with
                           contents:read and pull_requests:read permissions
            GITHUB_REPO  — owner/repo, e.g. "acme-corp/payment-service"

        Returns (diff_text, file_paths) on success, None on any failure.
        The caller logs a warning and falls back to the fixture diff on None.
        """
        token = os.getenv("GITHUB_TOKEN", "").strip()
        repo = os.getenv("GITHUB_REPO", "").strip()

        if not token or not repo:
            log.warning(
                "github.live.missing_config — GITHUB_TOKEN and GITHUB_REPO are required "
                "when INTEGRATION_GITHUB_MODE=live"
            )
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            resp = httpx.get(
                f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files",
                headers=headers,
                timeout=10.0,
            )
            resp.raise_for_status()
            files: list[dict] = resp.json()

            file_paths = [f["filename"] for f in files]

            # Reconstruct unified diff from per-file patch fields
            diff_parts: list[str] = []
            for f in files:
                patch = f.get("patch", "")
                if patch:
                    diff_parts.append(
                        f"diff --git a/{f['filename']} b/{f['filename']}\n"
                        f"--- a/{f['filename']}\n"
                        f"+++ b/{f['filename']}\n"
                        f"{patch}\n"
                    )

            diff = (
                "\n".join(diff_parts)
                or f"# PR #{pr_number} in {repo} — no patch content (binary or empty files)"
            )

            log.info(
                "github.live.fetched pr=%d repo=%s files=%d diff_bytes=%d",
                pr_number, repo, len(file_paths), len(diff),
            )
            return diff, file_paths

        except httpx.HTTPStatusError as exc:
            log.warning(
                "github.live.fetch_failed status=%d body=%s",
                exc.response.status_code, exc.response.text[:200],
            )
            return None
        except Exception as exc:
            log.warning("github.live.fetch_failed error=%s", exc)
            return None

    # ── Step 10: LLM call ─────────────────────────────────────────────────────

    def _call_llm_with_retry(
        self, messages: list[dict], pr_data: dict
    ) -> tuple[dict, Any]:
        """Returns (parsed_dict, usage). Retries ×2 on rate-limit / connection error."""
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = self._llm.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    timeout=60,
                )
                content = resp.choices[0].message.content or ""
                parsed = self._parse_json_with_retry(content, messages, pr_data)
                return parsed, resp.usage
            except (RateLimitError, APIConnectionError) as exc:
                last_exc = exc
                if attempt == 0:
                    log.warning("openai.retry attempt=1/2 error=%s", exc)
                    time.sleep(5)

        raise EscalateToHumanError(
            f"OpenAI unavailable after 2 attempts: {last_exc}", pr_data
        )

    def _parse_json_with_retry(
        self, content: str, messages: list[dict], pr_data: dict
    ) -> dict:
        """Parse JSON from LLM content. One retry with schema reminder on failure."""
        try:
            return _extract_json(content)
        except (json.JSONDecodeError, ValueError):
            log.warning("json.parse_failed — retrying with schema reminder")

        retry_messages = [
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": (
                    "Your previous response was not valid JSON. "
                    "Return ONLY a JSON object matching the RiskVerdict schema. "
                    "No prose, no markdown fences."
                ),
            },
        ]
        try:
            resp = self._llm.chat.completions.create(
                model=self._model,
                messages=retry_messages,
                response_format={"type": "json_object"},
                temperature=0.0,
                timeout=60,
            )
            return _extract_json(resp.choices[0].message.content or "")
        except Exception as exc:
            raise EscalateToHumanError(
                f"LLM returned malformed JSON after retry: {exc}", pr_data
            )

    def _reprompt_for_references(
        self, messages: list[dict], prior: dict, pr_data: dict
    ) -> dict:
        """Re-prompt LLM to include at least one historical_reference."""
        log.warning("historical_references.empty — re-prompting LLM for citations")
        reminder_messages = [
            *messages,
            {"role": "assistant", "content": json.dumps(prior)},
            {
                "role": "user",
                "content": (
                    "historical_references was empty in your previous response. "
                    "You MUST cite at least one document ID from the RAG CONTEXT above. "
                    "Return the complete RiskVerdict JSON with a non-empty "
                    "historical_references list."
                ),
            },
        ]
        try:
            resp = self._llm.chat.completions.create(
                model=self._model,
                messages=reminder_messages,
                response_format={"type": "json_object"},
                temperature=0.0,
                timeout=60,
            )
            return _extract_json(resp.choices[0].message.content or "")
        except Exception as exc:
            log.error("reprompt_for_references.failed error=%s", exc)
            return prior

    # ── Step 11: post-LLM validation + deterministic rules ────────────────────

    def _build_verdict(
        self, raw: dict, precomputed: dict, diff_text: str, pr_data: dict
    ) -> RiskVerdict:
        """Merge pre-computed fields, apply safety rules, validate schema."""
        merged: dict = {**raw}

        # Pre-computed fields always override LLM output — no exceptions
        for key in (
            "pci_scope_touched", "pci_scope_reason", "blast_radius",
            "feature_signals", "feature_context", "injection_attempts_detected",
        ):
            merged[key] = precomputed[key]

        # Defaults for LLM-decided fields if missing or malformed
        merged.setdefault("risk_level", "MEDIUM")
        merged.setdefault("score", _MIDPOINT["MEDIUM"])
        merged.setdefault("rationale", "Risk assessment incomplete — defaulted to MEDIUM.")
        merged.setdefault("guardrails_triggered", [])
        merged.setdefault("confidence", 0.7)

        strategy = merged.get("recommended_strategy")
        if not isinstance(strategy, dict):
            strategy = {}
        strategy.setdefault("canary_pct", 10)
        strategy.setdefault("bake_minutes", 30)
        strategy.setdefault("auto_promote", False)
        merged["recommended_strategy"] = strategy

        # Rule 1: risk_level cannot be LOW if pci OR IAM OR secrets
        level: str = merged["risk_level"]
        if level == "LOW" and (
            precomputed["pci_scope_touched"]
            or _touches_iam(diff_text)
            or _touches_secrets(diff_text)
        ):
            level = "MEDIUM"
            merged["risk_level"] = level
            merged["guardrails_triggered"].append("auto-escalated by safety rule")
            log.info("guardrail.escalated LOW→MEDIUM reason=pci_or_iam_or_secrets")

        # Rule 2: auto_promote must be false when HIGH
        if level == "HIGH" and strategy.get("auto_promote"):
            strategy["auto_promote"] = False
            merged["recommended_strategy"] = strategy
            log.info("guardrail.auto_promote_cleared reason=HIGH_risk")

        # Rule 3: historical_references must be non-empty (fallback handled in analyze)
        if not (merged.get("historical_references") or []):
            raise EscalateToHumanError(
                "historical_references empty in _build_verdict — should have been caught earlier",
                pr_data,
            )

        # Rule 4: blast_radius.service must be non-empty
        br = merged.get("blast_radius", {})
        if isinstance(br, dict) and not br.get("service"):
            br["service"] = pr_data.get("service_id", "unknown")
            merged["blast_radius"] = br

        # Rule 5: score must fall within the risk_level band
        try:
            score = int(merged.get("score", _MIDPOINT[level]))
        except (TypeError, ValueError):
            score = _MIDPOINT[level]
        if not (_BAND_LO[level] <= score <= _BAND_HI[level]):
            log.warning(
                "guardrail.score_corrected original=%s level=%s → midpoint=%s",
                score, level, _MIDPOINT[level],
            )
            score = _MIDPOINT[level]
        merged["score"] = score

        # Rule 6: confidence must be 0.0-1.0
        try:
            conf = float(merged.get("confidence", 0.7))
            merged["confidence"] = max(0.0, min(1.0, conf))
        except (TypeError, ValueError):
            merged["confidence"] = 0.7

        return RiskVerdict(**merged)

    # ── Step 12: OPA audit trail ──────────────────────────────────────────────

    def _opa_check(self, verdict: RiskVerdict) -> None:
        """OPA check for audit trail. risk.assess has no deny rules in current policy."""
        try:
            self._opa.check(
                action="risk.assess",
                context={
                    "risk_level": verdict.risk_level,
                    "pci_scope_touched": verdict.pci_scope_touched,
                    "score": verdict.score,
                },
            )
        except PolicyViolationError as exc:
            # Advisory only — log for audit trail, do not halt
            log.warning("opa.risk_assess_denied reasons=%s", exc.denial_reasons)
