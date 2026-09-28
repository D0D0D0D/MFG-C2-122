# Template Design Specification — MFG-C2-122

> Manufacturing Supplier Corrective-Action Evidence Brief Agent
> 承認済み提案（2026-07-23）の §4・§10・§12

## Position in AgentCore Architecture

- **Agent Class**: `SupplierCorrectiveActionEvidenceBriefAgent`
- **L1 Base**: `AgentBaseGraph`（DocGenerationAgent パターン。`AutonomousBaseGraph` ではない）
  - L2 継承は使用しない。L1 を直接継承する。
- **Composition**: 固定 5 ノード backbone の `main` スロットに `GraphNode`（`SupplierCapaReviewGraphNode`）を割り当て、
  内側に多段の domain workflow graph（`src/graph/domain_workflow_graph.py`）を持つ Cat 2 構成。
- **Three-Layer Separation**:
  - State: flat TypedDict composition（primitives + JSON-serializable のみ。Pydantic 禁止 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` のみ override・部分 dict 返却）
  - Graph: composition（`register_nodes()` でスロット注入。`add_edges()` は override しない）

## Design Intent（何をする AG か）

供給者提出の是正措置報告（CAPA/8D の自由記述: 根本原因・是正措置・有効性証拠）を、顧客が承認済みの
是正要求（SCAR／品質協定の 1 版）に照らして横断し、**根本原因 → 是正 → 有効性の因果連鎖の意味的十分性**を
当てはめ判断する。不足・因果の飛躍・要確認点を、是正要求・証拠 source ID・不足論点・確認質問に分離した
引用付きレビューブリーフとしてドラフトする。必須欄・書式の有無は決定論的 Stage 1 に閉じ、LLM 核には数えない。

**構造的にやらないこと（安全設計の核）**: `supplier_approval` / `shipment_release` / `effectiveness_verdict`
に相当する**確定結論フィールドを持たない**（出力スキーマ・context・KB 検索軸から構造的に排除）。供給者承認・
製品出荷・是正の有効性の最終判定はQA 責任者（人間）に残す。

**参考シグナル（advisory_signal・A-1 判断 (B-1)）**: 担当者の一次判断を補助するため、独立フィールド
`advisory_signal`（`evidence_sufficient` / `gaps_present` / `inconclusive` の 3 値）を出力する。これは
**非拘束の参考材料**であり、確定結論ではない。設計上の担保:
- **決定論的導出（優先順位つき）**: LLM に値を選ばせず、Stage 1（必須欄 gaps）・Stage 3（因果連鎖の不足/矛盾
  の有無）・Stage 4（根拠検証・読み取り信頼度）の**構造化結果**から機械的に導出する。優先順位を固定する:
  1. **`inconclusive`** — 読み取り不能・低信頼（Stage 4 の信頼度が閾値未満）、または照合不能。**他の条件より優先**
     （情報不足で判定材料が成立しない状態を最上位に置く）。
  2. **`gaps_present`** — inconclusive でなく、かつ Stage 1 の必須欄 gaps・Stage 3 の因果連鎖の不足/矛盾・
     Stage 4 の根拠不整合のいずれかが 1 件以上ある。
  3. **`evidence_sufficient`** — 上記いずれにも該当しない（gaps ゼロ・因果連鎖の飛躍なし・根拠整合）。
  （境界例: 必須欄不足かつ読み取り低信頼 → `inconclusive` が優先。gaps あり + 高信頼 → `gaps_present`。）
- **免責の構造付随（固定リテラル）**: 出力に必ず免責 `advisory_disclaimer` を同梱する。値は
  `config/config.yaml` の `advisory_disclaimer_text`（固定定数）で、コード/LLM で書き換えない。既定文言:
  「本シグナルは参考であり、供給者承認・製品出荷・是正措置の有効性の最終判定ではない。最終判定は QA 
  責任者が行う。」`advisory_signal` が出力される全ケースで S-3 は `advisory_disclaimer == <定数>` を検査し、
  欠落・改変・弱体化を拒否する。
- **確定結論フィールドは持たない**: `supplier_approval` / `shipment_release` / `effectiveness_verdict`
  という名前のフィールドや pass/fail の確定判定は出さない（誤用・自動連携の防止）。
- **S-3 検査**: `advisory_signal` が存在する全出力で (i) `advisory_disclaimer == <config 定数>`（欠落・改変・
  弱体化を拒否）、(ii) `supplier_approval` / `shipment_release` / `effectiveness_verdict` の禁止フィールドが
  Unicode 表記揺れを含め存在しないことを検査する。自然言語の確定表現は無限に言い換えられるため lexicon を
  主防御にはせず、閉じたフィールド契約と人間レビューを境界にする。いずれか違反は fail-closed。

## 3 モード（エントリポイント）

`src/api/server.py`（FastAPI `/invoke` adapter）が `mode` を受け取り、内側ワークフローに分岐させる。

本 AG は 1 顧客 1 deployment で、顧客内 KB に追加の混在禁止区画がないため entry-adapter は
`SCOPE_KEYS = ()` を宣言する。一方、3 モードはいずれも case / requirement / feedback を永続化する副作用を持つ。
したがって endpoint 名に関係なく `invoke` / `ingest` は `WRITE_INGEST`、`feedback` は
`WRITE_FEEDBACK` として認証し、standalone `INVOKE_AUTH_TOKEN` を許可せず、verified middleware の
非空 `caller_id` を必須とする。

| モード | 用途 | 主な入力 | 主な出力 |
|---|---|---|---|
| `ingest` | 承認済み是正要求（SCAR／品質協定）・過去是正レビュー観点を構造化して DB / KB に蓄積 | 是正要求版、過去レビュー観点（source-linked） | 蓄積結果（件数・provenance） |
| `invoke` | 新規是正案件のレビューブリーフ生成 | 供給者 CAPA/8D 報告 + 有効性証拠 | レビューブリーフ + 根拠マップ |
| `feedback` | 人間編集後の論点・確定理由を明示承認 + 監査イベント付きで反映 | 確定/編集済み論点、学習可否 | 更新済み構造化履歴 |

### AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は既存 Graph を継承した `MarketplaceGraph` を公開する。Marketplace runner の入力は
素の文字列 1 本なので、その文字列全体を供給者 CAPA 原文として case store に隔離し、Graph State へは
`mode=invoke`、`tenant_ref`、生成済み `case_ref` だけを渡す。入力文字列内に `mode=ingest` / `feedback` 相当の
JSON が含まれていても制御情報とは解釈しない。Marketplace から利用できるのはレビュー用 `invoke` だけで、
要求登録と feedback の書込モードは公開しない。

runner が渡す `input_context`（`conversation_history` を含む）は破棄し、`src/entry_adapter/` の
`SCOPE_KEYS` と `STG_DEFAULT_SCOPE_ENVS` だけから新しい dict を組み立てる。現行の単一顧客設計は
`SCOPE_KEYS=()` なので Marketplace scope も `{}` であり、テナント境界には検証済みの
`deployment_tenant_ref` を使う。将来 scope key が宣言された場合は対応する全 STG 環境変数を必須とし、未設定、
空文字、文字列内の任意の空白を `MarketplaceGraph.__init__()` で拒否して compile 前に fail closed する。

Marketplace context は `caller_trust_level=VERIFIED_EXTERNAL` に固定し、runner の caller / trace 情報は維持する。
本 AG は interrupt 非使用だが、Marketplace に resume channel がないため `hitl_allowed=False` も固定する。
runner の `compile()` 後に呼ばれる `provision_secrets()` では、allowlist provider を作り直して Azure の 3 接続値と
宣言済み scope を起動時検証する。LLM facade は接続値も client も保持せず、completion ごとに bound secrets を
解決して新しい `AzureOpenAIClient` を作る。Azure client だけをメソッド内で遅延 import し、それ以外の framework
/ security 依存は module import 時に解決する。

Azure LLM は既存 Stage 3 の closed response contract にのみ接続する。必須欄の有無を確定する Stage 1、
構造化 response の型射影、Stage 4 の優先順位付き `advisory_signal` 導出と S-3 provenance gate は変更しない。
したがって証跡の充足判定を自由文 LLM 応答へ移譲せず、既存の決定論的事実層との境界を維持する。

### feedback 入力契約（外部入力チャネル）

feedback は認証済み担当者から届く場合も外部入力として扱い、永続 store・監査イベント・顧客応答へ
入る前に次の closed schema へ射影する。

```json
{"human_approved": true, "topics": ["任意の短い論点"]}
```

- 許可キーは `human_approved`（必須・`true` のみ）と `topics`（省略可・`list[str]`）だけ。
  未宣言キー、nested dict/list、型違いは値をエラーへ転記せず `feedback_schema_invalid` で拒否する
- `topics` は件数・各文字列長・総文字数を上限化してから走査する
- 全文字列 leaf を SDK の `detect_pii` でマスクし、NFKC + casefold 後に prompt-injection
  パターンを検査する。検出時は永続化せず `feedback_injection_rejected` で拒否する
- store と `record_feedback()` の両境界で同じ検証を行い、監査と応答には検証済み・マスク済み値だけを載せる

## Architecture Overview

### Node Configuration（外側 backbone）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | フレームワーク既定（コンテキスト展開） | — | — | InitializeNode (default) |
| pre_process | テナント/権限解決（未解決 fail-closed）、入力検証、S-2 情報最小化、案件参照 ID 化 | `mode`, `user_input`, `tenant_ref` | `validated_input`, `case_ref`, `tenant_ref` | `PreProcessNode`（`FunctionNode`）override `execute` + `_extra_security_gate_input()` |
| main | `GraphNode` で内側 domain workflow を実行し、モード別ワークフローに委譲 | `validated_input`, `case_ref` | `brief_draft`, `evidence_map`, `status` | `SupplierCapaReviewGraphNode`（`GraphNode`）override `get_subgraph`/`extract_input`/`merge_output` |
| post_process | 根拠リンク実在性/対応検証、情報再走査、禁止フィールド遮断 | `brief_draft`, `evidence_map` | `output`, `requires_human_review`, `status` | `PostProcessNode`（`FunctionNode`）override `execute` + `_extra_security_gate_output()` |
| finalize | フレームワーク既定（出力確定） | — | — | FinalizeNode (default) |

### Inner Domain Workflow（`main` の `GraphNode` 内側 — `src/graph/domain_workflow_graph.py`）

| Stage | 主要ロジック | 出力 |
|---|---|---|
| Stage 1 決定論チェック | 8D 必須欄の有無・書式を validator / config で検証。因果連鎖の意味的結論は出さない。 | `fields_present`, `format_check`, `gaps` |
| Stage 2 過去観点想起 | 保存レコードの `tenant_ref` を DB predicate に投影し、CAPA 業務本文を `query` にした検索で検索セーフな過去観点を順位付けする。固定 section 名だけでは検索しない。結論ラベルは継承しない。 | `recalled_perspectives` |
| Stage 3 因果連鎖の当てはめ（LLM 核） | 是正措置報告を承認済み是正要求に当てはめ、根本原因 → 是正 → 有効性の因果連鎖の支持・不足・矛盾を抽出。応答は検証定数 `FindingField` から生成した closed contract（`causal_leaps: list[str]`, `confidence: number`）だけを許可し、未知キー・型違い・enum 外・範囲外を fail-closed にする。LLM 自由文は State / 顧客出力へ渡さない。 | `brief_draft`（区分済み論点） |
| Stage 4 根拠検証 | 全論点に source link を要求。claim → source の対応、テナント境界、禁止フィールドを検査。Stage 1 gaps と根拠検証結果から `advisory_signal` を決定論導出（LLM に確定結論を書かせない）。 | `evidence_map`, `requires_human_review`, `advisory_signal` |

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process

main 内側（SupplierCapaReviewGraphNode → domain_workflow_graph）:
  Stage 1 (決定論) → Stage 2 (KB 想起) → Stage 3 (LLM 因果連鎖) → Stage 4 (根拠検証)
```

