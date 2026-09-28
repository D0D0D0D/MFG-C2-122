"""Inner four-stage workflow for a supplier CAPA evidence brief."""

from __future__ import annotations

from typing import Any, NotRequired

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.nodes.domain_stage_nodes import (
    Stage1DeterministicCheckNode,
    Stage2PerspectiveRecallNode,
    Stage3CausalAssessmentNode,
    Stage4EvidenceVerificationNode,
)
from src.services import CapaServices, wire_services


class DomainWorkflowState(AgentState, total=False):
    mode: str
    tenant_ref: str
    case_ref: str
    ingest_ref: str
    feedback_ref: str
    fields_present: NotRequired[dict[str, bool]]
    format_check: NotRequired[dict[str, bool]]
    gaps: NotRequired[list[dict]]
    recalled_perspectives: NotRequired[list[dict]]
    corrective_requirements: NotRequired[list[dict]]
    requirement_matchable: NotRequired[bool]
    brief_draft: NotRequired[dict]
    causal_leaps: NotRequired[list[dict]]
    evidence_sources: NotRequired[list[dict]]
    analysis_confidence: NotRequired[float]
    advisory_signal: NotRequired[str]
    advisory_disclaimer: NotRequired[str]
    evidence_map: NotRequired[dict]
    verification_questions: NotRequired[list[str]]
    requires_human_review: NotRequired[bool]
    ingest_summary: NotRequired[dict]
    feedback_summary: NotRequired[dict]


class DomainWorkflowGraph(BaseGraph):
    """Fixed Stage 1→4 workflow; raw CAPA text never leaves service scope."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        services = self.config.get("services")
        self._services: CapaServices = services if isinstance(services, CapaServices) else wire_services()

    @property
    def name(self) -> str:
        return "supplier_capa_domain_workflow"

    @property
    def state_schema(self) -> type:
        return DomainWorkflowState

    def _validate_config(self) -> None:
        disclaimer = self.config.get("advisory_disclaimer_text")
        if not isinstance(disclaimer, str) or not disclaimer:
            raise ValueError("advisory_disclaimer_text is required for the domain workflow")

    def register_nodes(self) -> None:
        self._nodes["stage1"] = Stage1DeterministicCheckNode(self._services)
        self._nodes["stage2"] = Stage2PerspectiveRecallNode(self._services)
        self._nodes["stage3"] = Stage3CausalAssessmentNode(self._services, self.config.get("llm"))
        self._nodes["stage4"] = Stage4EvidenceVerificationNode(self.config["advisory_disclaimer_text"])

    def add_edges(self) -> None:
        self._sg.add_edge(START, "stage1")
        self._sg.add_edge("stage1", "stage2")
        self._sg.add_edge("stage2", "stage3")
        self._sg.add_edge("stage3", "stage4")
        self._sg.add_edge("stage4", END)

    def route(self, state: dict) -> str:
        return "stage1"

    def get_output(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            error_result = {"status": AgentStatus.ERROR.value, "error_log": state.get("error_log", [])}
            for key in ("error_code", "error_type"):
                if state.get(key):
                    error_result[key] = state[key]
            return error_result
        mode = state.get("mode")
        result: dict[str, Any] = {"mode": mode, "status": state.get("status", AgentStatus.SUCCESS.value)}
        if mode == "ingest":
            result["ingest_summary"] = state.get("ingest_summary", {})
        elif mode == "feedback":
            result["feedback_summary"] = state.get("feedback_summary", {})
        elif mode == "invoke":
            for key in (
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
            ):
                if key in state:
                    result[key] = state[key]
        else:
            return {"status": AgentStatus.ERROR.value, "error_log": ["DomainWorkflowGraph: unknown mode"]}
        return result
