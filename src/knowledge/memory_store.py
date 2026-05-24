"""
Deployment memory store — historical deploy outcomes for the Risk Analyst.

Persists DeploymentRecords to SQLite. Supports semantic similarity search over
diff summaries (cosine sim when embeddings are available, keyword fallback when
not). Scoped by service to prevent cross-service noise in risk scoring.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path(__file__).parent.parent.parent / ".agent_memory.db"


def _serialize_f32(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _deserialize_f32(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class DeploymentRecord:
    release_id: str
    service: str
    risk_score: int
    outcome: str                        # PROMOTE | ROLLBACK
    ttd_seconds: float
    incident_ids: list[str]
    timestamp: str
    diff_summary: str
    diff_embedding: list[float] | None = None


class DeploymentMemory:
    """SQLite-backed store for deployment outcomes used by the Risk Analyst."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        path = db_path or os.getenv("MEMORY_DB_PATH", str(_DEFAULT_DB))
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._setup_schema()
        self._api_key = os.getenv("OPENAI_API_KEY", "")
        self._model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
        self._base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    def _setup_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS deployments (
                release_id      TEXT PRIMARY KEY,
                service         TEXT NOT NULL,
                risk_score      INTEGER NOT NULL,
                outcome         TEXT NOT NULL,
                ttd_seconds     REAL NOT NULL,
                incident_ids    TEXT DEFAULT '[]',
                timestamp       TEXT NOT NULL,
                diff_summary    TEXT NOT NULL DEFAULT '',
                diff_emb_blob   BLOB
            );
        """)
        self._conn.commit()

    # ── Public API ────────────────────────────────────────────────────────────

    def record_outcome(
        self,
        release_id: str,
        service: str,
        risk_score: int,
        outcome: str,
        ttd_seconds: float,
        incident_ids: list[str],
        diff_summary: str = "",
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        blob = self._maybe_embed_blob(diff_summary)
        self._conn.execute(
            """INSERT OR REPLACE INTO deployments
               (release_id, service, risk_score, outcome, ttd_seconds,
                incident_ids, timestamp, diff_summary, diff_emb_blob)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (release_id, service, risk_score, outcome, ttd_seconds,
             json.dumps(incident_ids), ts, diff_summary, blob),
        )
        self._conn.commit()

    def get_recent_outcomes(self, service: str, limit: int = 20) -> list[DeploymentRecord]:
        rows = self._conn.execute(
            """SELECT * FROM deployments
               WHERE service = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (service, limit),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_similar_past_deploys(
        self, diff_summary: str, service: str, top_k: int = 5
    ) -> list[DeploymentRecord]:
        rows = self._conn.execute(
            "SELECT * FROM deployments WHERE service = ?", (service,)
        ).fetchall()
        if not rows:
            return []

        q_vec = self._try_embed(diff_summary)
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            if q_vec and row["diff_emb_blob"]:
                vec = _deserialize_f32(bytes(row["diff_emb_blob"]))
                score = _cosine_similarity(q_vec, vec)
            else:
                score = self._keyword_score(diff_summary, row["diff_summary"])
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._row_to_record(row) for _, row in scored[:top_k]]

    def seed_demo_history(self) -> int:
        """Seed 10 historical deployment records for demo and testing."""
        records = _DEMO_HISTORY
        for rec in records:
            self.record_outcome(**rec)
        return len(records)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _row_to_record(self, row: sqlite3.Row) -> DeploymentRecord:
        blob = row["diff_emb_blob"]
        emb = _deserialize_f32(bytes(blob)) if blob else None
        return DeploymentRecord(
            release_id=row["release_id"],
            service=row["service"],
            risk_score=row["risk_score"],
            outcome=row["outcome"],
            ttd_seconds=row["ttd_seconds"],
            incident_ids=json.loads(row["incident_ids"]),
            timestamp=row["timestamp"],
            diff_summary=row["diff_summary"],
            diff_embedding=emb,
        )

    def _maybe_embed_blob(self, text: str) -> bytes | None:
        if not self._api_key or not text:
            return None
        try:
            vec = self._embed(text[:4096])
            return _serialize_f32(vec)
        except Exception:
            return None

    def _try_embed(self, text: str) -> list[float] | None:
        if not self._api_key or not text:
            return None
        try:
            return self._embed(text[:4096])
        except Exception:
            return None

    def _embed(self, text: str) -> list[float]:
        from openai import OpenAI
        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        resp = client.embeddings.create(model=self._model, input=text)
        return resp.data[0].embedding

    @staticmethod
    def _keyword_score(query: str, doc: str) -> float:
        q_tok = frozenset(re.findall(r"\w+", query.lower()))
        d_tok = frozenset(re.findall(r"\w+", doc.lower()))
        if not q_tok:
            return 0.0
        return len(q_tok & d_tok) / len(q_tok)


# ── Demo seed data ────────────────────────────────────────────────────────────

_DEMO_HISTORY: list[dict[str, Any]] = [
    # ── 3 SettlementService ROLLBACKs linked to known incidents ──────────────
    dict(
        release_id="REL-SA-SS-001",
        service="ServiceA-SettlementService",
        risk_score=72,
        outcome="ROLLBACK",
        ttd_seconds=2820.0,
        incident_ids=["INC-4421"],
        diff_summary=(
            "Changed retry policy: removed jitter parameter, reduced backoff cap. "
            "Upgraded connection pool size from 50 to 100 concurrent connections."
        ),
    ),
    dict(
        release_id="REL-SA-SS-002",
        service="ServiceA-SettlementService",
        risk_score=65,
        outcome="ROLLBACK",
        ttd_seconds=1320.0,
        incident_ids=["INC-4502"],
        diff_summary=(
            "Schema migration: RENAME COLUMN settlement_status to status. "
            "Added index on (merchant_id, created_at). No rollback plan included."
        ),
    ),
    dict(
        release_id="REL-SA-SS-003",
        service="ServiceA-SettlementService",
        risk_score=58,
        outcome="ROLLBACK",
        ttd_seconds=2100.0,
        incident_ids=["INC-4577"],
        diff_summary=(
            "Bumped AuthCallback default timeout from 500ms to 5000ms. "
            "Added verbose debug logging for all settlement steps."
        ),
    ),
    # ── 4 healthy PROMOTE deploys across services ────────────────────────────
    dict(
        release_id="REL-SA-SS-004",
        service="ServiceA-SettlementService",
        risk_score=32,
        outcome="PROMOTE",
        ttd_seconds=0.0,
        incident_ids=[],
        diff_summary=(
            "Added jitter floor 500ms to retry policy (INC-4421 follow-up). "
            "Bumped pci-shared-crypto from 3.1.0 to 3.2.0."
        ),
    ),
    dict(
        release_id="REL-SA-AUTH-001",
        service="ServiceA-AuthService",
        risk_score=28,
        outcome="PROMOTE",
        ttd_seconds=0.0,
        incident_ids=[],
        diff_summary=(
            "Upgraded JWT library to 4.2.1 to patch CVE-2026-5678. "
            "No functional changes."
        ),
    ),
    dict(
        release_id="REL-SA-GW-001",
        service="ServiceA-PaymentGateway",
        risk_score=41,
        outcome="PROMOTE",
        ttd_seconds=0.0,
        incident_ids=[],
        diff_summary=(
            "Added /v2/payments/void endpoint. Implemented idempotency key validation. "
            "Updated OpenAPI spec."
        ),
    ),
    dict(
        release_id="REL-SA-AUTH-002",
        service="ServiceA-AuthorizationService",
        risk_score=19,
        outcome="PROMOTE",
        ttd_seconds=0.0,
        incident_ids=[],
        diff_summary=(
            "Reduced AuthCallback timeout from 5s back to 500ms (INC-4577 follow-up). "
            "Added bulkhead isolation for external calls."
        ),
    ),
    # ── 3 healthy ServiceB deploys ────────────────────────────────────────────
    dict(
        release_id="REL-SB-RW-001",
        service="ServiceB-ReconciliationWorker",
        risk_score=22,
        outcome="PROMOTE",
        ttd_seconds=0.0,
        incident_ids=[],
        diff_summary=(
            "Optimised SQL query for nightly reconciliation batch. "
            "Reduced median run time by 18%."
        ),
    ),
    dict(
        release_id="REL-SB-TS-001",
        service="ServiceB-TransactionService",
        risk_score=15,
        outcome="PROMOTE",
        ttd_seconds=0.0,
        incident_ids=[],
        diff_summary=(
            "Added circuit breaker around upstream notification calls. "
            "Default open threshold: 10% error over 60s window."
        ),
    ),
    dict(
        release_id="REL-SB-NS-001",
        service="ServiceB-NotificationService",
        risk_score=12,
        outcome="PROMOTE",
        ttd_seconds=0.0,
        incident_ids=[],
        diff_summary=(
            "Switched email provider SDK from v1 to v2. "
            "No API contract changes for callers."
        ),
    ),
]
