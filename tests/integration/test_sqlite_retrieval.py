"""Actual SQLite scope projection and query use through the compiled graph."""

from __future__ import annotations

import json
import sqlite3

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SupplierCorrectiveActionEvidenceBriefAgent
from src.services import SqlitePerspectiveKnowledgeBase, wire_services


def test_sqlite_search_projects_saved_scope_and_full_graph_uses_business_query():
    connection = sqlite3.connect(":memory:")
    kb = SqlitePerspectiveKnowledgeBase(connection)
    kb.add_projection(
        tenant_ref="partition_a",
        rows=[
            {
                "record_type": "perspective",
                "source_id": "perspective_a",
                "topic": "bearing vibration inspection",
            },
            {
                "record_type": "perspective",
                "source_id": "perspective_a_irrelevant",
                "topic": "paint colour documentation",
            },
        ],
    )
    kb.add_projection(
        tenant_ref="partition_b",
        rows=[
            {
                "record_type": "perspective",
                "source_id": "perspective_b",
                "topic": "bearing vibration inspection",
            }
        ],
    )
    services = wire_services(kb=kb)
    services.requirements.add_records(
        tenant_ref="partition_a",
        rows=[{"source_id": "req_sql", "text": "required", "approved": True, "version": "v1"}],
    )
    services.cases.store_case(
        tenant_ref="partition_a",
        case_ref="case_sql",
        report_text=json.dumps(
            {
                "root_cause": "bearing wear and vibration",
                "corrective_action": "replace bearing",
                "effectiveness_evidence": "vibration inspection passed",
            }
        ),
    )

    hits = kb.search(tenant_ref="partition_a", query="bearing vibration inspection")
    assert hits == [
        {
            "source_id": "perspective_a",
            "tenant_ref": "partition_a",
            "topic": "bearing vibration inspection",
            "kind": "review_perspective",
        }
    ]

    agent = SupplierCorrectiveActionEvidenceBriefAgent(config={"services": services})
    agent.compile()
    result = agent.invoke(
        json.dumps({"mode": "invoke", "tenant_ref": "partition_a", "case_ref": "case_sql"}),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )

    source_ids = {source["source_id"] for source in result["output"]["evidence_sources"]}
    assert "perspective_a" in source_ids
    assert "perspective_a_irrelevant" not in source_ids
    assert "perspective_b" not in source_ids
    connection.close()
