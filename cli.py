"""AGENTIC STAR Marketplace entry adapter for MFG-C2-122."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for pre-1.0.2 wheels."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import SupplierCorrectiveActionEvidenceBriefAgent
from src.services import CapaServices, wire_services

_NAMESPACE = "mfg"
_AGENT_NAME = "MFG-C2-122"
_PROVIDER_AGENT_NAME = "mfg_c2_122"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)


def _validate_scope_declarations() -> None:
    """Validate the entry-adapter declaration before reading deployment data."""

    if not isinstance(SCOPE_KEYS, tuple):
        raise RuntimeError("Marketplace SCOPE_KEYS must be a tuple")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must not contain duplicates")
    if any(not isinstance(key, str) or not key or any(char.isspace() for char in key) for key in SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must contain clean non-empty strings")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, dict) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS must exactly match SCOPE_KEYS")
    if any(
        not isinstance(env_name, str) or not env_name or any(char.isspace() for char in env_name)
        for env_name in STG_DEFAULT_SCOPE_ENVS.values()
    ):
        raise RuntimeError("Marketplace scope environment names must be clean non-empty strings")


def _allowed_environment_keys() -> frozenset[str]:
    return frozenset((*_AZURE_OPENAI_SECRET_KEYS, *STG_DEFAULT_SCOPE_ENVS.values()))


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only deployment values declared by this single-agent adapter."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _allowed_environment_keys():
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve a required value without trimming or repairing bad input."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Build exactly the scope declared by the reusable entry adapter."""

    _validate_scope_declarations()
    return {scope_key: _require_clean(provider, STG_DEFAULT_SCOPE_ENVS[scope_key]) for scope_key in SCOPE_KEYS}


class _AzureOpenAIClientCompat:
    """Small Azure completion client for AgentCore versions before 1.0.2."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = config

    def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        url = f"{endpoint}/openai/deployments/{deployment}/chat/completions" "?api-version=2024-10-21"
        request = Request(
            url,
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={
                "api-key": self._config["api_key"],
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - deployment-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        return {"content": content, "usage": payload.get("usage", {})}


class _AzureMarketplaceLLM:
    """Resolve Azure connection values and create a fresh client per call."""

    def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        # Azure is the sole lazy import. Framework/security modules stay eager
        # because Marketplace dispatches invoke() via asyncio.to_thread().
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        provider = current_secrets()
        client = AzureOpenAIClient(
            {
                "api_key": _require_clean(provider, "AZURE_OPENAI_API_KEY"),
                "azure_endpoint": _require_azure_endpoint(provider),
                "azure_deployment": _require_clean(provider, "AZURE_OPENAI_DEPLOYMENT"),
            }
        )
        return cast(dict[str, Any], client.complete(messages))


def _deployment_tenant_ref(config: dict[str, Any], scope: dict[str, str]) -> str:
    """Resolve the existing single-deployment tenant boundary."""

    candidate = scope.get("tenant_ref", config.get("deployment_tenant_ref"))
    if not isinstance(candidate, str) or not candidate or any(char.isspace() for char in candidate):
        raise ValueError("Marketplace deployment_tenant_ref must be a clean non-empty string")
    return candidate


class MarketplaceGraph(SupplierCorrectiveActionEvidenceBriefAgent):
    """Marketplace-only invoke boundary, deployment scope, and Azure wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # Scope is deployment configuration, so reject a bad Pod before compile.
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        marketplace_config = dict(config or {})
        marketplace_config.setdefault("services", wire_services())
        marketplace_config["llm"] = _AzureMarketplaceLLM()
        _deployment_tenant_ref(
            {"deployment_tenant_ref": "single_customer_deployment", **marketplace_config},
            scope,
        )
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner conversation_history is untrusted free text outside S-2.
        # Rebuild input_context exclusively from adapter-owned deployment data.
        del input_context
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        tenant_ref = _deployment_tenant_ref(self.config, scope)
        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("Marketplace CAPA input must be a non-empty string")

        services = self.config.get("services")
        if not isinstance(services, CapaServices):
            raise RuntimeError("Marketplace CAPA services are not configured")
        case_ref = services.cases.store_case(tenant_ref=tenant_ref, report_text=user_input)
        graph_input = json.dumps(
            {"mode": "invoke", "tenant_ref": tenant_ref, "case_ref": case_ref},
            ensure_ascii=False,
            sort_keys=True,
        )

        # Marketplace is always a verified external caller and has no resume
        # channel. Preserve runner identity/trace fields while suppressing HITL.
        base_ctx = ctx if ctx is not None else InvocationContext(session_id=session_id or "")
        marketplace_ctx = replace(
            base_ctx,
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=False,
        )
        return cast(
            dict[str, Any],
            super().invoke(
                graph_input,
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context=scope,
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # The runner provider may expose dotenv or other agents' keys. Compile
        # has completed; startup now validates only this adapter's allowlist.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _scope_from_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
