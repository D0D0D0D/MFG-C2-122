"""Inner fixed stages for the supplier CAPA domain workflow."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services import CapaServices, analyze_causal_chain, parse_report_fields, wire_services


class Stage1DeterministicCheckNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, services: CapaServices | None = None) -> None:
        super().__init__()
        self._services = services or wire_services()

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        try:
            envelope = json.loads(state.get("user_input", "{}"))
            mode = envelope["mode"]
            tenant_ref = envelope["tenant_ref"]
            if mode == "invoke":
                fields, readable = parse_report_fields(
                    self._services.cases.case_report(tenant_ref=tenant_ref, case_ref=envelope["case_ref"])
                )
                gaps = [
                    {"field": field, "reason": "required_field_missing"}
                    for field in ("root_cause", "corrective_action", "effectiveness_evidence")
                    if not fields.get(field)
                ]
                result = {
                    "mode": mode,
                    "tenant_ref": tenant_ref,
                    "case_ref": envelope["case_ref"],
                    "fields_present": {field: bool(value) for field, value in fields.items()},
                    "format_check": {"readable": readable},
                    "gaps": gaps,
                }
            elif mode == "ingest":
                result = {"mode": mode, "tenant_ref": tenant_ref, "ingest_ref": envelope["ingest_ref"]}
            elif mode == "feedback":
                result = {
                    "mode": mode,
                    "tenant_ref": tenant_ref,
                    "case_ref": envelope["case_ref"],
                    "feedback_ref": envelope["feedback_ref"],
                }
            else:
                raise ValueError("unknown mode")
        except (KeyError, TypeError, ValueError, LookupError, json.JSONDecodeError) as exc:
            emit_trace_event("capa_stage1_rejected", {"reason": str(exc)}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"Stage1: {exc}"]}
        emit_trace_event("capa_stage1_checked", {"mode": result["mode"]}, state)
        return {**result, "status": AgentStatus.SUCCESS.value}


class Stage2PerspectiveRecallNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, services: CapaServices | None = None) -> None:
        super().__init__()
        self._services = services or wire_services()

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        mode = state.get("mode")
        try:
            if mode == "ingest":
                result = {"ingest_summary": self._services.apply_ingest(tenant_ref=state["tenant_ref"], ingest_ref=state["ingest_ref"])}
            elif mode == "invoke":
                context = self._services.review_context(tenant_ref=state["tenant_ref"], case_ref=state["case_ref"])
                result = {
                    "corrective_requirements": context["requirements"],
                    "recalled_perspectives": context["perspectives"],
                    "requirement_matchable": context["requirement_matchable"],
                }
            elif mode == "feedback":
                result = {}
            else:
                raise ValueError("unknown mode")
        except (KeyError, LookupError, ValueError) as exc:
            emit_trace_event("capa_stage2_rejected", {"reason": str(exc)}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"Stage2: {exc}"]}
        emit_trace_event("capa_stage2_completed", {"mode": mode}, state)
        return {**result, "status": AgentStatus.SUCCESS.value}


class Stage3CausalAssessmentNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, services: CapaServices | None = None, llm: Any | None = None) -> None:
        super().__init__()
        self._services = services or wire_services()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        mode = state.get("mode")
        try:
            if mode == "invoke":
                review = self._services.review_context(tenant_ref=state["tenant_ref"], case_ref=state["case_ref"])
                result = analyze_causal_chain(
                    report_text=review["report_text"],
                    fields_present=state.get("fields_present", {}),
                    requirements=state.get("corrective_requirements", []),
                    case_ref=state["case_ref"],
                    llm=self._llm,
                )
                case_source = {"source_id": f"case_{state['case_ref']}", "kind": "capa_case"}
                result["evidence_sources"] = [case_source, *state.get("corrective_requirements", []), *state.get("recalled_perspectives", [])]
            elif mode == "feedback":
                result = {"feedback_summary": self._services.record_feedback(tenant_ref=state["tenant_ref"], feedback_ref=state["feedback_ref"])}
            elif mode == "ingest":
                result = {}
            else:
                raise ValueError("unknown mode")
        except (KeyError, LookupError, ValueError) as exc:
            emit_trace_event("capa_stage3_rejected", {"reason": str(exc)}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"Stage3: {exc}"]}
        emit_trace_event("capa_stage3_assessed", {"mode": mode, "llm_used": self._llm is not None}, state)
        return {**result, "status": AgentStatus.SUCCESS.value}


class Stage4EvidenceVerificationNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, advisory_disclaimer_text: str = "") -> None:
        super().__init__()
        self._advisory_disclaimer_text = advisory_disclaimer_text

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        if state.get("mode") != "invoke":
            emit_trace_event("capa_stage4_completed", {"mode": state.get("mode")}, state)
            return {"status": AgentStatus.SUCCESS.value}
        gaps = list(state.get("gaps", []))
        if not state.get("requirement_matchable", False):
            gaps.append({"field": "corrective_requirements", "reason": "approved_single_version_unavailable"})
        causal_leaps = list(state.get("causal_leaps", []))
        confidence = state.get("analysis_confidence", 1.0)
        readable = bool(state.get("format_check", {}).get("readable", False))
        inconclusive = not readable or not state.get("requirement_matchable", False) or not isinstance(confidence, (int, float)) or confidence < 0.6
        if inconclusive:
            signal = "inconclusive"
        elif gaps or causal_leaps:
            signal = "gaps_present"
        else:
            signal = "evidence_sufficient"
        questions = _questions_for(gaps, causal_leaps, inconclusive)
        claims = state.get("brief_draft", {}).get("claims", []) if isinstance(state.get("brief_draft"), dict) else []
        evidence_map = {"claims": claims}
        requires_review = signal != "evidence_sufficient"
        emit_trace_event("capa_stage4_verified", {"advisory_signal": signal, "requires_human_review": requires_review}, state)
        return {
            "gaps": gaps,
            "causal_leaps": causal_leaps,
            "verification_questions": questions,
            "advisory_signal": signal,
            "advisory_disclaimer": self._advisory_disclaimer_text,
            "evidence_map": evidence_map,
            "requires_human_review": requires_review,
            "status": AgentStatus.SUCCESS.value,
        }


def _questions_for(gaps: list[dict], causal_leaps: list[dict], inconclusive: bool) -> list[str]:
    questions: list[str] = []
    for gap in gaps:
        field = gap.get("field", "required item") if isinstance(gap, dict) else "required item"
        questions.append(f"{field} を裏付ける提出情報を確認してください。")
    for leap in causal_leaps:
        link = leap.get("link", "causal link") if isinstance(leap, dict) else "causal link"
        questions.append(f"{link} の因果接続を示す証拠を確認してください。")
    if inconclusive:
        questions.append("承認済み是正要求の照合対象版と提出資料の可読性を確認してください。")
    return list(dict.fromkeys(questions))
