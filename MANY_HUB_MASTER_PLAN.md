# MANY Hub — Master Implementation Plan

更新日: 2026-10-03  
ステータス: **実装の正本 / Dots へ渡す主仕様**  
製品名: **MANY Hub**  
想定リポジトリ: `ishizakahiroshi/many-hub`  
CLI仮称: `manyhub`

> この文書を、これまでの `slack-agent-bridge-*` / `agent-bridge` 系資料より優先する。
> 旧資料は Slack、dot、Grok Bot、MANY-AI-CLI、Deskly の調査記録・受入試験の参考資料として残す。
> 新規実装では、Slack・MANY-AI-CLI・Deskly・特定AIを Core の前提にしない。

---

## 0. 製品定義

### 一文でいうと

**MANY Hub は、異なるアプリ・AI Agent・人・自動処理の間で、仕事の依頼、進捗、質問、承認、結果を安全に受け渡すためのオープンなハブである。**

MANY Hub 自体は AI ではない。  
MANY Hub 自体は Slack 製品でも MCP 製品でも MANY-AI-CLI の一部でも Deskly の一部でもない。

### ブランドの意味

`MANY` は MANY-AI-CLI の略ではなく、このプロジェクトでは次の意味で使う。

> **Many tools. Many agents. One hub.**

既存の MANY-AI-CLI は MANY Hub の対応クライアント / Executor の一つとして扱う。

---

## 1. 最終的なユーザー体験

利用者は、自分が普段使う入口を変えなくてよい。

```text
Slack / 社内Chat / ChatGPT / Claude / AI CLI / Deskly / MANY-AI-CLI
                              │
                              ▼
                         MANY Hub
                  task / routing / policy
                  inbox / outbox / result
                              │
                 ┌────────────┼────────────┐
                 ▼            ▼            ▼
              Dots        Grok Bot     OpenClaw
              Codex       Hermes       MANY session
              Script      Human        Other executor
```

例:

1. Slack から「この設計をレビューして」と依頼する。
2. MANY Hub が `task_id` を発行する。
3. 適切な Executor へ配送する。
4. Executor が質問・承認待ち・進捗・結果を返す。
5. MANY Hub が元の会話・アプリへ対応付けて返す。
6. 別の入口から同じ `task_id` を確認しても、同じ仕事として扱われる。

---

## 2. Core に置く概念

Core から製品名を排除する。

### 2.1 Client

仕事を依頼・照会する側。

例:
- Slack
- 社内チャット
- ChatGPT
- Claude
- 素のAI CLI
- Deskly
- MANY-AI-CLI
- Web UI
- REST API client

### 2.2 Transport

メッセージやイベントを運ぶ経路。

例:
- MCP
- HTTP / REST
- WebSocket
- Webhook
- Slack API / Events
- CLI / IPC

### 2.3 Executor

実際に仕事を実行する側。

AIである必要はない。

例:
- Dots
- Grok Bot
- OpenClaw
- Hermes Agent
- Codex
- Claude Code
- MANY-AI-CLI session
- GitHub Actions
- Python script
- 人間

### 2.4 Adapter

製品固有の形式を MANY Hub の共通契約へ変換する層。

Core 内で以下のような分岐をしない。

```text
if executor == "grok" ...
if client == "many-ai-cli" ...
if transport == "slack" ...
```

製品固有処理は Adapter に閉じ込める。

---

## 3. Core Protocol

### 3.1 共通識別子

Core の正本として最低限以下を持つ。

```text
hub_instance_id
principal_id
task_id
request_id
run_id
client_id
transport_id
executor_id
conversation_id
message_id
thread_id
created_at
expires_at
revision
```

Slack 固有の以下は Slack Adapter 内へ閉じ込める。

```text
team_id
channel_id
root_ts
reply_ts
bot_id
app_id
```

MANY 固有の `session_id`、Deskly 固有の `project_id` なども Core の必須項目にしない。

必要なら `external_refs` で保持する。

```json
{
  "external_refs": {
    "many-ai-cli": {
      "session_id": "..."
    },
    "deskly": {
      "project_id": "..."
    }
  }
}
```

### 3.2 Task の状態

初期案:

```text
queued
running
awaiting_input
awaiting_approval
blocked
succeeded
failed
cancel_requested
cancelled
result_uncertain
```

