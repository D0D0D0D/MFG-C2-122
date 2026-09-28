# MFG-C2-122 — SupplierCorrectiveActionEvidenceBriefAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: MFG

## Overview

SupplierCorrectiveActionEvidenceBriefAgent turns a supplier's CAPA / 8D submission into a cited
review brief, read against the customer's own approved corrective requirements. It takes the three
things a supplier reports — root cause, corrective action, effectiveness evidence — and examines
the chain that connects them: where the evidence is missing, where the reasoning jumps, and what a
reviewer should ask before accepting it.

The chain is the point. A CAPA that names a plausible root cause and a plausible action can still
be unacceptable because nothing links the action to the cause, or because the effectiveness
evidence does not demonstrate the action worked. The agent separates what the submission
**states** from what it **implies**, and returns them as distinct lists — facts, inferences,
gaps, causal leaps and verification questions — each carrying the source ids it rests on.

What the agent will not do is built into the response schema, not just the prompt.
`supplier_approval`, `shipment_release` and `effectiveness_verdict` are **forbidden fields**: an
output containing any of them fails the output gate. The agent emits an `advisory_signal` — one of
`evidence_sufficient`, `gaps_present` or `inconclusive` — accompanied by a fixed disclaimer whose
text must match the configured constant exactly, or the output is rejected. A signal that could be
read as a decision is thereby made structurally impossible to emit, rather than discouraged.

`inconclusive` is a real outcome, not a failure. When the submission is unreadable, no approved
requirement matches it, or confidence falls below the threshold, the agent says so and sets
`requires_human_review` rather than producing a confident-looking brief over an input it could not
assess.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- backing stores for cases, approved corrective requirements and the perspective knowledge base.
  These are injectable seams (`wire_services()` in `src/services/service.py`); the defaults are
  in-process and do not survive a restart, so a real deployment substitutes its own;
- optionally a `BaseLLM` implementation through `config["llm"]`. Without it the agent runs its
  deterministic path and still produces a brief;
- **verified middleware identity** in front of the entry adapter — see *Input*, because this is
  stricter than it sounds.

The deployment is **single-customer by design**. `entry_adapter.SCOPE_KEYS` is declared as an
explicit empty tuple: there is no partition key below the deployment boundary, because there is no
customer-internal compartment in this domain. That is a deliberate declaration rather than an
omission, and the adapter fails loudly on a malformed one.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`), `timeout_s` (`30`) | Framework retry and timeout |
| `memory_enabled` (`false`) | No checkpointer — human review here is a flag on the output, not a suspended run |
| `hitl.enabled` (`false`) | Deliberately off; see below |
| `advisory_disclaimer_text` | The canonical disclaimer. The output gate rejects any response whose `advisory_disclaimer` is not exactly this string |
| `deployment_tenant_ref` (`single_customer_deployment`) | Internal reference for the single-customer deployment |

Human review is expressed as `requires_human_review: true` on a completed response, not as a
suspended run. The reviewer reads a finished brief and decides; there is nothing for the agent to
wait on mid-execution, so no checkpointer is needed.

The template does not approve suppliers, does not release shipments, does not render a final
effectiveness verdict, and does not interpret a quality standard in full — it reads one approved
version of the corrective requirements. Ingest accepts lightweight UTF-8 CSV only.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent resolves its secrets through the platform, and every route requires an authenticated middleware identity that the platform establishes. Without it, no route is reachable (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The agent is constructed at import: `src/api/server.py` loads the runtime config, wires the
in-process default stores, compiles the graph and provisions secrets while the module loads. It
starts cleanly, and `/health` answers `200`.

`POST /invoke` is a different matter, and this is the one thing to understand before deploying it
standalone. **Every mode of this agent is a write** — `invoke` stores the submitted CAPA as a
durable case before it analyses it, and `ingest` and `feedback` are writes on their face. The entry
adapter admits a write only from a **verified middleware principal**; a bearer
`INVOKE_AUTH_TOKEN`, which authenticates a caller but establishes no identity, is read-only. So on
a bare `uvicorn src.api.server:app`:

- no `Authorization` header, with `INVOKE_AUTH_TOKEN` set → **401**
- a correct bearer token → **403** (`Verified middleware identity is required.`)

There is no token that reaches this agent's routes. That is intentional — a caller that cannot be
identified must not be able to write into a quality record — and it means the standalone server is
useful for `/health` and for tests, not for exercising the agent. The test suite runs without a
platform connection, calling the graph directly with an injected context.

## Input

`POST /invoke` takes `mode` (`invoke` | `ingest` | `feedback`), `input`, and optionally
`session_id`, `case_ref`, `filename` and `content_type`.

- **`invoke`** — `input` is the CAPA report as a JSON document encoded as a string, carrying
  `root_cause`, `corrective_action` and `effectiveness_evidence` (Japanese key names 根本原因 /
  是正措置 / 有効性証拠 and camelCase spellings are accepted). The case is stored and analysed;
  only a reference to it enters graph state, never the raw report text.
- **`ingest`** — `input` is CSV text registering approved corrective requirements. The declared
  filename and content type are **not** trusted: the bytes are sanitised and validated as CSV, and
  a rejected file returns **400** with the reason rather than being partially loaded.
- **`feedback`** — `input` is a JSON object, and `case_ref` is required and must name a case this
  deployment actually holds (**422** without it, **404** if unknown).

All three require a verified middleware identity (see above).

```bash
curl -X POST https://<your-gateway>/invoke \
  -H 'Content-Type: application/json' \
  -d '{
    "mode": "invoke",
    "case_ref": "case-demo-1",
    "input": "{\"root_cause\":\"設備摩耗\",\"corrective_action\":\"部品交換\",\"effectiveness_evidence\":\"交換後の傾向確認\"}"
  }'
