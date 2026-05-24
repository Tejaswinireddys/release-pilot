"""3 unit tests for ServiceGraph — blast radius and PCI scope detection."""

import pytest

from src.knowledge.service_graph import ServiceGraph


@pytest.fixture
def graph():
    sg = ServiceGraph()
    sg.load()  # reads config/service_graph.json
    return sg


def test_blast_radius_one_hop(graph):
    """ServiceA-SettlementService has exactly 2 direct consumers."""
    br = graph.blast_radius("ServiceA-SettlementService")
    assert len(br.direct_consumers) == 2
    assert "ServiceB-ReconciliationWorker" in br.direct_consumers
    assert "ServiceB-AuditLogger" in br.direct_consumers


def test_blast_radius_two_hop(graph):
    """2-hop traversal from SettlementService reaches transitive services."""
    br = graph.blast_radius("ServiceA-SettlementService")
    # Hop 2: ReconciliationWorker → [NotificationService, AuditLogger]
    # AuditLogger is already in hop 1, so transitive adds NotificationService
    assert br.total_affected >= 3
    assert len(br.transitive_services) >= 1
    assert "ServiceB-NotificationService" in br.transitive_services


def test_pci_shared_lib_detected(graph):
    """A file under com/example/servicea/common/** triggers PCI scope."""
    pci, reason = graph.check_pci_scope(
        ["com/example/servicea/common/CryptoUtil.java"]
    )
    assert pci is True
    assert "shared-lib" in reason