### State Definition（flat TypedDict — `src/schemas/state.py`）

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `invoke` / `feedback` | ✅ |
| `tenant_ref` | `str` | テナント境界キー（全検索を貫通） | ✅ |
| `case_ref` | `str` | 是正案件参照 ID（生の是正報告全文は保持しない） | ✅（invoke/feedback） |
| `validated_input` | `str` | S-2 最小化後の検証済みメタ入力（生の是正報告本文ではない — 下記「生の是正報告のデータ経路」参照） | ✅ |
| `brief_draft` | `dict` | Stage 3 が生成する区分済み論点（是正要求・支持/不足/矛盾）。`post_process` が出力契約フィールドへ写像 | 中間 |
| `recalled_perspectives` | `list[dict]` | Stage 2 の過去照合観点（source-linked・非結論的） | 中間 |
| `corrective_requirements` | `list[dict]` | 是正要求（source ID 付き） | 出力 |
| `evidence_sources` | `list[dict]` | 証拠 source ID | 出力 |
| `gaps` | `list[dict]` | 不足論点 | 出力 |
| `causal_leaps` | `list[dict]` | 因果の飛躍 | 出力 |
| `verification_questions` | `list[str]` | 確認質問 | 出力 |
| `advisory_signal` | `str` | 参考シグナル（`evidence_sufficient`/`gaps_present`/`inconclusive`）。決定論導出・非拘束（A-1 B-1） | 出力 |
| `advisory_disclaimer` | `str` | 参考シグナルの免責固定文（承認/出荷/有効性の最終判定ではない旨） | 出力 |
| `evidence_map` | `dict` | claim → source 対応マップ | 出力 |
| `requires_human_review` | `bool` | 根拠不整合・低信頼時に True | 出力 |
| `status` | `str` | `AgentStatus` 値（`success` / `error` / `retry`） | ✅ |