```

The example is written against a gateway that establishes the caller identity. Sent to a bare
standalone server it returns 401 or 403, by design.

## Output

The standard AgentCore envelope is preserved. For `invoke`, `output` carries every declared field
— a response missing one of them fails the output gate rather than being returned partial:

```json
{
  "mode": "invoke",
  "gate_kind": "draft",
  "brief": {"facts": [], "inferences": [], "claims": []},
  "corrective_requirements": [],
  "evidence_sources": [],
  "gaps": [],
  "causal_leaps": [],
  "verification_questions": [],
  "advisory_signal": "gaps_present",
  "advisory_disclaimer": "本シグナルは参考であり、供給者承認・製品出荷・是正措置の有効性の最終判定ではない。最終判定はQA 責任者が行う。",
  "evidence_map": {"claims": []},
  "requires_human_review": true
}
```

`ingest` returns `ingest_summary`; `feedback` returns `feedback_summary`. Values above are
illustrative.

## Security and Limitations

Every node requires verified-external trust, and every write additionally requires a verified
middleware identity — the two checks are separate, and the second is what makes a bearer token
insufficient here.

The raw supplier report never enters graph state. The entry adapter stores it and passes a
reference, so the analysed content lives in the case store rather than in a state channel that
could reach a log or a checkpoint.

The output gate is the enforcement point for the agent's own limits, and it fails closed on all of
them: a forbidden decision field, a disclaimer that does not match the configured text exactly, an
`advisory_signal` outside the three permitted values, a missing required field, or a free-text
statement that no cited source covers. Provenance is checked rather than assumed — every claim
cites a source id from the retrieved set, and a citation to an id that was never retrieved is a
gate failure, not a returned brief with a bad footnote.

Known limits:

- **The comparison basis is one approved version of the corrective requirements**, not a full
  reading of a quality standard.
- **Ingest is CSV only.** PDF and spreadsheet formats are out of scope, and a mis-declared file is
  rejected rather than best-effort parsed.
- **Gap and leap detection is assistive.** A brief that reports no gaps has not established that
  the CAPA is adequate; it has established that this analysis found none.
- **`inconclusive` and `requires_human_review: true` mean read the source.** The evidence map is
  provided so that a reviewer can check the agent's reading against the original record.
- **Default stores are in-process.** Nothing persists across a restart until real backends are
  wired at the `wire_services()` seam.

Output is machine-generated and assistive. Supplier approval, shipment release and the final
effectiveness determination remain with the quality-assurance owner.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), document-generation pattern
- Generation mode: `llm` (with a deterministic path when no model is injected)
- HITL: not enabled — review is a flag on a completed response
- Persistence: injectable case / requirement / knowledge-base seams (in-process by default); no
  framework checkpointer
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using the in-process default stores and a fake model.
Running the agent itself starts cleanly with `uvicorn src.api.server:app`, but its routes are not
reachable without a gateway that establishes caller identity — see *Behaviour without the
platform*.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the output contract and the security
design, together with the design decision record and a comparison against adjacent templates.
`docs/03_test_spec.md` records the test specification and the verification results, including a
live-model run.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
