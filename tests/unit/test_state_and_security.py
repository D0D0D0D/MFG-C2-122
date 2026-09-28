"""TC-01 through TC-05 and TC-08 safety contracts."""

from __future__ import annotations

import ast
import json
import pkgutil

import pytest

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import SupplierCapaPostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


def _invoke_formatted(**overrides) -> dict:
    payload = {
        "mode": "invoke",
        "gate_kind": "draft",
        "brief": {"inferences": []},
        "corrective_requirements": [],
        "evidence_sources": [{"source_id": "case_1", "kind": "capa_case"}],
        "gaps": [],
        "causal_leaps": [],
        "verification_questions": [],
        "advisory_signal": "evidence_sufficient",
        "advisory_disclaimer": "configured disclaimer",
        "evidence_map": {"claims": [{"claim_id": "c1", "claim_text": "確認済み", "source_ids": ["case_1"]}]},
        "requires_human_review": False,
    }
    payload.update(overrides)
    return payload


def test_tc01_state_contract_is_flat_and_contains_design_fields():
    annotations = State.__annotations__
    assert {
        "mode",
        "tenant_ref",
        "case_ref",
        "validated_input",
        "brief_draft",
        "recalled_perspectives",
        "corrective_requirements",
        "evidence_sources",
        "gaps",
        "causal_leaps",
        "verification_questions",
        "advisory_signal",
        "advisory_disclaimer",
        "evidence_map",
        "requires_human_review",
        "status",
    } <= set(annotations)
    assert "report_text" not in annotations
    assert "credential" not in " ".join(annotations).lower()


def test_tc02_s3_rejects_missing_or_weakened_disclaimer_fail_closed():
    node = SupplierCapaPostProcessNode(advisory_disclaimer_text="configured disclaimer")
    with pytest.raises(SecurityViolationError, match="advisory_disclaimer"):
        node._extra_security_gate_output({"status": AgentStatus.SUCCESS.value, "formatted_output": _invoke_formatted(advisory_disclaimer="参考です")})


@pytest.mark.parametrize(
    "mutation",
    [
        {"supplier_approval": "yes"},
        {"shipment_release": "yes"},
        {"effectiveness_verdict": "yes"},
    ],
)
def test_tc02_s3_rejects_structurally_forbidden_conclusion_fields(mutation):
    node = SupplierCapaPostProcessNode(advisory_disclaimer_text="configured disclaimer")
    with pytest.raises(SecurityViolationError, match="forbidden conclusion"):
        node._extra_security_gate_output({"status": AgentStatus.SUCCESS.value, "formatted_output": _invoke_formatted(**mutation)})


def test_tc02_s3_allows_free_text_that_is_not_a_forbidden_output_field():
    node = SupplierCapaPostProcessNode(advisory_disclaimer_text="configured disclaimer")
    result = node._extra_security_gate_output(
        {
            "status": AgentStatus.SUCCESS.value,
            "formatted_output": _invoke_formatted(brief={"inferences": ["承 認する。ＡＰＰＲＯＶＥＤ ＦＯＲ ＳＨＩＰＭＥＮＴ"]}),
        }
    )

    assert result["formatted_output"]["brief"]["inferences"]


def test_tc02_s3_normalizes_fullwidth_forbidden_field_key():
    node = SupplierCapaPostProcessNode(advisory_disclaimer_text="configured disclaimer")
    with pytest.raises(SecurityViolationError, match="forbidden conclusion field"):
        node._extra_security_gate_output(
            {
                "status": AgentStatus.SUCCESS.value,
                "formatted_output": _invoke_formatted(**{"ｓｕｐｐｌｉｅｒ＿ａｐｐｒｏｖａｌ": "yes"}),
            }
        )


def test_tc02_s3_does_not_treat_the_fixed_advisory_disclaimer_as_a_conclusion():
    disclaimer = "本シグナルは参考であり、供給者承認・製品出荷・是正措置の有効性の最終判定ではない。最終判定はQA 責任者が行う。"
    node = SupplierCapaPostProcessNode(advisory_disclaimer_text=disclaimer)

    result = node._extra_security_gate_output(
        {"status": AgentStatus.SUCCESS.value, "formatted_output": _invoke_formatted(advisory_disclaimer=disclaimer)}
    )

    assert result["formatted_output"]["advisory_disclaimer"] == disclaimer


def test_tc03_src_contains_no_credential_shaped_literal():
    import pathlib
    import re

    credential = re.compile(r"(?:sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|Bearer\s+[A-Za-z0-9._-]{20,})")
    offenders = []
    for path in pathlib.Path("src").rglob("*.py"):
        if credential.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert offenders == []


def test_tc04_nodes_do_not_construct_or_store_invocation_context():
    import pathlib

    offenders = []
    for path in pathlib.Path("src/nodes").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(node, ast.Name) and node.id == "InvocationContext" for node in ast.walk(tree)):
            offenders.append(str(path))
    assert offenders == []


def test_tc05_preprocess_emits_domain_audit_event(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr("shared.utils.audit_logger.emit_trace_event", lambda event, _payload, _state: events.append(event))
    node = PreProcessNode()
    result = node.execute({"user_input": json.dumps({"mode": "ingest", "tenant_ref": "single", "ingest_ref": "ingest_1"})})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert events == ["capa_input_minimized"]


def test_tc05_s2_blocks_raw_report_even_when_metadata_otherwise_looks_valid():
    node = PreProcessNode()
    state = node._extra_security_gate_input(
        {
            "user_input": json.dumps(
                {"mode": "invoke", "tenant_ref": "single", "case_ref": "case_1", "report_text": "not permitted"}
            )
        }
    )
    assert state["status"] == AgentStatus.ERROR.value
    assert "prohibited" in state["error_log"][0]


def test_tc08_every_agent_node_explicitly_declares_allowed_trust_level():
    import src.nodes as node_package

    allowed = {TrustLevel.ANONYMOUS, TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL}
    discovered: list[type] = []
    for _, module_name, _ in pkgutil.walk_packages(node_package.__path__, prefix="src.nodes."):
        module = __import__(module_name, fromlist=["*"])
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and value.__module__ == module_name
                and issubclass(value, (FunctionNode, GraphNode))
                and value not in {FunctionNode, GraphNode}
            ):
                discovered.append(value)
    assert discovered
    assert all("required_trust_level" in node.__dict__ and node.required_trust_level in allowed for node in discovered)


def test_s3_sanitizes_pii_but_does_not_relax_provenance_contract():
    node = SupplierCapaPostProcessNode(advisory_disclaimer_text="configured disclaimer")
    result = node._extra_security_gate_output(
        {
            "status": AgentStatus.SUCCESS.value,
            "formatted_output": _invoke_formatted(corrective_requirements=[{"source_id": "case_1", "text": "contact a@example.com"}]),
        }
    )
    assert "a@example.com" not in str(result["formatted_output"])

    with pytest.raises(SecurityViolationError, match="non-existent"):
        node._extra_security_gate_output(
            {
                "status": AgentStatus.SUCCESS.value,
                "formatted_output": _invoke_formatted(
                    evidence_map={"claims": [{"claim_id": "c1", "claim_text": "確認済み", "source_ids": ["unknown"]}]}
                ),
            }
        )
