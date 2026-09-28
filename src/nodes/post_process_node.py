"""S-3 guarded output envelope for evidence-review briefs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
import unicodedata

import yaml

from framework.errors import SecurityViolationError
from framework.schemas.trust_level import TrustLevel
from shared.security import detect_credentials, detect_pii

from src.evidence_gate.gate_node import (
    ProvenanceContract,
    validate_provenance,
)
from src.evidence_gate.strict import StrictCoveragePolicy, find_uncited_numeric_or_comparative_claims
from src.output_envelope.envelope_node import OutputEnvelopeNode


_FORBIDDEN_FIELDS = {"supplier_approval", "shipment_release", "effectiveness_verdict"}
_CAPA_PROVENANCE = ProvenanceContract(
    gate_kind="draft",
    cited_ids_key="cited_source_ids",
    retrieved_records_key="evidence_sources",
    evidence_map_key="claims",
    record_id_keys=("source_id",),
    claim_source_keys=("source_ids",),
)
_CAPA_STRICT_POLICY = StrictCoveragePolicy(
    free_text_paths=(
        ("brief", "inferences"),
        ("gaps",),
        ("causal_leaps",),
        ("verification_questions",),
    ),
    claim_text_keys=("claim_text",),
)


class SupplierCapaPostProcessNode(OutputEnvelopeNode):
    """Format declared mode outputs and fail closed on unsafe conclusions."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": (
            "mode",
            "gate_kind",
            "brief",
            "corrective_requirements",
            "evidence_sources",
            "gaps",
            "causal_leaps",
            "verification_questions",
            "advisory_signal",
            "advisory_disclaimer",
            "evidence_map",
            "requires_human_review",
        ),
        "ingest": ("mode", "gate_kind", "ingest_summary"),
        "feedback": ("mode", "gate_kind", "feedback_summary"),
    }
    gate_kind_by_mode: ClassVar[dict[str, str]] = {"invoke": "draft", "ingest": "ingest", "feedback": "feedback"}

    def __init__(self, *, advisory_disclaimer_text: str | None = None) -> None:
        super().__init__()
        self._advisory_disclaimer_text = advisory_disclaimer_text or _config_disclaimer()

    def format_invoke(self, state: dict) -> dict[str, Any]:
        return {
            "brief": state.get("brief_draft", {}) or {},
            "corrective_requirements": state.get("corrective_requirements", []) or [],
            "evidence_sources": state.get("evidence_sources", []) or [],
            "gaps": state.get("gaps", []) or [],
            "causal_leaps": state.get("causal_leaps", []) or [],
            "verification_questions": state.get("verification_questions", []) or [],
            "advisory_signal": state.get("advisory_signal"),
            "advisory_disclaimer": state.get("advisory_disclaimer"),
            "evidence_map": state.get("evidence_map", {}) or {},
            "requires_human_review": bool(state.get("requires_human_review", False)),
        }

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return {"ingest_summary": state.get("brief_draft", {}) or {}}

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return {"feedback_summary": state.get("brief_draft", {}) or {}}

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        return _sanitize_value(formatted)

    def _extra_security_gate_output(self, result: dict) -> dict:
        result = super()._extra_security_gate_output(result)
        formatted = result["formatted_output"]
        if formatted.get("gate_kind") != "draft":
            return result

        _reject_forbidden_conclusions(formatted, self._advisory_disclaimer_text)
        evidence_map = formatted.get("evidence_map")
        if not isinstance(evidence_map, dict) or not isinstance(evidence_map.get("claims"), list):
            raise SecurityViolationError("S-3 gate: evidence_map.claims must be a list")
        claims = evidence_map["claims"]
        cited_ids = sorted(
            {
                str(source_id)
                for claim in claims
                if isinstance(claim, dict)
                for source_id in claim.get("source_ids", [])
                if isinstance(claim.get("source_ids", []), list)
            }
        )
        validate_provenance(
            {
                "gate_kind": "draft",
                "status": result.get("status"),
                "cited_source_ids": cited_ids,
                "evidence_sources": formatted.get("evidence_sources"),
                "claims": claims,
            },
            _CAPA_PROVENANCE,
        )
        flags = find_uncited_numeric_or_comparative_claims(
            _strict_free_text_projection(formatted), claims, policy=_CAPA_STRICT_POLICY
        )
        if flags:
            raise SecurityViolationError("S-3 gate: uncited numeric/comparative claim detected")
        return result


def _config_disclaimer() -> str:
    config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    value = data.get("advisory_disclaimer_text")
    if not isinstance(value, str) or not value:
        raise RuntimeError("config/config.yaml advisory_disclaimer_text is required")
    return value


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        if detect_credentials(value):
            raise SecurityViolationError("S-3 gate: credential-like value in output")
        findings = detect_pii(value)
        for finding in reversed(findings):
            value = value[: finding["start"]] + "[MASKED]" + value[finding["end"] :]
        return value
    return value


def _reject_forbidden_conclusions(formatted: dict[str, Any], disclaimer: str) -> None:
    signal = formatted.get("advisory_signal")
    if signal not in {"evidence_sufficient", "gaps_present", "inconclusive"}:
        raise SecurityViolationError("S-3 gate: advisory_signal is missing or invalid")
    if formatted.get("advisory_disclaimer") != disclaimer:
        raise SecurityViolationError("S-3 gate: advisory_disclaimer must equal the configured constant")
    for path, _value in _walk_values(formatted):
        final_key = _normalized_guard_text(path.rsplit(".", 1)[-1]).replace("-", "_")
        if final_key in {_normalized_guard_text(field) for field in _FORBIDDEN_FIELDS}:
            raise SecurityViolationError("S-3 gate: forbidden conclusion field")


def _normalized_guard_text(value: str) -> str:
    """Canonicalise case, width, and all Unicode whitespace for S-3 checks."""

    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _strict_free_text_projection(formatted: dict[str, Any]) -> dict[str, Any]:
    """Project prose only before strict coverage scans nested free-text paths.

    Source identifiers are structured provenance, not narrative claims.  Their
    digits must not be mistaken for an uncited quantitative assertion.
    """

    brief = formatted.get("brief", {})
    inferences = brief.get("inferences", []) if isinstance(brief, dict) else []
    return {
        "brief": {"inferences": inferences},
        "gaps": [
            {key: item[key] for key in ("field", "reason", "description") if key in item}
            for item in formatted.get("gaps", [])
            if isinstance(item, dict)
        ],
        "causal_leaps": [
            {key: item[key] for key in ("link", "description") if key in item}
            for item in formatted.get("causal_leaps", [])
            if isinstance(item, dict)
        ],
        "verification_questions": formatted.get("verification_questions", []),
    }


def _walk_values(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            yield from _walk_values(item, next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_values(item, f"{path}[{index}]")
    else:
        yield path, value
