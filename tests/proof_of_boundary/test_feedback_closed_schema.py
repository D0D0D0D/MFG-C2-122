"""feedback が閉じたスキーマであることと、manifest 宣言の整合を固定する。

由来: 2026-08-13 の独立レビュー（Batch 3）。レビュアーの再現 probe をそのまま回帰テスト化。

H6: `/invoke` の feedback mode が**任意の dict をそのまま保存**し、`record_feedback()` も
    `human_approved` 以外を型検証しない。結果、**未宣言キーと nested 値が
    永続 store・監査イベント・顧客応答のすべてに残る**。

Medium: `config/agent.yaml` の L1 base 宣言が実装と食い違う。
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.trust_level import TrustLevel

from src.api import server

MARKER = "EXTERNAL_RAW_MARKER"


class _VerifiedMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            state = scope.setdefault("state", {})
            state["trust_level"] = TrustLevel.VERIFIED_EXTERNAL
            state["caller_id"] = "verified-quality-engineer"
        await self.app(scope, receive, send)


def test_feedback_rejects_undeclared_and_nested_values():
    """未宣言キー・nested 値を含む feedback が拒否され、どこにも残らないこと。"""
    server.services.cases.cases.clear()
    server.services.cases.feedback_payloads.clear()
    server.services.cases.feedback_audit_events.clear()
    tenant_ref = server._runtime_config["deployment_tenant_ref"]
    server.services.cases.store_case(tenant_ref=tenant_ref,
                                     report_text="A stored supplier CAPA report.",
                                     case_ref="case_h6")

    feedback = {"human_approved": True,
                "topics": [{"raw": MARKER}],          # nested
                "undeclared": {"nested": MARKER}}     # 未宣言キー
    with TestClient(_VerifiedMiddleware(server.app)) as client:
        response = client.post("/invoke", json={
            "mode": "feedback", "case_ref": "case_h6", "input": json.dumps(feedback)})

    assert response.status_code >= 400, (
        f"未宣言キー・nested 値を含む feedback が受理された: HTTP {response.status_code}"
    )
    assert not server.services.cases.feedback_payloads, "拒否されるべき feedback が永続化された"
    assert not server.services.cases.feedback_audit_events, "拒否されるべき feedback が監査に残った"
    assert MARKER not in response.text, "外部由来の値が応答に返った"


def test_a_valid_feedback_still_succeeds():
    """宣言済みキーだけの feedback は通ること（過剰拒否の防止）。"""
    server.services.cases.cases.clear()
    server.services.cases.feedback_payloads.clear()
    server.services.cases.feedback_audit_events.clear()
    tenant_ref = server._runtime_config["deployment_tenant_ref"]
    server.services.cases.store_case(tenant_ref=tenant_ref,
                                     report_text="A stored supplier CAPA report.",
                                     case_ref="case_ok")
    with TestClient(_VerifiedMiddleware(server.app)) as client:
        response = client.post("/invoke", json={
            "mode": "feedback", "case_ref": "case_ok",
            "input": json.dumps({"human_approved": True})})
    assert response.status_code == 200, f"正当な feedback が落ちた: {response.status_code}"


def test_manifest_declares_the_actual_l1_base():
    """manifest の L1 base 宣言が実装と一致すること。"""
    from src.graph.graph import SupplierCorrectiveActionEvidenceBriefAgent as Agent

    manifest = yaml.safe_load(Path("config/agent.yaml").read_text())
    declared = manifest["base_type"]
    actual = Agent.__bases__[0].__name__
    assert Agent.__bases__[0] is AgentBaseGraph, "L1 直継承でなくなっている"
    assert declared == actual, f"manifest.base_type={declared} だが実装は {actual}"
