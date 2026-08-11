# Hermes Reflection Retry Gate Design

## Objective

修复 Hermes scheduled report reflection 的 Agent/MCP 契约冲突，同时保持现有安全校验边界不变。每个有效的 `session_id`/`revision` 在同一个 UTC 日期最多消耗一次反思尝试；安全拒绝只能在后续 UTC 日期由新的 Cron run 重试。连续三个不同 UTC 日期失败后才进入 `attention_required`。

修复后的部署继续使用新历史日期做完整 T+1/T+7/T+15 验收。已经进入 `attention_required` 的验收 artifact 保持不变，不能直接编辑、重置或删除。

## Incident Context

2026-07-26 验收报告的 T+1 processor 正常生成三个 review fact。Hermes memory Agent 对其中两个 session 完成 revision 1 reflection、index promotion 和 memory add，但第三个 session 在同一次 Agent run 内连续提交三次不安全反思，最终持久化为：

```text
reflection_state=attention_required
memory_state=blocked
reflection_attempt_count=3
last_error_code=REFLECTION_UNSAFE_CONTENT
```

当前契约存在两个冲突：

1. Skill 要求 `submit_report_reflection` 对每个 item 只调用一次，但 MCP 的拒绝响应建议“修改并重试”，Agent 因此可能在同一次 run 内耗尽全部尝试。
2. 反思 schema 包含 causal hypotheses，但安全校验器禁止裸词 `cause` 和 `caused`；Skill 没有明确要求使用概率性因果措辞。

安全拒绝正文不会持久化，因此事故只能定位到 allowlisted 错误类别，不能也不应通过读取 memory 或输出 evidence 来反推原始内容。

## Safety Boundary

现有反思安全校验器保持不变，包括：

- certainty patterns；
- real-order instructions；
- prompt-injection patterns；
- credential patterns；
- unsupported post-decision external sources；
- Hermes report marker 和 entry delimiter。

修复不增加自动 sanitizer，不放宽任何正则，不允许 Agent 搜索外部资料，也不允许脚本直接读写 Hermes `MEMORY.md`。有效反思仍必须基于单个 bounded evidence packet。

## Persisted Model

`ReportLearningRevision` 新增：

```python
last_reflection_attempt_date: date | None = None
```

该字段记录最近一次真正消耗尝试次数的 UTC 日期。缺少字段的旧 JSON 通过默认 `None` 保持向后兼容。序列化使用 ISO `YYYY-MM-DD`。字段不接受 Agent 或 MCP 请求参数；生产路径只能使用服务端 UTC 日期。

`reflection_attempt_count` 继续表示实际执行 bounded validation 并被拒绝的次数。同日被 retry gate 拒绝的请求不增加该计数，也不覆盖原错误码或更新时间。

## State Machine

### First attempt on UTC date D

对于有效 session、有效 revision 且当前 snapshot 为下一条 `pending` revision：

- 校验成功：保持现有流程，写入 reflection，设置 `reflection_state=ready`，revision 1 设置 `memory_state=add_pending`，revision 2/3 设置 `memory_state=replace_pending`，并更新项目学习索引。
- 校验失败：原子写入 `reflection_attempt_count + 1`、`last_reflection_attempt_date=D` 和 allowlisted `last_error_code`。
- 第一次或第二次失败：保持 `reflection_state=pending`、`memory_state=blocked`。
- 第三次失败：设置 `reflection_state=attention_required`、`memory_state=blocked`。

### Repeated attempt on UTC date D

在解析和 bounded validation 反思内容之前，在 report-store 排他锁内检查 snapshot：

- 若 `reflection_state=pending` 且当前日期不晚于 `last_reflection_attempt_date`，抛出 `ReportReflectionRetryDeferred` domain exception；这也安全阻断主机 UTC 时钟回拨后的较早日期；
- 不验证反思正文；
- 不增加 attempt count；
- 不更新 error code、timestamps、index 或 memory state；
- MCP 返回 `REPORT_REFLECTION_RETRY_DEFERRED`；
- Agent 必须停止该 session/revision 在本次 run 中的处理并继续独立 item。

### Attempt on a later UTC date

`pending` item 仍由 `report-reflection-pending` 返回。若当前服务端 UTC 日期晚于 `last_reflection_attempt_date`，允许一次新尝试。第二天生成的有效反思可以正常进入 `ready`，之前的拒绝不会污染 lesson、index 或 Hermes memory。

### Idempotency and stale requests

- 已成功持久化的相同 reflection payload 保持现有幂等成功语义。
- 已成功持久化但 payload 不同，保持现有 stale/conflict 行为。
- `attention_required` revision 不重新进入 pending，也不提供 reset 命令。
- 无法识别有效 session/revision 的 malformed envelope 不计入尝试次数，因为无法安全确定持久化身份。
- 具有有效 session/revision 的 schema、evidence、outcome、verdict 和 unsafe-content 拒绝均受日期门禁约束。

## Atomicity and Clock

retry gate 检查、bounded validation rejection 和失败状态写入必须在同一个 report-store lock ownership path 中完成。两个并发请求在同一 UTC 日期最多一个请求执行 validation 并消耗 attempt；另一个请求读取到已写入的日期后返回 deferred。

生产路径使用 `datetime.now(timezone.utc).date()`。`submit_report_reflection` 核心函数增加 keyword-only `attempt_date: date | None = None`；`None` 在函数内解析为当天 UTC 日期，测试传入固定日期。MCP tool schema 不暴露该参数，Agent 不能伪造日期。

