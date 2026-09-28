"""Outer S-2 validation and input minimisation."""

from __future__ import annotations

import json
import re
from typing import ClassVar

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.nodes.function_node import FunctionNode


_SAFE_REF = re.compile(r"^[0-9A-Za-z_-]+$")
_ALLOWED_MODES = {"ingest", "invoke", "feedback"}
_FORBIDDEN_INPUT_KEYS = {
    "capa_report",
    "report_text",
    "raw_report",
    "credential",
    "token",
    "password",
    "api_key",
}


class PreProcessNode(FunctionNode):
    """Validate safe entry envelopes before any subgraph receives them."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        from shared.utils.audit_logger import emit_trace_event

        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        try:
            envelope = _parse_envelope(state.get("user_input", ""))
            mode = envelope["mode"]
            tenant_ref = envelope["tenant_ref"]
            safe = {"mode": mode, "tenant_ref": tenant_ref}
            if mode in {"invoke", "feedback"}:
                safe["case_ref"] = _require_ref(envelope.get("case_ref"), "case_ref")
            if mode == "ingest":
                safe["ingest_ref"] = _require_ref(envelope.get("ingest_ref"), "ingest_ref")
            if mode == "feedback":
                safe["feedback_ref"] = _require_ref(envelope.get("feedback_ref"), "feedback_ref")
        except ValueError as exc:
            emit_trace_event("capa_input_rejected", {"reason": str(exc)}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": [f"PreProcessNode: {exc}"]}

        emit_trace_event("capa_input_minimized", {"mode": mode, "case_ref": safe.get("case_ref", "")}, state)
        delta = {
            "mode": mode,
            "tenant_ref": tenant_ref,
            "validated_input": json.dumps(safe, ensure_ascii=False, sort_keys=True),
            "status": AgentStatus.SUCCESS.value,
        }
        if "case_ref" in safe:
            delta["case_ref"] = safe["case_ref"]
        return delta

    def _extra_security_gate_input(self, state: dict) -> dict:
        """Fail closed when a caller bypasses the adapter with report material."""

        raw = state.get("user_input", "")
        if not isinstance(raw, str):
            return _s2_error(state, "user_input must be a JSON metadata envelope")
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return _s2_error(state, "raw or non-JSON input is not accepted by the graph")
        if not isinstance(envelope, dict):
            return _s2_error(state, "input envelope must be an object")
        forbidden = _FORBIDDEN_INPUT_KEYS & set(envelope)
        if forbidden:
            return _s2_error(state, f"input envelope contains prohibited field(s): {sorted(forbidden)}")
        return state


def _parse_envelope(raw: object) -> dict[str, str]:
    if not isinstance(raw, str):
        raise ValueError("user_input must be a JSON metadata envelope")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("raw or malformed input cannot enter checkpointed state") from exc
    if not isinstance(value, dict):
        raise ValueError("input envelope must be an object")
    forbidden = _FORBIDDEN_INPUT_KEYS & set(value)
    if forbidden:
        raise ValueError(f"input envelope contains prohibited field(s): {sorted(forbidden)}")
    mode = value.get("mode")
    tenant_ref = value.get("tenant_ref")
    if mode not in _ALLOWED_MODES:
        raise ValueError("mode must be one of ingest, invoke, feedback")
    if not isinstance(tenant_ref, str) or not tenant_ref.strip() or tenant_ref != tenant_ref.strip():
        raise ValueError("tenant_ref is unresolved")
    return value


def _require_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_REF.fullmatch(value):
        raise ValueError(f"{label} must use only [0-9A-Za-z_-]")
    return value


def _s2_error(state: dict, message: str) -> dict:
    result = dict(state)
    result["status"] = AgentStatus.ERROR.value
    result["error_log"] = [f"PreProcessNode S-2: {message}"]
    return result
