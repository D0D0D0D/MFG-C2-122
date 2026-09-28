"""Checkpoint-safe state contract for MFG-C2-122.

Raw supplier CAPA/8D text is deliberately absent.  It is stored in the case
record service and is fetched only for the duration of an inner workflow stage.
"""

from __future__ import annotations

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    """Flat, JSON-serializable outer workflow state (docs/02_design.md)."""

    mode: str
    tenant_ref: str
    case_ref: str
    validated_input: str
    brief_draft: dict
    recalled_perspectives: list[dict]
    corrective_requirements: list[dict]
    evidence_sources: list[dict]
    gaps: list[dict]
    causal_leaps: list[dict]
    verification_questions: list[str]
    advisory_signal: str
    advisory_disclaimer: str
    evidence_map: dict
    requires_human_review: bool
    error_code: str
    error_type: str
    status: str
