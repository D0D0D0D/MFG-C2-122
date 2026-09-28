"""Unit contracts for the AGENTIC STAR Marketplace entry adapter."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets
from src.graph.graph import SupplierCorrectiveActionEvidenceBriefAgent


@pytest.fixture(autouse=True)
def azure_environment(monkeypatch):
    values = {
        "AZURE_OPENAI_API_KEY": "key-a",
        "AZURE_OPENAI_ENDPOINT": "https://azure.example.invalid",
        "AZURE_OPENAI_DEPLOYMENT": "deployment-a",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _capture_parent_invoke(monkeypatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "success"}

    monkeypatch.setattr(SupplierCorrectiveActionEvidenceBriefAgent, "invoke", fake_invoke)
    return captured


def test_invoke_discards_runner_context_forces_trust_and_suppresses_hitl(monkeypatch):
    captured = _capture_parent_invoke(monkeypatch)
    agent = cli.MarketplaceGraph(config={})
    runner_ctx = InvocationContext(
        caller_id="marketplace-caller",
        caller_trust_level=TrustLevel.INTERNAL,
        hitl_allowed=True,
    )
    report = json.dumps(
        {
            "mode": "feedback",
            "root_cause": "bearing wear",
            "corrective_action": "replace bearing",
            "effectiveness_evidence": "trend check",
        }
    )

    result = agent.invoke(
        report,
        session_id="market-session",
        ctx=runner_ctx,
        input_context={
            "conversation_history": ["unscanned text"],
            "tenant_ref": "attacker-tenant",
            "mode": "feedback",
        },
    )

    assert result == {"status": "success"}
    assert captured["input_context"] == {}
    envelope = json.loads(captured["user_input"])
    assert envelope == {
        "mode": "invoke",
        "tenant_ref": "single_customer_deployment",
        "case_ref": envelope["case_ref"],
    }
    assert report not in captured["user_input"]
    assert (
        agent.config["services"].cases.case_report(
            tenant_ref="single_customer_deployment",
            case_ref=envelope["case_ref"],
        )
        == report
    )
    assert captured["ctx"].caller_id == "marketplace-caller"
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False


def test_scope_is_built_data_driven_from_entry_adapter_declarations(monkeypatch):
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("supplier_id", "capa_program_id"))
    monkeypatch.setattr(
        cli,
        "STG_DEFAULT_SCOPE_ENVS",
        {
            "supplier_id": "STG_DEFAULT_SUPPLIER_ID",
            "capa_program_id": "STG_DEFAULT_CAPA_PROGRAM_ID",
        },
    )
    monkeypatch.setenv("STG_DEFAULT_SUPPLIER_ID", "supplier-a")
    monkeypatch.setenv("STG_DEFAULT_CAPA_PROGRAM_ID", "program-a")
    captured = _capture_parent_invoke(monkeypatch)

    agent = cli.MarketplaceGraph(config={})
    agent.invoke("CAPA report")

    assert captured["input_context"] == {
        "supplier_id": "supplier-a",
        "capa_program_id": "program-a",
    }


@pytest.mark.parametrize("invalid", [None, "", "   ", "bad value", "bad\nvalue", "bad\tvalue"])
def test_scope_rejects_unset_empty_and_whitespace_at_startup(monkeypatch, invalid):
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("supplier_id",))
    monkeypatch.setattr(
        cli,
        "STG_DEFAULT_SCOPE_ENVS",
        {"supplier_id": "STG_DEFAULT_SUPPLIER_ID"},
    )
    if invalid is None:
        monkeypatch.delenv("STG_DEFAULT_SUPPLIER_ID", raising=False)
    else:
        monkeypatch.setenv("STG_DEFAULT_SUPPLIER_ID", invalid)

    with pytest.raises((MissingSecret, ValueError)) as error:
        cli.MarketplaceGraph(config={})

    assert "STG_DEFAULT_SUPPLIER_ID" in str(error.value)


def test_scope_declaration_mismatch_fails_at_startup(monkeypatch):
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("supplier_id",))
    monkeypatch.setattr(cli, "STG_DEFAULT_SCOPE_ENVS", {})

    with pytest.raises(RuntimeError, match="exactly match"):
        cli.MarketplaceGraph(config={})


@pytest.mark.parametrize("env_name", cli._AZURE_OPENAI_SECRET_KEYS)
def test_provision_secrets_rejects_missing_azure_value_at_startup(monkeypatch, env_name):
    monkeypatch.delenv(env_name)
    agent = cli.MarketplaceGraph(config={})
    agent.compile()

    with pytest.raises((MissingSecret, ValueError)) as error:
        agent.provision_secrets(NullProvider())

    assert env_name in str(error.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    agent = cli.MarketplaceGraph(config={})
    agent.compile()
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "gate-test-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gate-test-deployment")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(NullProvider())


def test_allowlisted_provider_hides_undeclared_environment(monkeypatch):
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    assert cli._AllowlistedEnvironmentProvider().get("UNRELATED_SECRET") is None


def test_azure_llm_creates_fresh_client_and_resolves_connection_per_call(monkeypatch):
    constructed: list[dict[str, str]] = []

    class RecordingAzureClient:
        def __init__(self, config):
            constructed.append(dict(config))
            self.config = config

        def complete(self, messages):
            assert messages == [{"role": "user", "content": "classify"}]
            return {"content": self.config["api_key"]}

    fake_module = types.ModuleType("shared.services.llm.azure_openai_client")
    fake_module.AzureOpenAIClient = RecordingAzureClient
    monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)

    llm = cli._AzureMarketplaceLLM()
    with bound_secrets(cli._AllowlistedEnvironmentProvider()):
        assert llm.complete([{"role": "user", "content": "classify"}]) == {"content": "key-a"}
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key-b")
        assert llm.complete([{"role": "user", "content": "classify"}]) == {"content": "key-b"}

    assert [config["api_key"] for config in constructed] == ["key-a", "key-b"]
    assert not hasattr(llm, "_client")
    assert not hasattr(llm, "_cached_client")


def test_compile_wires_azure_only_into_existing_llm_seam():
    agent = cli.MarketplaceGraph(config={})
    agent.compile()

    main = agent._nodes["main"]
    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)
    assert main._config["llm"] is agent.config["llm"]
    # The adapter does not alter the deterministic Stage 1/4 implementation or
    # the typed response projection that guards the existing LLM seam.
    assert main.__class__.__module__ == "src.nodes.main_node"


def test_full_marketplace_invoke_preserves_deterministic_fact_layer(monkeypatch):
    class ContractAzureClient:
        def __init__(self, config):
            assert config["azure_deployment"] == "deployment-a"

        def complete(self, messages):
            assert [message["role"] for message in messages] == ["system", "user"]
            return {
                "content": json.dumps({"causal_leaps": [], "confidence": 0.9}),
                "usage": {},
            }

    fake_module = types.ModuleType("shared.services.llm.azure_openai_client")
    fake_module.AzureOpenAIClient = ContractAzureClient
    monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)

    agent = cli.MarketplaceGraph(config={})
    agent.compile()
    agent.provision_secrets(NullProvider())
    report = json.dumps(
        {
            "root_cause": "bearing wear",
            "corrective_action": "replace bearing",
            "effectiveness_evidence": "trend check",
        }
    )
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(report)

    assert result["status"] == "success"
    assert result["output"]["advisory_signal"] == "inconclusive"
    facts = result["output"]["brief"]["facts"]
    assert [(fact["field"], fact["present"]) for fact in facts] == [
        ("root_cause", True),
        ("corrective_action", True),
        ("effectiveness_evidence", True),
    ]
    assert all(fact["source_ids"] for fact in facts)
    assert report not in json.dumps(result, ensure_ascii=False)
