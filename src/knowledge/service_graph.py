"""
Service graph — PCI scope detection, blast-radius calculation, SLO thresholds.

Reads config/service_graph.json. The Risk Analyst uses check_pci_scope to
determine if changed files touch the CDE; the SLO Sentinel uses lookup to
retrieve per-service thresholds.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── PCI signal 1: well-known path segments ────────────────────────────────────
_PCI_PATH_RE = re.compile(
    r"(?i)(?:payment[s]?|card[s]?|billing|cardholder|pci|cvv|pan)/"
)


def _glob_match(pattern: str, path: str) -> bool:
    """Minimal ** glob matching for file-path patterns."""
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return bool(re.fullmatch(regex, path))


@dataclass
class ServiceInfo:
    name: str
    owner_team: str
    pci_scope: bool
    file_paths: list[str]
    direct_consumers: list[str]
    criticality: str
    slos: dict[str, Any]


@dataclass
class BlastRadius:
    service: str
    direct_consumers: list[str]
    transitive_services: list[str]
    total_affected: int


_DEFAULT_CONFIG = Path(__file__).parent.parent.parent / "config" / "service_graph.json"


class ServiceGraph:
    """Reads service_graph.json and answers scope/topology queries."""

    def __init__(self) -> None:
        self._services: dict[str, ServiceInfo] = {}
        self._pci_shared_libs: list[str] = []

    def load(self, path: str | Path | None = None) -> None:
        data = json.loads(Path(path or _DEFAULT_CONFIG).read_text())
        self._pci_shared_libs = data.get("pci_shared_libs", [])
        for name, cfg in data.get("services", {}).items():
            self._services[name] = ServiceInfo(
                name=name,
                owner_team=cfg.get("owner_team", "unknown"),
                pci_scope=cfg.get("pci_scope", False),
                file_paths=cfg.get("file_paths", []),
                direct_consumers=cfg.get("direct_consumers", []),
                criticality=cfg.get("criticality", "low"),
                slos=cfg.get("slos", {}),
            )

    def lookup(self, service_name: str) -> ServiceInfo | None:
        return self._services.get(service_name)

    def blast_radius(self, service_name: str) -> BlastRadius:
        """
        2-hop blast radius from service_name.

        direct_consumers: services that directly consume service_name.
        transitive_services: additional services reachable in hop 2 (not already
                             in direct_consumers).
        """
        svc = self._services.get(service_name)
        direct = list(svc.direct_consumers) if svc else []

        transitive: set[str] = set()
        for dep_name in direct:
            dep = self._services.get(dep_name)
            if dep:
                for grandchild in dep.direct_consumers:
                    if grandchild not in direct and grandchild != service_name:
                        transitive.add(grandchild)

        transitive_list = sorted(transitive)
        return BlastRadius(
            service=service_name,
            direct_consumers=direct,
            transitive_services=transitive_list,
            total_affected=len(direct) + len(transitive_list),
        )

    def check_pci_scope(self, file_paths: list[str]) -> tuple[bool, str]:
        """
        Returns (pci_scope_touched, reason_string).

        3 signals evaluated:
          1. Path regex — well-known PCI path segments.
          2. Service file_paths match — file belongs to a PCI-scoped service.
          3. pci_shared_libs match — file is in a shared PCI library.

        Defaults to (True, "uncertainty-default") when graph has no services
        loaded (ambiguous configuration).
        """
        if not self._services:
            return True, "uncertainty-default"

        for fp in file_paths:
            # Signal 1: path regex
            if _PCI_PATH_RE.search(fp):
                return True, f"path-regex:{fp}"

            # Signal 2: service file_paths flag
            for svc in self._services.values():
                if svc.pci_scope:
                    for pattern in svc.file_paths:
                        if _glob_match(pattern, fp):
                            return True, f"service-file-paths:{svc.name}:{fp}"

            # Signal 3: shared-libs allowlist
            for pattern in self._pci_shared_libs:
                if _glob_match(pattern, fp):
                    return True, f"shared-lib:{pattern}:{fp}"

        return False, "no-pci-signals"
