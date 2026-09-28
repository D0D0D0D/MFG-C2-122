"""Cat 2 GraphNode wrapping the supplier CAPA domain workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.schemas.trust_level import TrustLevel

from src.cat2_graph_skeleton.graph_node import DomainWorkflowGraphNode, ModeSpec
from src.graph.domain_workflow_graph import DomainWorkflowGraph


class SupplierCapaReviewGraphNode(DomainWorkflowGraphNode):
    """Translate outer checkpoint-safe state to the cached inner workflow."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "handle"
    propagate_hitl: ClassVar[bool] = False
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "ingest": ModeSpec(
            "ingest",
            {
                "brief_draft": "ingest_summary",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
        ),
        "feedback": ModeSpec(
            "feedback",
            {
                "brief_draft": "feedback_summary",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
        ),
        "invoke": ModeSpec(
            "draft",
            {
                "brief_draft": "brief_draft",
                "recalled_perspectives": "recalled_perspectives",
                "corrective_requirements": "corrective_requirements",
                "evidence_sources": "evidence_sources",
                "gaps": "gaps",
                "causal_leaps": "causal_leaps",
                "verification_questions": "verification_questions",
                "advisory_signal": "advisory_signal",
                "advisory_disclaimer": "advisory_disclaimer",
                "evidence_map": "evidence_map",
                "requires_human_review": "requires_human_review",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
        ),
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        # HITL and memory are explicitly disabled for this workflow, therefore
        # the cached subgraph must not receive an unused checkpointer.
        super().__init__(config=config, use_inner_checkpointer=False)

    def _build_subgraph(self) -> DomainWorkflowGraph:
        return DomainWorkflowGraph(self._parent_config())

    def _parent_config(self) -> dict[str, Any]:
        forwarded = dict(self._config)
        # This Cat 2 workflow is flag-based human review; it never interrupts.
        forwarded["hitl"] = {"enabled": False}
        forwarded["memory_enabled"] = False
        return forwarded

    def extract_input(self, state: dict) -> str:
        envelope = {"mode": state.get("mode"), "tenant_ref": state.get("tenant_ref")}
        for key in ("case_ref",):
            if state.get(key) is not None:
                envelope[key] = state[key]
        try:
            validated = json.loads(state.get("validated_input", "{}"))
        except (TypeError, json.JSONDecodeError):
            validated = {}
        if isinstance(validated, dict):
            for key in ("ingest_ref", "feedback_ref"):
                if validated.get(key) is not None:
                    envelope[key] = validated[key]
        return json.dumps(envelope, ensure_ascii=False, default=str, sort_keys=True)