如果 validation 成功，现有 index upsert 仍发生在 report record 成功持久化后。retry-deferred 路径必须是字节级无写入路径。

## MCP Contract

新增安全错误码：

```text
REPORT_REFLECTION_RETRY_DEFERRED
```

对于第一次 bounded validation rejection，MCP 保留具体 allowlisted code，例如 `REFLECTION_UNSAFE_CONTENT`，但 `suggested_action` 改为明确说明：

```text
Do not submit this session and revision again in the current Agent run.
Leave the item pending for a later scheduled run and continue independent items.
```

对于同日重复提交，MCP 返回 `REPORT_REFLECTION_RETRY_DEFERRED`，使用相同的 no-retry 指令。响应不包含 reflection、evidence、lesson、memory content 或触发短语。

MCP 仍在进入 store 前拒绝无法归属的 envelope，例如无效 session ID、无效 revision 或非 mapping reflection。对于已经具有有效 session ID/revision 且 reflection 是 mapping 的请求，reflection schema validation 下沉到持久化核心；当前针对 extra fields 的前置 `ReportReflection.model_validate` 分支移除。这样 schema rejection 会像其它 bounded rejection 一样原子记录日期和 attempt，同时对外仍映射为 `INVALID_REPORT_REFLECTION`。

FastMCP 的 decorated tool wrapper 继续按公开 `ReportReflection` schema 校验外部 RPC
payload，并把 nested unknown fields 等 malformed RPC 输入映射为结构化
`INVALID_REPORT_REFLECTION`；这类请求尚未进入持久化边界，因此不消耗 bounded
attempt，但响应仍使用同一条 no-current-run-retry 指令。内部
`submit_report_reflection_impl` 的有效 identity/mapping 路径不再提前执行
该 schema special case，进入核心后发生的 schema rejection 仍受 UTC-date gate 约束。

## Skill Contract

`tradingagents-scheduled-paper-reviews` Skill 的 report reflection 段落必须：

- 明确同一 listing 中每个 `session_id`/`revision` 只允许一次 evidence fetch 和一次 submit；
- 任意 `ok != true` 后禁止在本次 run 再次 fetch、regenerate 或 submit 同一 item；
- 明确忽略任何建议在当前 run 立即 retry 的一般性错误文本，以安全错误码和本 Skill 的 no-retry 规则为准；
- 要求 causal hypotheses 使用 `may have contributed`、`is consistent with`、`could indicate` 等校准措辞；
- 禁止 certainty、real-order、credential、prompt-injection、unsupported external-source、marker 和 delimiter 内容；
- 不复述 evidence 正文，不打印 rejected payload。

Skill 继续独立处理其它 session。单个反思失败不阻止已经 ready 的独立 report 进入 memory promotion。

## Operational Recovery

当前 `attention_required` session 保留为不可变审计 artifact：

- 不修改 `report_memories/<session_id>.json`；
- 不删除 review、report、schedule、index 或 session；
- 不向该 session 手工添加 Hermes memory；
- 不增加 reset/requeue 命令。

修复合并并部署后，安装更新后的 Skill 和 Python package，保持四个 Cron paused，选择一个未使用且 T+15 已完整结束的新历史日期，重新执行完整 v2 acceptance。原失败 report 不计为成功验收报告。

只有新的验收报告在 T+1、T+7、T+15 三阶段全部通过 verifier，且 bounded retention 验收通过后，才恢复生产 Cron。

## Tests

### Schema and storage

- 缺少 `last_reflection_attempt_date` 的旧 revision JSON 可以读取，值为 `None`。
- 新字段 ISO round trip 稳定。

### Core state transitions

- UTC D 首次 unsafe rejection 写入 attempt 1 和 D。
- UTC D 第二、第三次提交返回 deferred，record bytes 不变。
- UTC D+1 第二次实际 rejection 写入 attempt 2 和 D+1。
- UTC D+2 第三次实际 rejection 才进入 `attention_required`。
- UTC D 失败后，UTC D+1 有效 reflection 成功进入 `ready`、更新 index 和 `add_pending`。
- 同日两个并发 rejection 只消耗一次 attempt。
- 所有 bounded rejection code 都使用相同 gate，不只处理 unsafe content。

### MCP

- 首次 rejection 返回原 allowlisted code 和 no-current-run-retry action。
- 同日重复返回 `REPORT_REFLECTION_RETRY_DEFERRED`，attempt count 不变。
- tool schema 不暴露 attempt date。
- 成功、idempotent replay 和 stale payload 的既有测试保持通过。

### Skill and runbook

- 静态契约测试验证一次 fetch/submit、no retry、calibrated causal wording 和 prohibited categories。
- 部署文档记录 deferred 状态、三天门禁、失败 artifact 保留和新日期重新验收流程。

### Verification gate

- focused report-learning/MCP/Skill tests；
- full Hermes integration tests；
- full repository test suite；
- `compileall`；
- `git diff --check`。

## Out of Scope

- 放宽安全校验器；
- 自动改写或清洗 LLM reflection；
- 在同一天为 operator 提供 bypass；
- 重置现有 `attention_required` artifact；
- 修改 Hermes memory retention policy；
- 修改 T+1/T+7/T+15 日期语义；
- 真实交易、外部消息或新增凭据。