仕事の実行状態と、通知・配送状態を分ける。

### 3.3 イベント

初期案:

```text
task.created
run.started
run.progress
run.question
approval.requested
approval.resolved
artifact.published
run.completed
run.failed
run.cancel_requested
run.cancelled
delivery.failed
```

### 3.4 Executor Capability

Executor を製品名で判定しない。

例:

```json
{
  "executor_id": "executor-123",
  "capabilities": {
    "start_run": true,
    "async_result": true,
    "structured_result": true,
    "resume_owned_run": false,
    "cancel_run": true,
    "interactive_approval": false,
    "artifacts": true,
    "threaded_conversation": true
  }
}
```

未検証能力は `false` とする。

---

## 4. 外部インターフェース

MCP を正本にしない。  
MANY Hub のサービス層を正本にし、それを複数の入口から呼ぶ。

### 4.1 共通操作

仮称:

```text
capabilities
task.create
task.get
task.list
task.reply
task.cancel
executor.list
artifact.get
```

### 4.2 MCP

ChatGPT、Claude、MCP対応CLI等の入口。

MCP Tool 名の仮案:

```text
manyhub_capabilities
manyhub_task_create
manyhub_task_get
manyhub_task_list
manyhub_task_reply
manyhub_task_cancel
manyhub_executor_list
```

### 4.3 CLI

MCP 非対応 CLI や人間が利用する。

```bash
manyhub capabilities --json
manyhub task create --input-file request.json --json
manyhub task get TASK_ID --json
manyhub task list --json
manyhub task reply TASK_ID --input-file reply.json --json
manyhub task cancel TASK_ID --json
```

### 4.4 HTTP API

Deskly、社内チャット、Web UI、MANY-AI-CLI Adapter などから利用する。

API は最初から Internet 公開を前提にしない。

---

## 5. Adapter 方針

初期の Adapter 候補:

### Client / Transport

- Slack
- Generic Webhook / REST
- MCP
- CLI
- Deskly
- MANY-AI-CLI
- 社内チャット

### Executor

- Test / Mock Executor
- Generic command executor
- Codex headless
- Claude Code headless
- MANY-AI-CLI
- dot / Dots
- Grok Bot
- OpenClaw
- Hermes Agent

**すべてを v0.1 で実装しない。**

最初は Mock + Slack + Generic CLI/MCP + 1 Executor で Core を証明する。

---

## 6. MANY-AI-CLI と Deskly の「import」の意味

### 6.1 禁止する設計

- MANY-AI-CLI の provider / PTY / Hub を MANY Hub にコピーする
- Deskly の DB を MANY Hub の正本にする
- 両製品の内部 DB を直接読書きする
- Git submodule で実装を密結合することを必須とする

### 6.2 推奨する設計

MANY-AI-CLI / Deskly は Adapter または Importer を提供する。

#### MANY-AI-CLI

取り込み候補:
- 明示的に選択された session の参照
- provider / model の表示用 metadata
- handoff / result の明示的な受渡し
- MANY側が公開する安全なAPI

取り込まない:
- 全端末ログ
- 全セッションの自動公開
- CLI 認証情報
- Hub 管理トークン
- 任意 PTY への無条件入力

#### Deskly

取り込み候補:
- 明示的に選んだ案件 / project / work item の参照
- task と Deskly item の external reference
- MANY Hub の結果概要へのリンク

取り込まない:
- Deskly DB の丸ごと複製
- Deskly の利用者認証 DB
- 案件全体の自動完了
- 許可していない連絡履歴

### 6.3 Importer の原則

`import` は「コピーして所有権を奪う」ではなく、

> **元システムを正本のまま維持し、MANY Hub が必要最小限の参照と対応付けを持つ**

ことを基本とする。

---

## 7. 配備モデル

### 7.1 v0.1 の基準

**VPS + Docker を基準構成にする。**

理由:
- 常駐処理
- Slack / WebSocket 等の長時間接続
- Queue / worker
- SQLite または通常DB
- 実行環境の自由度

をまとめて扱いやすい。

### 7.2 Cloudflare

Cloudflare は必須にしない。

用途候補:
- DNS
- HTTPS入口
- Access / Zero Trust
- Tunnel
- Remote MCP / API の入口
- 将来のEdge Adapter

