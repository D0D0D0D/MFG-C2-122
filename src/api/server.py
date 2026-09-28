"""FastAPI adapter for ingest, invoke, and feedback modes."""

from __future__ import annotations

import json
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory

from src.entry_adapter import AuthContext, Operation, authenticate
from src.graph.graph import SupplierCorrectiveActionEvidenceBriefAgent, load_runtime_config
from src.ingest_file_sanitizer.file_sanitizer import sanitize_file_bytes
from src.services import CapaServices, wire_services


app = FastAPI(title="Manufacturing Supplier Corrective-Action Evidence Brief Agent")
_runtime_config = load_runtime_config()
services: CapaServices = wire_services()
agent = SupplierCorrectiveActionEvidenceBriefAgent(config={**_runtime_config, "services": services})
agent.compile()
agent.provision_secrets(
    secrets_factory(namespace="mfg", agent_name="supplier-corrective-action-evidence-brief")
)


class InvokeRequest(BaseModel):
    mode: Literal["ingest", "invoke", "feedback"] = "invoke"
    input: str
    session_id: str = ""
    case_ref: str = ""
    filename: str = "ingest.csv"
    content_type: str = "text/csv"


_OPERATIONS_BY_MODE = {
    # invoke stores the supplied CAPA as a durable case before analysis, so it
    # is a write even though the route returns a review brief.
    "invoke": Operation.WRITE_INGEST,
    "ingest": Operation.WRITE_INGEST,
    "feedback": Operation.WRITE_FEEDBACK,
}


def _ctx(request: Request, session_id: str, *, operation: Operation) -> tuple[InvocationContext, AuthContext]:
    """Build verified context for one customer deployment without scope keys.

    This template is intentionally single-customer-per-deployment.  There is
    no customer-internal KB partition below the deployment boundary,
    so entry_adapter declares ``SCOPE_KEYS = ()``.  The public operation-aware
    API still enforces that every path which persists business state requires
    a verified middleware identity; a standalone entry token is read-only.
    """

    auth = authenticate(request, operation=operation)
    ctx = InvocationContext(
        session_id=session_id or str(uuid4()),
        caller_trust_level=auth.trust,
        caller_id=auth.principal["caller_id"],
    )
    return ctx, auth


def _safe_envelope(mode: str, **references: str) -> str:
    payload = {
        "mode": mode,
        "tenant_ref": _runtime_config["deployment_tenant_ref"],
        **{key: value for key, value in references.items() if value},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    ctx, auth = _ctx(request, req.session_id, operation=_OPERATIONS_BY_MODE[req.mode])
    if auth.scope:
        raise RuntimeError("scope-less deployment received an unexpected partition scope")
    tenant_ref = _runtime_config["deployment_tenant_ref"]
    try:
        if req.mode == "invoke":
            case_ref = services.cases.store_case(
                tenant_ref=tenant_ref,
                report_text=req.input,
                case_ref=req.case_ref or None,
            )
            envelope = _safe_envelope("invoke", case_ref=case_ref)
        elif req.mode == "ingest":
            sanitized = sanitize_file_bytes(
                req.input.encode("utf-8"),
                declared_name=req.filename,
                declared_content_type=req.content_type,
                allowed_channels=("csv",),
            )
            if not sanitized.accepted:
                raise HTTPException(status_code=400, detail={"rejected": sanitized.rejected})
            ingest_ref = services.cases.store_ingest_payload(tenant_ref=tenant_ref, rows=sanitized.sanitized_rows)
            envelope = _safe_envelope("ingest", ingest_ref=ingest_ref)
        else:
            if not req.case_ref:
                raise HTTPException(status_code=422, detail="feedback mode requires case_ref")
            if not services.cases.has_case(tenant_ref=tenant_ref, case_ref=req.case_ref):
                raise HTTPException(status_code=404, detail="case_ref is not available for this deployment")
            try:
                feedback = json.loads(req.input)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=422, detail="feedback input must be a JSON object") from exc
            if not isinstance(feedback, dict):
                raise HTTPException(status_code=422, detail="feedback input must be a JSON object")
            feedback_ref = services.cases.store_feedback_payload(
                tenant_ref=tenant_ref,
                feedback=feedback,
                case_ref=req.case_ref,
            )
            envelope = _safe_envelope("feedback", case_ref=req.case_ref, feedback_ref=feedback_ref)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with bound_secrets(agent._secrets_provider):
        return agent.invoke(envelope, ctx=ctx)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "supplier-corrective-action-evidence-brief"}
