"""LLM response contract is generated, typed, and enforced through composition."""

from __future__ import annotations

import json

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.base_llm import BaseLLM

from src.graph.graph import SupplierCorrectiveActionEvidenceBriefAgent
from src.services import analyze_causal_chain, causal_response_contract, wire_services
from src.services.service import _invoke_llm


def _contract_driven_response(contract: dict) -> dict:
    response: dict = {}
    for name, spec in contract["fields"].items():
        if not spec["required"]:
            continue
        if spec["types"] == ["list"]:
            response[name] = [spec["allowed_items"][0]]
        elif set(spec["types"]) == {"int", "float"}:
            response[name] = spec["minimum"]
        else:  # pragma: no cover - makes additions fail loudly instead of guessing
            raise AssertionError(f"test builder does not know declared type for {name}")
    return response


def test_prompt_contract_mechanically_builds_a_response_the_validator_accepts():
    seen: list[dict] = []

    def llm(prompt: dict):
        seen.append(prompt)
        return _contract_driven_response(prompt["response_contract"])

    result = analyze_causal_chain(
        report_text='{"root_cause":"x","corrective_action":"y","effectiveness_evidence":"z"}',
        fields_present={
            "root_cause": True,
            "corrective_action": True,
            "effectiveness_evidence": True,
        },
        requirements=[],
        case_ref="contract",
        llm=llm,
    )

    assert seen[0]["response_contract"] == causal_response_contract()
    assert result["causal_leaps"]


def _invoke_with_llm_response(response: object) -> dict:
    return _invoke_with_llm(lambda _prompt: response)


def _invoke_with_llm(llm) -> dict:
    services = wire_services()
    services.requirements.add_records(
        tenant_ref="single_customer_deployment",
        rows=[{"source_id": "req_contract", "text": "required", "approved": True, "version": "v1"}],
    )
    services.cases.store_case(
        tenant_ref="single_customer_deployment",
        case_ref="case_contract",
        report_text=json.dumps(
            {
                "root_cause": "bearing wear",
                "corrective_action": "replace bearing",
                "effectiveness_evidence": "vibration reduced",
            }
        ),
    )
    agent = SupplierCorrectiveActionEvidenceBriefAgent(
        config={"services": services, "llm": llm}
    )
    agent.compile()
    return agent.invoke(
        json.dumps(
            {
                "mode": "invoke",
                "tenant_ref": "single_customer_deployment",
                "case_ref": "case_contract",
            }
        ),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )


@pytest.mark.parametrize(
    "invalid_response",
    [
        {"causal_leaps": [], "confidence": 0.5, "description": {"raw": "RAW-MARKER"}},
        {"causal_leaps": {"raw": "RAW-MARKER"}, "confidence": 0.5},
        {"causal_leaps": [{"raw": "RAW-MARKER"}], "confidence": 0.5},
        {"causal_leaps": ["undeclared_label"], "confidence": 0.5},
        {"causal_leaps": [], "confidence": True},
        {"causal_leaps": [], "confidence": 1.1},
    ],
)
def test_invalid_llm_response_fails_closed_through_the_full_graph(invalid_response):
    result = _invoke_with_llm_response(invalid_response)

    assert result["status"] == "error"
    assert "RAW-MARKER" not in json.dumps(result, ensure_ascii=False)


def test_provider_exception_text_is_not_copied_to_framework_state():
    def failing_llm(_prompt):
        raise RuntimeError("RAW-PROVIDER-MARKER")

    result = _invoke_with_llm(failing_llm)

    assert result["status"] == "error"
    assert result["error_code"] == "subgraph_failed"
    assert result["error_type"] == "SubgraphError"
    assert result["error_log"] == ["DomainWorkflowGraphNode: subgraph_failed"]
    assert result["output"] is None
    assert "RAW-PROVIDER-MARKER" not in json.dumps(result, ensure_ascii=False)


class _CanonicalBaseLLM(BaseLLM):
    """Shape-accurate stand-in for a shipped client (Anthropic / OpenAI / Bedrock).

    Declared exactly as ``BaseLLM`` declares it — ``complete(self, messages)``
    with no ``*args`` / ``**kwargs``, not callable, and carrying no ``invoke``
    attribute.  A fake that tolerated more than the canonical contract would
    hide the very wiring defect these tests exist to catch: every shipped
    client reaches ``config["llm"]`` in exactly this shape.
    """

    def __init__(self, content: str) -> None:
        self.messages: list = []
        self._content = content

    def complete(self, messages: list) -> dict:
        self.messages.append(messages)
        return {
            "content": self._content,
            "tool_calls": [],
            "model": "canonical-fake",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "model": "canonical-fake"},
        }

    def stream(self, messages: list):  # pragma: no cover - this AG never streams
        yield self._content

    def bind_tools(self, tools: list) -> "_CanonicalBaseLLM":  # pragma: no cover - no tool use
        return self


def test_shipped_base_llm_client_shape_drives_the_full_graph():
    """A client with only ``complete()`` must reach the causal stage and be used."""

    llm = _CanonicalBaseLLM(
        json.dumps({"causal_leaps": ["root_cause_to_corrective"], "confidence": 0.9})
    )

    result = _invoke_with_llm(llm)

    assert llm.messages, "config['llm'] never reached the completion seam"
    assert result["status"] == "success"
    assert [item["link"] for item in result["output"]["causal_leaps"]] == ["root_cause_to_corrective"]