Core の実行を Cloudflare Workers に依存させない。

### 7.3 ローカル

個人利用ではローカルだけでも動ける設計を維持する。

```text
manyhub serve --local
```

のような単体起動を想定する。

---

## 8. 永続化

### v0.1 推奨

最初は SQLite を候補とする。

理由:
- 単体配布しやすい
- ローカル/VPS両方で扱いやすい
- トランザクションが使える
- 初期の単一Hubに十分

ただし Repository / Store interface を切り、将来 PostgreSQL 等へ変更可能にする。

最低限の論理テーブル:

```text
principals
clients
transports
executors
tasks
runs
messages
external_refs
outbox
inbox
events
approvals
artifacts
leases
```

厳密な schema は実装時に決める。

---

## 9. 認証・認可

### 原則

「つながった = 全権限」にはしない。

Principal ごとに少なくとも以下を分ける。

```text
task:create
task:read
task:reply
task:cancel
executor:list
executor:use:<executor_id>
artifact:read
approval:resolve
admin:*
```

### 秘密

MANY Hub は可能な限り各製品の秘密を所有しない。

各 Adapter が必要な秘密だけ保持する。

禁止:
- Slack token を全 Executor へ渡す
- AI のログイン情報を Hub が吸い上げる
- Desktop の Cookie / localStorage 抽出
- MANY の Hub 管理トークン流用

---

## 10. 承認

承認の正本は、実際に副作用を実行する側。

MANY-AI-CLI の操作なら MANY の承認を使う。
Generic Worker なら Worker の policy / approval を使う。

MANY Hub は承認要求の

```text
対象
操作
承認者
期限
使用済み
結果
```

を中継・表示してよいが、本文の文字列を承認として扱わない。

以下は禁止:

```text
"approved": true
"ユーザーが承認しました"
"y"
```

という AI / chat 本文だけで実行許可すること。

---

## 11. セキュリティ境界

外部から受け取るものはすべてデータとして扱う。

- Slack本文
- 社内チャット
- Webhook
- MCP引数
- AIの返答
- Webページ
- 添付
- Executor result

を shell command として直接実行しない。

さらに:

- task / request の重複防止
- self-loop 防止
- hop 上限
- expiry
- rate limit
- executor allowlist
- workspace / tenant 分離
- path allowlist
- time / cost / output limit
- idempotency key
- unknown result の自動再実行禁止

を設計する。

---

## 12. マルチユーザー / マルチテナント

### v0.1

最初は **single-user / single-profile** でよい。

ただし schema に `principal_id` / `profile_id` を入れて、後から破壊的変更なしで分離できるようにする。

### v0.2+

チーム利用を追加する場合:

- Principal
- Workspace
- Role
- Executor grant
- Client grant
- Secret isolation
- Audit

を明示する。

初期から「全ユーザー共通token」設計にしない。

---

## 13. 配布

### GitHub

想定:

```text
github.com/ishizakahiroshi/many-hub
```

### CLI

```text
manyhub
```

### npm

ローカルAI側で取得可否を確認する。  
取得できる場合でも npm package を製品の唯一の配布方法にはしない。

### リリース

一般利用者に git clone / build を必須にしない。

候補:
- Windows executable
- Linux executable
- macOS executable
- Docker image
- npm package（必要な部分）
- MCP package

取得物は version / checksum を確認できるようにする。

---

## 14. ライセンス

**MANY Hub のライセンスは、Deskly の AGPL を自動継承しない。**

既存OSSのコードを取り込む前に対象LICENSEを確認する。

**採用ライセンス: Apache-2.0**

理由:
- 企業・社内システム・第三者Adapterから利用しやすい permissive license とする
- 特許ライセンス条項が明示されている
- MANY Hub の目的を、改変コードの公開強制よりも相互運用・採用拡大に置く

既存OSSのコードを取り込む前に対象LICENSEを確認し、
Apache-2.0との互換性、NOTICE等の表示義務を確認する。

---

## 15. Dots が最初に確認する事項

実装開始時に、次を調査して記録する。

### 必須