**State Constraints (mandatory):**
- Flat TypedDict のみ（primitives + JSON-serializable）
- JWT / API keys / credentials を State に置かない（checkpoint DB 漏洩）
- InvocationContext は `config["configurable"]` 経由のみ（State に置かない）
- Pydantic / dataclass / 任意 Python オブジェクト禁止（msgpack 非互換）
- **生の機微情報・是正報告全文を State / checkpoint に残さない** — `case_ref` で参照 ID 化

**生の是正報告のデータ経路（checkpoint 安全性の担保）:**
`AgentState` に継承される `user_input` は checkpoint されるため、**生の CAPA/8D 報告本文を `user_input` に載せない**。
入口 adapter（`src/api/server.py`）が invoke 前に報告本文を `capa_cases` / 原記録ストアへ永続化し、
グラフには `case_ref` と S-2 最小化済みの安全メタデータ（`validated_input`）のみを渡す。
内側 Stage は報告本文が必要な箇所で service 層経由に**一時取得**し、State には戻さない（`brief_draft` 等の
中間フィールドにも生本文を格納しない）。これにより checkpoint / node_history には参照 ID と論点のみが残る。

### 出力契約（固定フィールド分離）

`corrective_requirements` / `evidence_sources` / `gaps` / `causal_leaps` / `verification_questions` を
別フィールドに固定する。加えて **`advisory_signal`（非拘束の参考シグナル・3 値）+ `advisory_disclaimer`
（免責固定文）** を持つ（A-1 判断 (B-1)）。**`supplier_approval` / `shipment_release` /
`effectiveness_verdict` に相当する確定結論フィールドは出力スキーマに持たない**（S-3 で構造遮断・
参考シグナルの確定結論化〔免責欠落・禁止フィールド追加〕を受入テストで拒否条件化）。

