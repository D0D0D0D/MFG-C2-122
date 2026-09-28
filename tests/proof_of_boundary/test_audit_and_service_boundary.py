"""PB-1 and PB-3: audit and service seams at the framework boundary."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import SupplierCorrectiveActionEvidenceBriefAgent
from src.nodes.domain_stage_nodes import Stage1DeterministicCheckNode
from src.services import wire_services


def test_pb1_domain_stage_emits_audit_event(monkeypatch):
    services = wire_services()
    services.cases.store_case(
        tenant_ref="single_customer_deployment",
        case_ref="case_audit",
        report_text=json.dumps({"root_cause": "x", "corrective_action": "y", "effectiveness_evidence": "z"}),
    )
    events: list[str] = []
    monkeypatch.setattr("shared.utils.audit_logger.emit_trace_event", lambda event, _payload, _state: events.append(event))

    result = Stage1DeterministicCheckNode(services).execute(
        {"user_input": json.dumps({"mode": "invoke", "tenant_ref": "single_customer_deployment", "case_ref": "case_audit"})}
    )

    assert result["status"] == "success"
    assert events == ["capa_stage1_checked"]


def test_pb3_llm_is_an_injected_service_seam_and_raw_report_does_not_leave_it():
    received: list[dict] = []

    def fake_llm(prompt: dict):
        received.append(prompt)
        return {"causal_leaps": ["contradictory_evidence"], "confidence": 0.5}

    services = wire_services()
    services.requirements.add_records(
        tenant_ref="single_customer_deployment",
        rows=[{"source_id": "req_pb", "text": "approved requirement", "approved": True, "version": "v1"}],
    )
    raw_report = json.dumps({"root_cause": "private cause", "corrective_action": "action", "effectiveness_evidence": "proof"})
    services.cases.store_case(tenant_ref="single_customer_deployment", case_ref="case_pb", report_text=raw_report)
    agent = SupplierCorrectiveActionEvidenceBriefAgent(config={"services": services, "llm": fake_llm})
    agent.compile()

    result = agent.invoke(
        json.dumps({"mode": "invoke", "tenant_ref": "single_customer_deployment", "case_ref": "case_pb"}),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )

    assert received and received[0]["report_text"] == raw_report
    assert result["output"]["advisory_signal"] == "inconclusive"
    assert raw_report not in json.dumps(result, ensure_ascii=False)
