"""TC-01 / TC-08 and Cat 2 main-node contracts."""

from __future__ import annotations

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.nodes.main_node import SupplierCapaReviewGraphNode
from src.graph.graph import SupplierCorrectiveActionEvidenceBriefAgent
from src.services import wire_services


def _services():
    services = wire_services()
    services.requirements.add_records(
        tenant_ref="single_customer_deployment",
        rows=[{"source_id": "req_1", "text": "根本原因と有効性証拠を提示すること", "approved": "true", "version": "v1"}],
    )
    services.cases.store_case(
        tenant_ref="single_customer_deployment",
        case_ref="case_1",
        report_text=json.dumps(
            {"root_cause": "設備摩耗", "corrective_action": "部品を交換", "effectiveness_evidence": "傾向を確認"}
        ),
    )
    return services


def _state() -> dict:
    return {
        "mode": "invoke",
        "tenant_ref": "single_customer_deployment",
        "case_ref": "case_1",
        "validated_input": json.dumps(
            {"mode": "invoke", "tenant_ref": "single_customer_deployment", "case_ref": "case_1"}
        ),
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "session_id": "session",
        "correlation_id": "correlation",
        "trace_id": "trace",
        "thread_id": "thread",
        "hitl_allowed": False,
        "status": AgentStatus.SUCCESS.value,
    }


class TestSupplierCapaReviewGraphNode:
    def test_main_node_runs_cached_inner_workflow_and_returns_changed_keys_only(self):
        node = SupplierCapaReviewGraphNode(config={"services": _services(), "advisory_disclaimer_text": "disclaimer"})

        result = node.execute(_state())

        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["advisory_signal"] == "evidence_sufficient"
        assert result["advisory_disclaimer"] == "disclaimer"
        assert "node_history" not in result
        assert "user_input" not in result

    def test_main_node_passes_upstream_error_without_starting_subgraph(self):
        node = SupplierCapaReviewGraphNode(config={"advisory_disclaimer_text": "disclaimer"})

        assert node.execute({"status": AgentStatus.ERROR.value}) == {}
        assert node._subgraph is None

    def test_main_node_disables_hitl_and_inner_checkpointer(self):
        node = SupplierCapaReviewGraphNode(config={"advisory_disclaimer_text": "disclaimer"})

        assert node.propagate_hitl is False
        assert node._parent_config()["hitl"] == {"enabled": False}
        assert node._parent_config()["memory_enabled"] is False
        assert node._use_inner_checkpointer is False

    def test_main_node_declares_verified_external_trust(self):
        assert SupplierCapaReviewGraphNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_missing_required_8d_fields_produce_gaps_present_when_matching_is_available(self):
        services = wire_services()
        services.requirements.add_records(
            tenant_ref="single_customer_deployment",
            rows=[{"source_id": "req_gap", "text": "required", "approved": True, "version": "v1"}],
        )
        services.cases.store_case(
            tenant_ref="single_customer_deployment",
            case_ref="case_42",
            report_text=json.dumps({"root_cause": "摩耗"}),
        )
        agent = SupplierCorrectiveActionEvidenceBriefAgent(config={"services": services})
        agent.compile()

        result = agent.invoke(
            json.dumps({"mode": "invoke", "tenant_ref": "single_customer_deployment", "case_ref": "case_42"}),
            ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )

        assert result["output"]["advisory_signal"] == "gaps_present"
        assert result["output"]["gaps"]

    def test_inconclusive_has_priority_over_gaps_when_source_is_unreadable(self):
        services = wire_services()
        services.requirements.add_records(
            tenant_ref="single_customer_deployment",
            rows=[{"source_id": "req_unreadable", "text": "required", "approved": True, "version": "v1"}],
        )
        services.cases.store_case(
            tenant_ref="single_customer_deployment", case_ref="case_unreadable", report_text="unstructured CAPA text"
        )
        agent = SupplierCorrectiveActionEvidenceBriefAgent(config={"services": services})
        agent.compile()

        result = agent.invoke(
            json.dumps({"mode": "invoke", "tenant_ref": "single_customer_deployment", "case_ref": "case_unreadable"}),
            ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        )

        assert result["output"]["advisory_signal"] == "inconclusive"