## DB / KB Schema（design-entry blocker §12(a)(b)(d) の確定）

監査用の原記録（DB）と、モデル可視の retrieval-safe projection（KB）を責務分離する。

| ストア | 主なテーブル / document | 内容 | 制約 |
|---|---|---|---|
| DB | `capa_cases` | 是正案件メタ（`tenant_ref`, `case_ref`, モード, レビュー状態） | tenant key 付き。tenant 解決不可は fail-closed |
| DB | `corrective_requirements` | 顧客承認済み是正要求（SCAR／品質協定）の版管理 | 版 ID を持つ。1 案件に対し照合対象は承認済み 1 版 |
| DB | `review_perspectives` | 過去是正レビュー観点（原記録） | 監査用。結論ラベルを含みうるが KB へは射影しない |
| DB | `feedback_audit_events` | feedback モードの明示承認 + 監査イベント | 追記のみ |
| KB | 過去照合観点 projection | source-linked な非結論的照合観点・provenance のみ | 原文の無制限ベクトル化禁止。`supplier_approval`/有効性結論ラベルをプロンプトへ与えない |

**照合ロジック（§12(b)）**: `corrective_requirements` の承認済み 1 版を Stage 3 の当てはめ基準として与える。
AG に全品質規格（IATF/ISO 全文）を解釈させない — 顧客承認済みの是正要求だけを規則参照にする。