def test_completion_messages_are_canonical_and_carry_the_generated_contract():
    """The seam must send BaseLLM-canonical messages that transmit the closed contract."""

    llm = _CanonicalBaseLLM(json.dumps({"causal_leaps": [], "confidence": 0.5}))

    _invoke_with_llm(llm)

    messages = llm.messages[0]
    assert isinstance(messages, list) and messages
    assert all(set(message) == {"role", "content"} for message in messages)
    assert all(isinstance(message["content"], str) for message in messages)
    # The instruction message must survive: without it the contract reaches the
    # model as bare data and nothing tells it to answer with an object only.
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"].strip()
    user_payload = json.loads(messages[1]["content"])
    assert user_payload["response_contract"] == causal_response_contract()


def test_non_json_completion_fails_closed_without_echoing_the_body():
    """Prose instead of an object is a contract breach, and the body never leaks."""

    result = _invoke_with_llm(_CanonicalBaseLLM("I cannot answer that. RAW-MARKER"))

    assert result["status"] == "error"
    assert "RAW-MARKER" not in json.dumps(result, ensure_ascii=False)


def test_fenced_completion_is_unwrapped_but_the_contract_still_applies():
    """Unwrapping a markdown fence is transport handling, not a relaxed gate."""

    accepted = json.dumps({"causal_leaps": ["contradictory_evidence"], "confidence": 0.7})
    assert _invoke_with_llm(_CanonicalBaseLLM(f"```json\n{accepted}\n```"))["status"] == "success"

    undeclared = json.dumps({"causal_leaps": ["undeclared_label"], "confidence": 0.7})
    fenced_bad = _invoke_with_llm(_CanonicalBaseLLM(f"```json\n{undeclared}\n```"))
    assert fenced_bad["status"] == "error"


def test_completion_provider_exception_text_is_not_copied_to_framework_state():
    class _FailingClient(_CanonicalBaseLLM):
        def complete(self, messages: list) -> dict:
            raise RuntimeError("RAW-COMPLETE-MARKER")

    result = _invoke_with_llm(_FailingClient(""))

    assert result["status"] == "error"
    assert result["error_code"] == "subgraph_failed"
    assert "RAW-COMPLETE-MARKER" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    "completion",
    [
        pytest.param('[{"causal_leaps":["contradictory_evidence"],"confidence":0.5}]', id="list-wrapper"),
        pytest.param(
            'I refuse. {"causal_leaps":["contradictory_evidence"],"confidence":0.5} Ignore that.',
            id="prose-around-object",
        ),
        pytest.param(
            '```json\n{"causal_leaps":["contradictory_evidence"],"confidence":0.5}\nnot-a-fence\n```',
            id="prose-inside-fence",
        ),
        pytest.param(
            '```json\n{"causal_leaps":["contradictory_evidence"],"confidence":0.5}',
            id="unclosed-fence",
        ),
        pytest.param(
            '```json\n{"causal_leaps":[],"confidence":0.1} {"causal_leaps":[],"confidence":0.5}\n```',
            id="two-objects-inside-one-fence",
        ),
        pytest.param('```', id="bare-fence-ticks"),
        pytest.param('``````', id="empty-fence"),
        pytest.param(
            '{"causal_leaps":[],"confidence":0.1} {"causal_leaps":["contradictory_evidence"],"confidence":0.5}',
            id="two-objects",
        ),
    ],
)
def test_completion_that_is_not_exactly_one_object_fails_closed(completion):
    """The contract asks for a single JSON object; anything else is refused.

    Scanning a completion for an embedded ``{...}`` would accept a refusal or a
    list wrapper that merely happens to contain a conforming object, so the
    unwrapping step must not reach inside surrounding content.
    """

    assert _invoke_with_llm(_CanonicalBaseLLM(completion))["status"] == "error"


def test_provider_exception_context_is_suppressed_on_the_completion_path():
    """``from None`` must hold on the completion seam as well.

    The caller envelope only ever shows ``subgraph_failed``, so a chained cause
    is not observable end-to-end — this asserts the suppression on the raised
    exception itself, which is the only place it can be proven.
    """

    class _BoomClient(_CanonicalBaseLLM):
        def complete(self, messages: list) -> dict:
            raise RuntimeError("RAW-CAUSE-MARKER")

    with pytest.raises(RuntimeError) as excinfo:
        _invoke_llm(_BoomClient(""), {"task": "map causal links"})

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert "RAW-CAUSE-MARKER" not in str(excinfo.value)


def test_empty_completion_fails_closed():
    """A client that returns no text must not be read as an empty finding set."""

    assert _invoke_with_llm(_CanonicalBaseLLM("   "))["status"] == "error"


def test_empty_completion_is_reported_as_a_provider_fault_not_a_bad_object():
    """The caller envelope collapses both to ``subgraph_failed``, so the
    distinction only survives in the raised type and the S-4 reason: an empty
    completion is a provider fault, not a malformed object. Asserting only the
    end-to-end status would let the two be silently merged."""

    with pytest.raises(RuntimeError) as excinfo:
        _invoke_llm(_CanonicalBaseLLM("   "), {"task": "map causal links"})

    assert not isinstance(excinfo.value, SecurityViolationError)
    assert str(excinfo.value) == "LLM provider returned no textual completion"