1. `ishizakahiroshi/many-hub` の GitHub 名取得可否
2. npm / PyPI / Docker namespace の取得可否
3. 使用する実装言語と理由
4. MCP SDK / server 実装候補
5. Slack Adapter の公式SDK候補
6. SQLite の利用方法
7. クロスプラットフォーム配布方法
8. VPS + Docker の最小構成
9. Cloudflare をどこまで利用するか
10. v0.1 で実装する最小 Executor
11. MANY-AI-CLI / Deskly の安全な接続点
12. 参照予定OSSのLICENSE

### 実装言語の判断基準

言語は名前や既存製品に合わせるのではなく、次で比較する。

- MCP実装
- Slack / WebSocket
- SQLite
- 単一バイナリ配布
- Windows / Linux / macOS
- Docker
- Adapter SDK
- テスト
- 依存供給網
- 将来のCloudflare Adapter

比較結果を短く残した上で選び、その後は一つに固定する。

---

## 16. v0.1 のスコープ

### 必須

- Core task model
- SQLite persistence
- Outbox / Inbox
- REST または内部Service API
- CLI
- MCP
- Mock Executor
- Slack Adapter
- 1つの実Executor
- task_id と thread の往復
- 再起動復旧
- duplicate / loop 防止
- timeout
- cancel request
- structured result
- capability discovery
- secrets 非露出
- Docker/VPS起動
- ローカル単体起動
- テスト

### 非必須

- Deskly本番統合
- MANY-AI-CLI本番統合
- ChatGPT全環境対応
- Claude全環境対応
- dot / Grok / OpenClaw / Hermes 全対応
- Teams
- Discord
- 完成した管理Web UI
- Marketplace公開
- SaaS課金
- マルチテナント完成
- Cloudflare WorkersへのCore移植

Adapter interface は用意しても、未検証製品を「対応済み」と表示しない。

---

## 17. 実装順序

### P0 — Core

- Repo初期化
- License
- Architecture
- Config
- SQLite
- task / run / event
- outbox / inbox
- mock executor
- tests

**合格:** SlackもMANYもDesklyもAIも不要で、Mock Client → Mock Executor → Result が通る。

### P1 — CLI / MCP

- CLI
- MCP
- capability discovery
- auth
- task create / get / reply / cancel

**合格:** MANY-AI-CLIなしで利用できる。

### P2 — Slack

- posting
- thread mapping
- receive
- sender validation
- dedupe
- result return
- reconnect

**合格:** 1 request → 1 result が同じ thread に戻る。

### P3 — Real Executor

まず1製品だけ。

候補を比較して最も安全に非対話実行できるものを選ぶ。

**合格:** Client → Hub → Executor → Hub → Client の完全往復。

### P4 — External AI / Bot

- dot / Dots
- Grok Bot
- OpenClaw
- Hermes

を一つずつ Adapter として検証。

**合格:** 製品ごとの起動・返信・cancel・question・result 条件を対応表に残す。

### P5 — MANY / Deskly

Coreを変更せず Adapter だけで接続できることを証明する。

**合格:** Adapterを無効化すると既存製品は従来通り動く。

### P6 — VPS / Team

- Docker
- HTTPS
- auth
- backup
- update
- restore
- multi-user preparation

---

## 18. 受入基準

最低限:

- [ ] MANY-AI-CLIなしで起動できる
- [ ] Desklyなしで起動できる
- [ ] SlackなしでCoreテストが通る
- [ ] MCPなしでもCLI/APIで利用できる
- [ ] 特定AIなしでMock Executorで動く
- [ ] task / request / run が別概念
- [ ] Executor Capabilityで対応機能を判定
- [ ] 製品名によるCore分岐がない
- [ ] Slack固有IDがCore schemaの必須ではない
- [ ] MANY/Deskly IDがCore schemaの必須ではない
- [ ] 同じイベントを2回受信しても実行は1回
- [ ] self-loopしない
- [ ] resultを新taskとして自動再投入しない
- [ ] timeout後の遅延resultを勝手に実行しない
- [ ] cancel要求と停止確認を区別する
- [ ] 不明な副作用を自動再試行しない
- [ ] token / credential がログ・Git・AI入力へ出ない
- [ ] VPSで再起動後もtaskが復旧する
- [ ] ローカル単体利用が可能
- [ ] Adapterを外してもCoreが成立する
- [ ] 未検証製品を対応済みと表示しない