ingest は要求/観点の両 batch を先に全件検証し、store ごとに 1 回の batch write を行う。2 行目の検証失敗時に
1 行目だけが残らないことを不変条件とする。さらに第2 target writer が部分書込後に失敗した場合は、要求 batch を
identity 単位、KB batch を scoped key 単位で補償 rollback し、両 target を事前状態へ戻す。SQLite retrieval adapter は
`WHERE tenant_ref = ?` を必須とし、保存レコード由来の scope を hit に含める。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, permissions, credential handle）— `config["configurable"]` 経由
- [x] SecurityViolationError — テナント未解決・禁止フィールド検出時
- [x] **S-2**: `_extra_security_gate_input()`（`PreProcessNode` = `FunctionNode`）— 情報最小化、機微情報・是正報告全文を state/log に残さない検査
- [x] **S-3**: `_extra_security_gate_output()`（`PostProcessNode` = `FunctionNode`）— claim→source 対応検証、
      機微情報再走査、`supplier_approval`/`shipment_release`/`effectiveness_verdict` 相当 span/フィールドの検出と拒否（preservation + filtering 両変種）
- [x] **S-4**: `emit_trace_event()` — 各 `execute()` 内で最低 1 件のドメインイベント（`node_start`/`node_complete`/`node_error` は
      framework が `__call__()` で自動発火するため重複させない）

> **S-2/S-3 gate behaviour by node type:**
> - `PreProcessNode` / `PostProcessNode` は `FunctionNode` サブクラス → framework の `@final` ゲートが自動実行。
>   ドメイン検査は `_extra_security_gate_input()` / `_extra_security_gate_output()` でのみ拡張（`_security_gate_*` 直 override は TypeError）。
> - `SupplierCapaReviewGraphNode` は `GraphNode` → S-2/S-3 は内側 workflow / 子グラフ境界で適用済みのため
>   deliberate no-op passthrough（`GraphNode` の設計どおり・バイパスではない）。

### Composition Pattern

- **Pattern**: `GraphNode`（in-process subgraph composition）
- **Composition target**: `src/graph/domain_workflow_graph.py`（内側 domain workflow graph）
- **Mandatory abstract methods（合成一貫性のレビュー観点）**: `get_subgraph` / `extract_input` / `merge_output` を全実装。
  `merge_output` は内側結果を明示マッピング（raw pass-through 禁止 — 出力契約の schema 境界責任）。
- **error_strategy**: `"propagate"`（既定。内側エラーは `SubgraphError` として再送出し fail fast）
- **propagate_hitl**: `False`（本テンプレは Stage 4 の要確認を `requires_human_review` フラグで表現し、
  backbone `interrupt()` は使わない設計 → `hitl.enabled` は false。PB-7 は auto-waived）

## Import Isolation Confirmation
- [x] Level 0（agenticstar-platform SDK）を import しない
- [x] import 対象は `framework/` と `shared/` のみ（`agents/base/` は不要・不使用）

## Security Design（S-1〜S-5・§12(c) の受入契約）

