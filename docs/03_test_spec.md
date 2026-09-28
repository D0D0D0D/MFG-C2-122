# テスト仕様 — MFG-C2-122

## 方針

LLM・DB・KB は注入シームに差し替え、テストからネットワークへ接続しない。CAPA 原文はケース・ストアにだけ置き、Graph State / checkpoint / 出力に残らないことを重点的に確認する。

## フレームワーク・セキュリティ契約

| ID | 検証 | 期待結果 | 実装テスト |
|---|---|---|---|
| TC-01 | State 契約 | 設計表の flat TypedDict フィールドを持ち、原文・credential を持たない | `tests/unit/test_state_and_security.py` |
| TC-02 | `SecurityViolationError` | 免責改変、確定結論フィールド、実在しない source ID を fail-closed で拒否 | `tests/unit/test_state_and_security.py` |
| TC-03 | credential 不在 | `src/` に credential 形状リテラルを置かない | `tests/unit/test_state_and_security.py` |
| TC-04 | InvocationContext 境界 | ノードは InvocationContext を生成・State 保持しない。adapter / framework configurable 経由のみ | `tests/unit/test_state_and_security.py` |
| TC-05 | S-4 audit | 各実装 Stage のドメイン監査イベントを発行する | `tests/unit/test_state_and_security.py` |
| TC-06 | S-2 非バイパス | `FunctionNode._security_gate_input()` の override は `TypeError` | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-07 | S-3 非バイパス | `FunctionNode._security_gate_output()` の override は `TypeError` | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-08 | trust level | 全ノードが許可 enum を class body で明示し、外部検証済み trust を要求。加えて PB-6 の常設フィクスチャで **`execute()` が呼ばれなかったこと**を証明する（`test_pb_invoke_order.py` の `test_s1_denial_refuses_execution_before_execute` — `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / `execute` 未呼び出し を assert。正本 docs/03 の TC-08 要求） | `tests/unit/test_state_and_security.py` |

## ドメイン・モード試験

| ID | 入力 / 条件 | 期待結果 | 実装テスト |
|---|---|---|---|
| BL-01 | 承認済み要求 1 版 + 3 必須欄を持つ CAPA | `evidence_sufficient`、根拠マップ付きブリーフ | `tests/unit/test_main_node.py` |
| BL-02 | 要求版が未登録または複数 | `inconclusive` を最優先し、`requires_human_review=true` | `tests/integration/test_api_modes.py` |
| BL-03 | 必須欄不足 | `gaps_present`（照合不能でなければ）と確認質問 | `tests/unit/test_main_node.py` |
| BL-04 | 注入 fake LLM が低 confidence | LLM はラベルのみ返し、参考シグナルは Stage 4 が決定論導出 | `tests/proof_of_boundary/test_audit_and_service_boundary.py` |
| BL-05 | CSV ingest | 是正要求 / 過去観点を DB / retrieval-safe KB projection に分離保存 | `tests/integration/test_api_modes.py` |
| BL-06 | 人間が `human_approved=true` の feedback | append-only 監査イベントと feedback summary | `tests/integration/test_api_modes.py` |
| BL-07 | PDF を CSV と自己申告 | マジックバイトで拒否し `rejected[]` を返す | `tests/integration/test_api_modes.py` |
| BL-08 / N-6,N-7 | 3 モードの実副作用に基づく operation 認証 | scope-less を明示し、standalone token では invoke/ingest/feedback の全 write が 403、store は不変。middleware identity では全モード HTTP composition が通る | `tests/integration/test_api_modes.py`, `tests/parts/entry-adapter/` |
| BL-09 / V-4 | LLM closed response contract | prompt の宣言から応答を機械生成して受理。未知キー、`str`/`list` 型違い、list 内 dict、enum 外、bool/範囲外 confidence は full graph で error。provider 例外本文は state に出さない | `tests/unit/test_llm_response_contract.py` |
| BL-10 / R-1,R-2,R-5 | 実 SQLite scope + query | SQLite の保存 `tenant_ref` を SQL predicate に投影し 1 hit。別区画 hit は見えず、full graph 上流が CAPA 業務本文を query に渡す | `tests/integration/test_sqlite_retrieval.py` |
| BL-11 / I-9 | ingest batch 原子性 | 2 行目の不正で両 target store に1行目も残らず、正常 batch は store ごとに一括 write。第2 target が部分書込後に例外を出しても、補償 rollback で両 store が空へ戻る | `tests/integration/test_api_modes.py` |
| BL-12 / G-4 | inner subgraph failure projection | provider 例外本文を含む LLM 障害時も `graph.invoke()` の caller dict に固定 `error_code` / `error_type` / 重複排除済み `error_log` が届き、生の provider 文字列は出ない | `tests/unit/test_llm_response_contract.py` |

## Proof of Boundary

| ID | 境界 | 期待結果 | 実装テスト |
|---|---|---|---|
| PB-1 | Node → audit | Stage 実行時に domain trace event が発行される | `test_audit_and_service_boundary.py` |
| PB-2 | State 直列化 | State は primitive / list / dict のみ | `test_state_safety.py` |
| PB-3 | 外部サービス | fake LLM seam に加え、実 SQLite adapter が保存 scope で 1 hit し、別区画を返さない。原文は service 境界を越えない | `test_audit_and_service_boundary.py`, `tests/integration/test_sqlite_retrieval.py` |
| PB-4 | import isolation | Level 0 SDK import がない | `test_import_isolation.py` |
| PB-5 | checkpoint 安全 | credential / Pydantic / Context が State にない | `test_state_safety.py` |
| PB-6 | invoke 順序 | S-1 → S-2 → execute → S-3 の外側順序を検証 | `test_pb_invoke_order.py` |
| PB-7 | HITL | **Auto-waived**。`hitl.enabled: false`、interrupt / checkpointer を使わない | `test_pb7_hitl_interrupt_propagation.py` |

## 実行コマンド

```bash
source .venv/bin/activate
pytest tests/ -q --tb=line
pytest tests/proof_of_boundary/ -q --tb=line
```

## 実 LLM 1 回転検証（live LLM check）

`config/agent.yaml` の `generation_mode: "llm"` 宣言に対する実挙動の裏取り。モックは常に整形済みの
応答を返すため、「プロンプトが出力契約を伝えているか」「注入シームが出荷済みクライアントを受けられるか」は
実 LLM を通さないと分からない。

| 項目 | 値 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | Azure OpenAI のモデルを `BaseLLM` 実装として注入 |
| SDK | `agenticstar-agentcore[anthropic]==1.0.1`（CI の `AGENTCORE_WHEEL_SPEC` と一致） |
| 注入シーム | `config["llm"]` → `DomainWorkflowGraph.register_nodes()` → `Stage3CausalAssessmentNode` → `analyze_causal_chain(llm=...)` |
| LLM 呼び出し回数 | 1 |
| 結果 | ✅ `status: success` |
| 非空だった出力キー | `brief`, `corrective_requirements`, `evidence_sources`, `causal_leaps`, `verification_questions`, `advisory_signal`, `advisory_disclaimer`, `evidence_map`, `mode`, `gate_kind` |
| 応答 | `{"causal_leaps":["contradictory_evidence","root_cause_to_corrective","corrective_to_effectiveness"],"confidence":0.99}` — closed contract に適合 |

### この検証で見つかった欠陥（修正済み）

**出荷済み `BaseLLM` クライアントを注入すると必ず失敗していた。**
`config["llm"]` は標準の `shared.services.llm.base_llm.BaseLLM` スロットだが、`_invoke_llm()` は
`callable(llm)` か `llm.invoke(prompt_dict)` しか受け付けていなかった。`BaseLLM` の抽象メソッドは
`complete(messages: list) -> dict` / `stream` / `bind_tools` の 3 つのみで、`invoke` は存在せず
インスタンスは callable でもない。実測では `AnthropicClient` と同形状のクライアントを注入すると
`complete()` が **0 回**しか呼ばれず `status=error / error_code=subgraph_failed / output=null` になった。

修正は `_invoke_llm()` に canonical な completion 経路（`complete` 検出 → `causal_prompt_messages()` で
messages 組み立て → 応答 `content` を JSON として解釈）を追加したもの。既存の callable / `invoke` 経路は
in-process アダプタ向けに残している。closed contract の検証（`_project_causal_response`）と
provider 本文の抑止は変更していない。

| 追加テスト（`tests/unit/test_llm_response_contract.py`） | 内容 |
|---|---|
| `test_shipped_base_llm_client_shape_drives_the_full_graph` | `complete()` だけを持つクライアントが full graph で実際に使われる |
| `test_completion_messages_are_canonical_and_carry_the_generated_contract` | `[system, user]` の canonical messages で、生成した `response_contract` が確かに載る |
| `test_non_json_completion_fails_closed_without_echoing_the_body` | 散文応答は fail-closed。本文は state に出ない |
| `test_completion_that_is_not_exactly_one_object_fails_closed` | list 包み / 前後散文 / 未閉鎖フェンス / 複数 object は拒否（フェンス剥がしはゲート緩和ではない） |
| `test_fenced_completion_is_unwrapped_but_the_contract_still_applies` | 完全な 1 フェンスは剥がすが、中身の enum 違反は従来どおり error |
| `test_completion_provider_exception_text_is_not_copied_to_framework_state` | provider 例外本文が caller envelope に出ない |
| `test_provider_exception_context_is_suppressed_on_the_completion_path` | `raise ... from None` の抑止が保たれている |
| `test_empty_completion_fails_closed` | 空 completion を「所見なし」と読まない |
| `test_empty_completion_is_reported_as_a_provider_fault_not_a_bad_object` | 空 completion は provider 障害として、不正オブジェクトとは別の理由で記録される |

### 副次的に混入させて潰した欠陥（ReDoS）

フェンス剥がしを最初は正規表現 `` ```(?:json)?\s*(.*?)\s*``` `` の `fullmatch` で実装したが、
隣接する `\s*` と `.*?` が同じ空白列を取り合い、**閉じフェンスが無い応答で破滅的バックトラック**を起こした
（実測: 空白 3,200 字で 7.87 秒。LLM が出しうる長さで、呼び出し側から長さを制限できない）。
正規表現をやめて文字列スライス（`_strip_complete_fence()`）に置き換え、同じ受理/拒否の集合を保ったまま
200,000 字でも 0.00 秒になることを確認した。「正規表現の量指定子は必ず有界に」と同じ論点。

### 再現手順

```bash
export AZURE_OPENAI_API_KEY=<key>
python3 work/scripts/mrb_live_llm_check.py <repo>
```

`tests/conftest.py` の `agent_config` / `case_payload` fixture が、承認済み是正要求 3 件・過去観点 1 件・
弱い RCA を含む CAPA 報告を seed する。

### 🔴 出荷される経路では LLM が注入されない（実測・2026-08-20）

上表の ✅ は**注入シームが生きている**ことを示すが、**誰かが注入したときの話**である。
`src/api/server.py:25` は `config={**_runtime_config, "services": services}` を渡しており
**`llm` キーを一切設定しない**。`src/` のどこにも `AnthropicClient` / `OpenAIClient` /
`BedrockClient` / `build_llm` は無い。本番と同じ構築手順を実行した実測:

```
config['llm'] in production construction: None
stage3 node: Stage3CausalAssessmentNode
  _llm = None
```

したがって `config/agent.yaml` の `generation_mode: "llm"` は**出荷経路では成立していない**。
配線には `requires.secrets` への API キー宣言が要るため、本改版では触れず所見として留置する。
Retail Demand Anomaly Detection Agent (TimesFM 2.5 Zero-Shot) と同型。

### 補足 — conftest のアダプタを削除した理由

削除した `init_with_complete_adapter` は「This AG's reviewed seam accepts invoke(prompt)/callable」と
注記され初回レビューで PASS していたが、**そのシームは本番では成立しない**。標準の `config["llm"]` は
`BaseLLM` であり、出荷クライアント（Anthropic / OpenAI / Bedrock）は `complete(messages) -> dict` のみを
実装し、callable でも `invoke` 保持でもない。アダプタは Agent の `__init__` を monkeypatch して
`BaseLLM` を `invoke(prompt)` へ包み直しており、**本番契約違反をテスト側で埋めていた**。
実測でも修正前は `complete()` 0 回・`subgraph_failed`・`output=null` になる。
修正は `complete()` を最初に試す形で、既存の callable / `invoke` 分岐は残してあるため後方非互換ではない。