---

## 19. 旧資料から引き継ぐもの

旧Slack連携資料から以下を引き継ぐ。

- 1タスク1親投稿
- thread対応の永続化
- task_id / request_id / run_id
- outbox
- 重複防止
- timeout
- 同一スレッドへの返答
- 人間承認とAI本文の分離
- self-loop防止
- 1 request / 1 result の初期試験
- dot / Grok を個別に検証する
- 投稿成功とAI作業成功を分ける
- 再起動復旧
- tokenを秘密として扱う
- Slackを唯一のTask DBにしない

ただし、これらを Slack 固有実装としてではなく Core の抽象機能へ移す。

---

## 20. Dots への実装指示

以下をそのまま主指示として使用できる。

```text
MANY Hub を一気通貫で実装してください。

この MANY_HUB_MASTER_PLAN.md を実装の正本とし、
旧 slack-agent-bridge / agent-bridge 系資料は背景・Slack固有試験・
dot/Grok固有試験の参考資料として扱ってください。
矛盾する場合は本書を優先してください。

重要:
- 製品名は MANY Hub。
- MANY-AI-CLIの機能ではない。
- Desklyの機能でもない。
- Slack専用ではない。
- MCP専用ではない。
- 特定AI専用ではない。
- Coreから製品固有名とIDを排除する。
- Client / Transport / Executor / Adapter を分離する。
- 実装はMANY-AI-CLI、Deskly、Slack、AIがなくてもCore単体テスト可能にする。
- 最初から全Adapterを実装せず、P0→P6の順で進める。

最初に調査して決める:
1. GitHub / package namespaceの取得状況
2. 実装言語
3. MCP方式
4. 永続化
5. 認証方式
6. VPS/Docker構成
7. Adapter contract
8. 最初の実Executor
9. 参照OSSとライセンス

上記について短いdecision recordを残した後、
承認不要な範囲の設計・実装・モックテストを継続してください。

認証情報の入力、外部サービスのOAuth認可、本番への実投稿、
本番VPSへの反映、公開packageのpublish、GitHub repositoryの新規公開、
push/merge/releaseなど外部へ副作用がある操作は、
ユーザーから別途許可された範囲でだけ実施してください。

実装途中で未検証の製品固有仕様に当たった場合、
Coreへ特例を入れて回避せずAdapter側に閉じ込め、
対応不可 / 未検証をcapabilityとして明示してください。

最終報告には:
- architecture
- decisions
- tests
- supported matrix
- unsupported / unverified
- security boundaries
- deployment
- update / rollback
- backup / restore
- remaining adapters
を含めてください。
```

---

## 21. 実装前に人間が最終確認する項目

次の値だけはユーザーの判断として残す。

```yaml
product_name: MANY Hub
repository_name: many-hub
cli_name: manyhub

repository_visibility: public

license: Apache-2.0
primary_language: TBD

v0_1_deployment:
  local: true
  vps_docker: true
  cloudflare_core: false

v0_1_multi_tenant: false

v0_1_adapters:
  slack: true
  mcp: true
  cli: true
  many_ai_cli: later
  deskly: later
  dots: verify
  grok_bot: later
  openclaw: later
  hermes: later
```

`license` は Apache-2.0 で確定。`primary_language` は Dots が調査案を提示し、最終確定する。

---

## 22. 今後の文書ルール

実装が始まったら文書を増殖させない。

### 正本

`MANY_HUB_MASTER_PLAN.md`

### 実装後に追加してよいもの

```text
README.md
docs/architecture.md
docs/protocol.md
docs/adapters.md
docs/deployment.md
docs/security.md
docs/compatibility.md
docs/decisions/ADR-xxxx.md
```

Master Plan に日々の実装状況を書き続けない。  
実装事実は README / source / tests / compatibility matrix を正本にする。

---

# 結論

MANY Hub は「AIをつなぐアプリ」ではなく、

**仕事の依頼と結果を、異なるClient・Transport・Executor間で安全にルーティングする共通基盤**

として設計する。

MANY-AI-CLI、Deskly、Slack、ChatGPT、Claude、Dots、Grok Bot、
OpenClaw、Hermes Agentは、すべて Core から見れば交換可能な接続先である。

この境界を守ることを最優先とする。