| Layer | 設計方針 |
|---|---|
| S-1 Trust Gate | `required_trust_level = VERIFIED_EXTERNAL`（機微な供給者品質情報を扱う標準業務操作）。各ノードのクラス body に明示宣言。 |
| S-2 Input Gate | `PreProcessNode._extra_security_gate_input()` で情報最小化・テナント境界確認。機微情報を state/log に残さない。 |
| S-3 Output Gate | `PostProcessNode._extra_security_gate_output()` で claim→source 対応、`supplier_approval`/`shipment_release`/`effectiveness_verdict` 相当の確定結論フィールドを拒否。**`advisory_signal` 出力時は `advisory_disclaimer == <config 定数>` を検査**。LLM 応答は `FindingField` / `project_typed_finding` で顧客出力より前に型射影し、未知キー・型違いを拒否する。根拠不在・不整合は `requires_human_review` または fail-closed。 |
| S-4 Audit | 各 `execute()` で `emit_trace_event()`（`shared.utils.audit_logger`）。副作用の有無に関わらず最低 1 件。 |
| S-5 Credential | secrets は `ctx.secrets.require()` 経由（`os.environ` 禁止）。品質記録系の書込 credential を保持しない。src/ に credential リテラルを置かない。 |

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| 判定材料の出力（A-1） | 確定結論フィールドは持たず論点のみ | 非拘束の参考シグナル（advisory_signal 3 値 + 免責）を独立フィールドで出す | **参考シグナル方式 (B-1)** | 担当者の一次判断を補助しつつ、確定結論フィールド（承認/出荷/有効性）は持たない。決定論導出・免責構造付随・S-3 で確定結論化を拒否（ユーザー判断 2026-07-24） |
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定パイプラインの当てはめ型 workflow。自律 think-act ループ不要（Cat 2） |
| Composition pattern | Standalone FunctionNode in main | GraphNode（inner subgraph） | **GraphNode** | Stage 1〜4 の多段 domain workflow を内側グラフに閉じ、外側 backbone を汚さない |
| HITL | backbone `interrupt()` | `requires_human_review` フラグ | **フラグ方式** | 供給者承認判定を人間に残す設計は「要確認フラグ + 根拠マップ提示」で足りる。checkpointer/interrupt 不要（`hitl.enabled: false`・PB-7 auto-waived） |
| error_strategy | propagate | handle | **propagate** | 内側エラーは fail fast。graceful degradation は不要 |

## Differentiation（§12(e) — 近接テンプレートとの差異明文化）

| 比較対象 | 主体・規則体系 | 入力 | 出力 | 本提案との境界 |
|---|---|---|---|---|
| GMP Manufacturing Deviation & CAPA Report Drafting Agent（GMP 逸脱 CAPA 起草） | 自社・作成側 / 製薬 GMP | 自社逸脱 | CAPA 起草 | 本提案は購買品質・**受領側**が供給者提出 SCAR/CAPA を**レビュー**。規則体系（IATF/品質協定）・入力・出力が異なる |
| SDSRevisionEvidenceAgent（SDS 改訂証拠） | 製造品質 / GHS・HazCom・REACH | SDS 改訂 | 改訂整合レビュー | 危険物文書 vs 是正措置。規則体系・入力が異なる |

## Design-entry Blocker Status（decision.md §12）

- (a) DB / KB schema・retrieval-safe projection の分離 → 本 doc「DB / KB Schema」節で確定
- (b) 承認済み是正要求の版管理・照合ロジック → 同節で確定（承認済み 1 版を Stage 3 基準に）
- (c) S-3 安全契約（結論フィールド構造遮断・根拠リンク必須）→ 本 doc「Security Design」節 + 出力契約で schema/受入テスト化方針を確定（実装フェーズで test_spec に落とす）
- (d) 顧客データ export 最小要件・統治契約 → PoC 開始前の受入（顧客窓口オーナー）。本テンプレは外部公開 API 非依存
- (e) 近接テンプレートとの差異明文化 → 本 doc「Differentiation」節で確定
