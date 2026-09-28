"""Fixtures consumed by ``work/scripts/mrb_live_llm_check.py``."""

from __future__ import annotations

import json

import pytest

from src.services import wire_services


_LIVE_TENANT = "single_customer_deployment"
_LIVE_CASE_REF = "case-live"


def _supplier_capa() -> dict:
    """CAPA with a weak operator-error RCA and inadequate effectiveness proof."""

    return {
        "root_cause": (
            "The supplier attributes repeated under-torque to operator technique, although "
            "the attached trend shows torque-tool calibration drifting before the defect lots."
        ),
        "corrective_action": (
            "All operators received refresher training; the torque tool and calibration "
            "system were not changed."
        ),
        "effectiveness_evidence": (
            "One lot inspected immediately after training had no under-torque findings; "
            "no multi-lot recurrence window, capability study, MSA, or calibration result "
            "was provided."
        ),
    }


@pytest.fixture
def _live_runtime():
    services = wire_services()
    services.requirements.add_records(
        tenant_ref=_LIVE_TENANT,
        rows=[
            {
                "source_id": "req-root-cause",
                "text": (
                    "The root cause must be evidence-based and distinguish occurrence from "
                    "escape causes."
                ),
                "approved": True,
                "version": "capa-policy-v3",
            },
            {
                "source_id": "req-corrective-action",
                "text": (
                    "Corrective action must address the verified cause, including measurement "
                    "or tool-control weaknesses."
                ),
                "approved": True,
                "version": "capa-policy-v3",
            },
            {
                "source_id": "req-effectiveness",
                "text": (
                    "Effectiveness evidence must cover multiple production lots and include "
                    "recurrence monitoring plus calibration or capability evidence."
                ),
                "approved": True,
                "version": "capa-policy-v3",
            },
        ],
    )
    services.kb.add_projection(
        tenant_ref=_LIVE_TENANT,
        rows=[
            {
                "record_type": "perspective",
                "source_id": "perspective-torque-drift",
                "topic": (
                    "torque tool calibration drift operator retraining MSA capability "
                    "multi-lot effectiveness recurrence"
                ),
            }
        ],
    )
    services.cases.store_case(
        tenant_ref=_LIVE_TENANT,
        case_ref=_LIVE_CASE_REF,
        report_text=json.dumps(_supplier_capa(), ensure_ascii=False),
    )

    return services


@pytest.fixture
def agent_config(_live_runtime) -> dict:
    """Server-equivalent stores; the checker injects ``config['llm']``."""

    return {
        "services": _live_runtime,
        "memory_enabled": False,
        "hitl": {"enabled": False},
    }


@pytest.fixture
def case_payload(_live_runtime) -> dict:
    """Scope-less server envelope referencing the stored CAPA report."""

    return {
        "mode": "invoke",
        "tenant_ref": _LIVE_TENANT,
        "case_ref": _LIVE_CASE_REF,
    }
