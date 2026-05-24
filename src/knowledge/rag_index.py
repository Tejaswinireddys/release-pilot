"""
sqlite-vec-backed RAG index for deployment runbooks and incident history.

Uses OpenAI-compatible embeddings (EMBEDDING_MODEL env var). Falls back to
pure-Python cosine similarity when sqlite-vec is unavailable, and to keyword
overlap when no API key is present. All embeddings are cached — each unique
document is embedded exactly once.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EMBEDDING_DIM = 3072  # text-embedding-3-large; use 1536 for text-embedding-3-small


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


def _doc_id(content: str, metadata: dict) -> str:
    payload = content + json.dumps(metadata, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class RAGResult:
    doc_id: str
    content_snippet: str          # first 500 chars of content
    metadata: dict[str, Any] = field(default_factory=dict)
    similarity_score: float = 0.0


class RAGIndex:
    """
    Semantic retrieval index backed by SQLite (+ sqlite-vec when available).

    Embedding fallback chain:
      1. sqlite-vec KNN search    (OpenAI key present + sqlite-vec installed)
      2. Pure-Python cosine sim   (OpenAI key present, sqlite-vec missing)
      3. Keyword overlap scoring  (no OpenAI key)
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._setup_schema()
        self._api_key = os.getenv("OPENAI_API_KEY", "")
        self._model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
        self._base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self._vec_available = self._try_load_vec()

    def _try_load_vec(self) -> bool:
        if not self._api_key:
            return False
        try:
            import sqlite_vec
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
                    doc_id TEXT PRIMARY KEY,
                    embedding FLOAT[{EMBEDDING_DIM}]
                )
            """)
            self._conn.commit()
            return True
        except Exception:
            return False

    def _setup_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id           TEXT PRIMARY KEY,
                content      TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}',
                created_at   REAL DEFAULT (unixepoch())
            );
            CREATE TABLE IF NOT EXISTS embeddings (
                doc_id        TEXT PRIMARY KEY,
                embedding_blob BLOB NOT NULL,
                FOREIGN KEY (doc_id) REFERENCES documents(id)
            );
        """)
        self._conn.commit()

    # ── Public API ────────────────────────────────────────────────────────────

    def index_document(self, content: str, metadata: dict | None = None) -> str:
        """Store document; embed it exactly once. Returns doc_id."""
        meta = metadata or {}
        doc_id = _doc_id(content, meta)
        exists = self._conn.execute(
            "SELECT 1 FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
        if not exists:
            self._conn.execute(
                "INSERT INTO documents (id, content, metadata_json) VALUES (?, ?, ?)",
                (doc_id, content, json.dumps(meta)),
            )
            self._conn.commit()
            self._maybe_embed(doc_id, content)
        return doc_id

    def query(self, question: str, top_k: int = 5, threshold: float = 0.7) -> list[RAGResult]:
        """Semantic (or keyword fallback) search. Filters by threshold."""
        if self._vec_available:
            results = self._vec_search(question, top_k)
        elif self._api_key:
            results = self._cosine_search(question, top_k)
        else:
            results = self._keyword_search(question, top_k)
        return [r for r in results if r.similarity_score >= threshold]

    def build_from_directory(self, path: str) -> int:
        """Index all .md files in path, splitting on '## Service:' headers."""
        count = 0
        for fp in Path(path).rglob("*.md"):
            text = fp.read_text(errors="replace")
            chunks = _split_markdown(text)
            base_meta = {"source_file": str(fp)}
            for chunk in chunks:
                self.index_document(chunk, {**base_meta, "chunk_header": _first_line(chunk)})
                count += 1
        return count

    def seed_demo_data(self) -> int:
        """Seed 10 inline demo documents covering ServiceA, ServiceB, and incidents."""
        docs = _DEMO_DOCS
        for content, meta in docs:
            self.index_document(content, meta)
        return len(docs)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _maybe_embed(self, doc_id: str, content: str) -> None:
        if not self._api_key:
            return
        try:
            vec = self._embed(content[:8192])  # token limit guard
            blob = _serialize_f32(vec)
            self._conn.execute(
                "INSERT OR REPLACE INTO embeddings (doc_id, embedding_blob) VALUES (?, ?)",
                (doc_id, blob),
            )
            if self._vec_available:
                self._conn.execute(
                    "INSERT OR REPLACE INTO vec_embeddings (doc_id, embedding) VALUES (?, ?)",
                    (doc_id, blob),
                )
            self._conn.commit()
        except Exception:
            pass  # embedding failure is non-fatal; keyword fallback still works

    def _embed(self, text: str) -> list[float]:
        from openai import OpenAI
        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        resp = client.embeddings.create(model=self._model, input=text)
        return resp.data[0].embedding

    def _vec_search(self, query: str, top_k: int) -> list[RAGResult]:
        try:
            blob = _serialize_f32(self._embed(query[:8192]))
            rows = self._conn.execute("""
                SELECT v.doc_id, d.content, d.metadata_json, v.distance
                FROM vec_embeddings v
                JOIN documents d ON d.id = v.doc_id
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance
            """, (blob, top_k)).fetchall()
            return [
                RAGResult(
                    doc_id=r["doc_id"],
                    content_snippet=r["content"][:500],
                    metadata=json.loads(r["metadata_json"]),
                    similarity_score=max(0.0, 1.0 - r["distance"]),
                )
                for r in rows
            ]
        except Exception:
            return self._cosine_search(query, top_k)

    def _cosine_search(self, query: str, top_k: int) -> list[RAGResult]:
        try:
            q_vec = self._embed(query[:8192])
        except Exception:
            return self._keyword_search(query, top_k)
        rows = self._conn.execute(
            "SELECT doc_id, embedding_blob FROM embeddings"
        ).fetchall()
        scored: list[tuple[float, str]] = []
        for row in rows:
            vec = _deserialize_f32(row["embedding_blob"])
            score = _cosine_similarity(q_vec, vec)
            scored.append((score, row["doc_id"]))
        scored.sort(reverse=True)
        results = []
        for score, doc_id in scored[:top_k]:
            doc = self._conn.execute(
                "SELECT content, metadata_json FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
            if doc:
                results.append(RAGResult(
                    doc_id=doc_id,
                    content_snippet=doc["content"][:500],
                    metadata=json.loads(doc["metadata_json"]),
                    similarity_score=score,
                ))
        return results

    def _keyword_search(self, query: str, top_k: int) -> list[RAGResult]:
        tokens = frozenset(re.findall(r"\w+", query.lower()))
        rows = self._conn.execute(
            "SELECT id, content, metadata_json FROM documents"
        ).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            doc_tokens = frozenset(re.findall(r"\w+", row["content"].lower()))
            overlap = len(tokens & doc_tokens)
            if overlap > 0:
                score = overlap / (len(tokens) + 1)
                scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            RAGResult(
                doc_id=row["id"],
                content_snippet=row["content"][:500],
                metadata=json.loads(row["metadata_json"]),
                similarity_score=score,
            )
            for score, row in scored[:top_k]
        ]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_markdown(content: str) -> list[str]:
    """Split on '## Service:' section headers; return non-empty chunks."""
    parts = re.split(r"(?m)^(?=## Service:)", content)
    return [p.strip() for p in parts if p.strip()]


def _first_line(text: str) -> str:
    return text.split("\n", 1)[0].strip()


# ── Demo seed documents ───────────────────────────────────────────────────────

_DEMO_DOCS: list[tuple[str, dict]] = [
    (
        "## Service: ServiceA AuthService\n\n"
        "Owner: team-a-core | PCI scope: true\n"
        "Handles authentication for all payment transactions.\n"
        "SLO: error_rate < 0.1%, p99 < 200ms, availability >= 99.99%\n"
        "Direct consumers: ServiceA-UI, ServiceA-PaymentGateway\n"
        "Key dependencies: ServiceA-AuthorizationService, token-store\n"
        "On-call: #team-a-core-oncall | Runbook: https://wiki/servicea-auth-runbook",
        {"service": "ServiceA-AuthService", "type": "service_doc", "pci": True},
    ),
    (
        "## Service: ServiceA SettlementService\n\n"
        "Owner: team-a-core | PCI scope: true\n"
        "Processes end-of-day settlement batches. Writes final transaction records.\n"
        "SLO: error_rate < 0.1%, p99 < 400ms, availability >= 99.99%\n"
        "Direct consumers: ServiceB-ReconciliationWorker, ServiceB-AuditLogger\n"
        "Key integrations: AuthCallback, downstream ledger APIs\n"
        "On-call: #team-a-core-oncall | Runbook: https://wiki/servicea-settlement-runbook",
        {"service": "ServiceA-SettlementService", "type": "service_doc", "pci": True},
    ),
    (
        "## Service: ServiceA PaymentGateway\n\n"
        "Owner: team-a-core | PCI scope: true\n"
        "API contract v2.3 endpoints:\n"
        "  POST /v2/payments/authorize\n"
        "  POST /v2/payments/capture\n"
        "  POST /v2/payments/refund\n"
        "  GET  /v2/payments/{id}/status\n"
        "Rate limits: 5000 req/s per merchant. Breaker: 5% error opens at 30s.\n"
        "Direct consumers: ServiceA-UI",
        {"service": "ServiceA-PaymentGateway", "type": "api_contract", "pci": True},
    ),
    (
        "## Service: ServiceB ReconciliationWorker\n\n"
        "Owner: team-b-core | PCI scope: false\n"
        "Batch job that consumes settlement records and produces reconciliation reports.\n"
        "Runs nightly at 02:00 UTC. SLA: complete within 4 hours.\n"
        "Direct consumers: ServiceB-NotificationService, ServiceB-AuditLogger\n"
        "Depends on: ServiceA-SettlementService (upstream feed)\n"
        "On-call: #team-b-core-oncall",
        {"service": "ServiceB-ReconciliationWorker", "type": "service_doc", "pci": False},
    ),
    (
        "## Service: ServiceB TransactionService\n\n"
        "Owner: team-b-core | PCI scope: false\n"
        "Handles non-PCI transaction metadata. Does NOT store PAN or CVV.\n"
        "SLO: error_rate < 1%, p99 < 1500ms, availability >= 99.9%\n"
        "Throughput limit: 2000 req/s. Backpressure: 503 after queue depth > 10k.\n"
        "Direct consumers: ServiceB-NotificationService",
        {"service": "ServiceB-TransactionService", "type": "service_doc", "pci": False},
    ),
    (
        "## Incident INC-4421\n\n"
        "Service: ServiceA-SettlementService | Date: 2026-02-15\n"
        "Severity: P1 | Duration: 47 minutes\n"
        "Root cause: retry loop with exponential backoff misconfiguration. "
        "A config change removed the jitter parameter, causing thundering-herd "
        "retries against the downstream ledger during a transient network blip. "
        "The SettlementService saturated its own connection pool within 8 minutes.\n"
        "Resolution: reverted config, added jitter floor of 500ms, deployed hotfix.\n"
        "Action items: add connection-pool saturation alert, add retry config validation.",
        {"incident_id": "INC-4421", "type": "incident", "service": "ServiceA-SettlementService"},
    ),
    (
        "## Incident INC-4502\n\n"
        "Service: ServiceA-SettlementService | Date: 2026-03-22\n"
        "Severity: P2 | Duration: 22 minutes\n"
        "Root cause: DB lock contention after schema migration. "
        "A RENAME COLUMN migration acquired an ACCESS EXCLUSIVE lock on the settlements "
        "table for 4 minutes, blocking all settlement writes. p99 latency spiked to 12s.\n"
        "Resolution: kill long-running migration, rolled back schema change, re-ran with "
        "a shadow-column approach during off-peak window.\n"
        "Action items: require off-peak window for lock-escalating migrations, add migration "
        "lock-wait timeout of 30s.",
        {"incident_id": "INC-4502", "type": "incident", "service": "ServiceA-SettlementService"},
    ),
    (
        "## Incident INC-4577\n\n"
        "Service: ServiceA-SettlementService | Date: 2026-04-08\n"
        "Severity: P2 | Duration: 35 minutes\n"
        "Root cause: downstream AuthCallback timeout cascade. "
        "AuthCallback raised its default timeout from 500ms to 5s in a routine config "
        "change without notifying SettlementService. Settlement batch workers began "
        "holding threads waiting on AuthCallback, exhausting the thread pool. "
        "Error rate reached 14% before circuit breaker opened.\n"
        "Resolution: reduced AuthCallback timeout back to 500ms, added bulkhead isolation "
        "for AuthCallback calls in SettlementService.\n"
        "Action items: cross-team timeout change notification process, bulkhead pattern "
        "for all external calls.",
        {"incident_id": "INC-4577", "type": "incident", "service": "ServiceA-SettlementService"},
    ),
    (
        "## ServiceA Ownership Graph\n\n"
        + json.dumps({
            "ServiceA-AuthService": {
                "owner": "team-a-core",
                "oncall": "#team-a-core-oncall",
                "consumers": ["ServiceA-UI", "ServiceA-PaymentGateway"],
            },
            "ServiceA-SettlementService": {
                "owner": "team-a-core",
                "oncall": "#team-a-core-oncall",
                "consumers": ["ServiceB-ReconciliationWorker", "ServiceB-AuditLogger"],
            },
            "ServiceA-PaymentGateway": {
                "owner": "team-a-core",
                "oncall": "#team-a-core-oncall",
                "consumers": ["ServiceA-UI"],
            },
            "ServiceA-AuthorizationService": {
                "owner": "team-a-core",
                "oncall": "#team-a-core-oncall",
                "consumers": ["ServiceA-SettlementService"],
            },
            "ServiceA-UI": {
                "owner": "team-a-frontend",
                "oncall": "#team-a-frontend-oncall",
                "consumers": [],
            },
        }, indent=2),
        {"type": "ownership_graph", "scope": "ServiceA"},
    ),
    (
        "## Release Notes ServiceA v2.3.1\n\n"
        "Date: 2026-05-01 | Outcome: PROMOTED | Risk: MEDIUM (score 48)\n"
        "PCI scope: true | SOX scope: true\n"
        "Changes:\n"
        "  - Upgraded pci-shared-crypto from 3.1.0 to 3.2.0 (CVE-2026-1234 patch)\n"
        "  - Added jitter floor to retry policy (INC-4421 follow-up)\n"
        "  - Bumped AuthCallback timeout to 500ms from 300ms (INC-4577 follow-up)\n"
        "Canary: 5 steps, all SLOs green. No error-rate regression detected.\n"
        "Approvals: pci-assessor@example.com, cab-lead@example.com\n"
        "Audit hash: a3f8c1d2e4b5...",
        {
            "type": "release_note",
            "service": "ServiceA",
            "version": "v2.3.1",
            "outcome": "PROMOTED",
        },
    ),
]


if __name__ == "__main__":
    idx = RAGIndex()
    n = idx.seed_demo_data()
    print(f"Seeded {n} documents.\n")

    queries = [
        "Who owns the ServiceB reconciliation work?",
        "What was the root cause of INC-4421?",
        "What services consume ServiceA SettlementService?",
    ]
    for q in queries:
        print(f"Query: {q}")
        results = idx.query(q, top_k=3, threshold=0.0)
        for i, r in enumerate(results[:3], 1):
            print(f"  [{i}] {r.doc_id} (score={r.similarity_score:.3f})")
            print(f"      {r.content_snippet[:120].replace(chr(10), ' ')}")
        print()
