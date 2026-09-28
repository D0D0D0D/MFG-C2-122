# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: S-1 trust gate -> S-4 node_start ->
# S-2 _security_gate_input() -> execute() -> S-3 _security_gate_output() ->
# S-4 node_complete, for every concrete node under src/nodes/.

import importlib
import inspect
import pkgutil

import pytest
from typing import ClassVar
from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    from framework.nodes.base_node import BaseNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError as exc:
        pytest.fail(f"PB-6 cannot import src.nodes; framework/template setup is broken: {exc}")

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


# ── PB-6 S-1 negative boundary（2026-08-17 正本追従で移植）──────────────
# 正本 tests/proof_of_boundary/test_pb_invoke_order.py より。テンプレのノード構成に
# 依存しない常設フィクスチャで「不足 trust では execute() に到達しない」ことを証明する
# （TC-08 の要求。既存の全ノード走査テストは触っていない）。


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


class TestInvokeOrder:
    """PB-6: __call__ must run S-1 -> node_start -> S-2 -> execute() -> S-3 -> node_complete."""

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            if node_cls.__name__ == "SupplierCapaReviewGraphNode":
                import json

                from src.services import wire_services

                services = wire_services()
                services.cases.store_case(
                    tenant_ref="single",
                    case_ref="case_pb6",
                    report_text=json.dumps(
                        {"root_cause": "x", "corrective_action": "y", "effectiveness_evidence": "z"}
                    ),
                )
                instance = node_cls(config={"services": services, "advisory_disclaimer_text": "PB-6 disclaimer"})
            else:
                instance = node_cls()
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
            }
            # Supply a metadata-only envelope to the S-2 node.  The default
            # scaffold state was intentionally too empty once this template
            # made its input gate fail closed; a rejected S-2 path correctly
            # skips execute(), so it cannot demonstrate the full call order.
            if node_cls.__name__ == "PreProcessNode":
                state["user_input"] = '{"mode":"ingest","tenant_ref":"single","ingest_ref":"ingest_1"}'
            elif node_cls.__name__ == "SupplierCapaReviewGraphNode":
                state.update(
                    {
                        "status": "success",
                        "mode": "invoke",
                        "tenant_ref": "single",
                        "case_ref": "case_pb6",
                        "validated_input": '{"mode":"invoke","tenant_ref":"single","case_ref":"case_pb6"}',
                        "session_id": "pb6-session",
                        "trace_id": "pb6-trace",
                        "thread_id": "pb6-thread",
                        "hitl_allowed": False,
                    }
                )
            elif node_cls.__name__ == "Stage1DeterministicCheckNode":
                state["user_input"] = '{"mode":"ingest","tenant_ref":"single","ingest_ref":"ingest_1"}'
            elif node_cls.__name__ in {"Stage2PerspectiveRecallNode", "Stage3CausalAssessmentNode", "Stage4EvidenceVerificationNode"}:
                state.update({"mode": "ingest", "tenant_ref": "single", "ingest_ref": "ingest_1"})
            elif node_cls.__name__ == "SupplierCapaPostProcessNode":
                state.update({"mode": "ingest", "brief_draft": {}})
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            # A GraphNode invokes its inner graph between its own execute and
            # S-3, so its child framework audit events legitimately appear in
            # the shared sink.  Assert the outer ordering as a boundary
            # subsequence while retaining exact order for FunctionNodes.
            if issubclass(node_cls, __import__("framework.nodes.graph_node", fromlist=["GraphNode"]).GraphNode):
                valid = order[:3] == expected[:3] and order[-2:] == expected[-2:]
            else:
                valid = order == expected
            if not valid:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)

    def test_s1_denial_refuses_execution_before_execute(self, monkeypatch):
            """TC-08: an always-present privileged node proves the negative S-1 path."""
            import framework.nodes.base_node as base_node_module

            events: list[str] = []
            execute_calls: list[object] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state: events.append(event_type),
            )
            original_execute = _PrivilegedTrustGateFixture.execute

            def spy_execute(self, state):
                execute_calls.append(state)
                return original_execute(self, state)

            monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
            result = _PrivilegedTrustGateFixture()(
                {
                    "caller_trust_level": _trust_predecessor(
                        _PrivilegedTrustGateFixture.required_trust_level
                    ).value,
                    "correlation_id": "tc08-s1-denial",
                }
            )

            assert result["status"] == "error"
            assert "S-1 trust gate denied" in result["error_log"][0]
            assert events == ["s1_denied"]
            assert not execute_calls
