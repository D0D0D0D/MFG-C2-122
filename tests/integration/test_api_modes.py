"""Three-mode adapter integration tests; all persistence is in-memory."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

from src.api import server


def _token_headers(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    return {"authorization": "Bearer mock-entry-token"}


class _VerifiedMiddleware:
    """Test stand-in for the deployment middleware that owns identity stamps."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            state = scope.setdefault("state", {})
            state["trust_level"] = TrustLevel.VERIFIED_EXTERNAL
            state["caller_id"] = "verified-quality-engineer"
        await self._app(scope, receive, send)


def _trusted_app():
    return _VerifiedMiddleware(server.app)


def _clear_store() -> None:
    server.services.cases.cases.clear()
    server.services.cases.ingest_payloads.clear()
    server.services.cases.feedback_payloads.clear()
    server.services.cases.feedback_audit_events.clear()
    server.services.requirements.records.clear()
    server.services.kb.records.clear()


def test_ingest_invoke_feedback_modes_are_isolated_from_raw_state(monkeypatch):
    _clear_store()
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    requirement_batch_sizes: list[int] = []
    perspective_batch_sizes: list[int] = []
    original_requirement_write = server.services.requirements.write_batch
    original_perspective_write = server.services.kb.write_batch

    def requirement_write(records):
        requirement_batch_sizes.append(len(records))
        original_requirement_write(records)

    def perspective_write(records):
        perspective_batch_sizes.append(len(records))
        original_perspective_write(records)

    monkeypatch.setattr(server.services.requirements, "write_batch", requirement_write)
    monkeypatch.setattr(server.services.kb, "write_batch", perspective_write)
    with TestClient(_trusted_app()) as client:
        ingest = client.post(
            "/invoke",
            json={
                "mode": "ingest",
                "input": (
                    "record_type,source_id,text,approved,version\n"
                    "requirement,req_api,根本原因と是正措置と有効性証拠を示す,true,v1\n"
                    "perspective,persp_api,因果接続の証拠を確認する,true,v1\n"
                ),
            },
        )
        assert ingest.status_code == 200
        assert ingest.json()["output"]["ingest_summary"]["corrective_requirements_written"] == 1
        assert requirement_batch_sizes == [1]
        assert perspective_batch_sizes == [1]

        report = {"root_cause": "工具摩耗", "corrective_action": "工具交換", "effectiveness_evidence": "傾向確認"}
        invoke = client.post(
            "/invoke",
            json={"mode": "invoke", "case_ref": "case_api", "input": json.dumps(report)},
        )
        assert invoke.status_code == 200
        output = invoke.json()["output"]
        assert output["advisory_signal"] == "evidence_sufficient"
        assert output["requires_human_review"] is False
        assert json.dumps(report, ensure_ascii=False) not in json.dumps(output, ensure_ascii=False)

        feedback = client.post(
            "/invoke",
            json={"mode": "feedback", "case_ref": "case_api", "input": json.dumps({"human_approved": True, "topics": ["因果接続"]})},
        )
        assert feedback.status_code == 200
        assert feedback.json()["output"]["feedback_summary"]["human_approved"] is True
        assert len(server.services.cases.feedback_audit_events) == 1


def test_invoke_without_an_approved_single_requirement_version_is_inconclusive(monkeypatch):
    _clear_store()
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    with TestClient(_trusted_app()) as client:
        response = client.post(
            "/invoke",
            json={
                "mode": "invoke",
                "case_ref": "case_no_requirement",
                "input": json.dumps({"root_cause": "x", "corrective_action": "y", "effectiveness_evidence": "z"}),
            },
        )
    assert response.status_code == 200
    output = response.json()["output"]
    assert output["advisory_signal"] == "inconclusive"
    assert output["requires_human_review"] is True


def test_adapter_rejects_unauthenticated_and_disguised_pdf_ingest(monkeypatch):
    _clear_store()
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    with TestClient(server.app, raise_server_exceptions=False) as client:
        unauthenticated = client.post("/invoke", json={"mode": "invoke", "input": "{}"})
    assert unauthenticated.status_code == 403

    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    with TestClient(_trusted_app()) as client:
        rejected = client.post(
            "/invoke",
            json={"mode": "ingest", "filename": "records.csv", "content_type": "text/csv", "input": "%PDF-1.7 fake"},
        )
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["rejected"]


def test_feedback_for_an_unknown_case_is_rejected_fail_closed(monkeypatch):
    _clear_store()
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    with TestClient(_trusted_app()) as client:
        response = client.post(
            "/invoke",
            json={"mode": "feedback", "case_ref": "case_missing", "input": json.dumps({"human_approved": True})},
        )
    assert response.status_code == 404


def test_standalone_token_cannot_reach_any_business_state_write(monkeypatch):
    _clear_store()
    headers = _token_headers(monkeypatch)
    store_case_calls: list[str] = []
    original_store_case = server.services.cases.store_case

    def store_case_spy(**kwargs):
        store_case_calls.append(str(kwargs.get("case_ref")))
        return original_store_case(**kwargs)

    monkeypatch.setattr(server.services.cases, "store_case", store_case_spy)
    payloads = (
        {"mode": "invoke", "case_ref": "case_token", "input": "{}"},
        {"mode": "ingest", "input": "record_type,source_id,text\nrequirement,req_token,text"},
        {"mode": "feedback", "case_ref": "case_token", "input": '{"human_approved": true}'},
    )

    with TestClient(server.app) as client:
        responses = [client.post("/invoke", headers=headers, json=payload) for payload in payloads]

    assert [response.status_code for response in responses] == [403, 403, 403]
    assert store_case_calls == []
    assert server.services.cases.cases == {}
    assert server.services.cases.ingest_payloads == {}
    assert server.services.cases.feedback_payloads == {}
    assert server.services.requirements.records == []
    assert server.services.kb.records == []


def test_ingest_batch_validation_failure_commits_neither_target_store(monkeypatch):
    _clear_store()
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    with TestClient(_trusted_app()) as client:
        response = client.post(
            "/invoke",
            json={
                "mode": "ingest",
                "input": (
                    "record_type,source_id,text,approved,version\n"
                    "requirement,req_valid,required,true,v1\n"
                    "perspective,bad id,unsafe second row,true,v1\n"
                ),
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert server.services.requirements.records == []
    assert server.services.kb.records == []


def test_ingest_second_target_failure_rolls_back_both_stores_over_http(monkeypatch):
    _clear_store()
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    original_write = server.services.requirements.write_batch

    def write_then_fail(records):
        original_write(records)
        raise RuntimeError("injected second-target failure")

    monkeypatch.setattr(server.services.requirements, "write_batch", write_then_fail)
    with TestClient(_trusted_app()) as client:
        response = client.post(
            "/invoke",
            json={
                "mode": "ingest",
                "input": (
                    "record_type,source_id,text,approved,version\n"
                    "requirement,req_rollback,required,true,v1\n"
                    "perspective,perspective_rollback,bearing review,true,v1\n"
                ),
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert server.services.requirements.records == []
    assert server.services.kb.records == []
