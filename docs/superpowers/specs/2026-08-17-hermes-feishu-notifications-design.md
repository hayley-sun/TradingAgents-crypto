# Hermes Feishu Notifications Design

## Goal

在不修改现有日报、复盘和 Hermes memory 语义的前提下，为生产环境增加飞书群机器人通知：

- 每份新归档日报成功后发送一条 BTC、ETH、SOL 摘要。
- 四个既有生产 Cron 任一产生新的 failed execution 时，最迟 5 分钟发送告警。
- 12:00 archive execution 已完成但没有生成当日报告时，发送一条待归档告警。
- 飞书不可用或通知器失败不得改变四个生产任务的执行结果。

通知使用飞书群自定义机器人 Webhook 和签名校验。所有时间按
`Asia/Shanghai` 解释。

## Existing Production Jobs

通知器只观察以下部署中的四个 job，不修改其 schedule、script、skill、workdir
或 delivery：

| Purpose | Job ID | Schedule |
| --- | --- | --- |
| Daily submit | `2d445dfc1a8a` | `0 8 * * *` |
| Daily archive | `5b7f7906306a` | `0 12 * * *` |
| Scheduled review processor | `d6c0e087e5a8` | `15 8 * * *` |
| Scheduled review memory | `e93cfab5f78e` | `30 8 * * *` |

现有 job 继续使用 Hermes local delivery。通知器不得运行分析、归档报告、执行复盘、
调用 LLM、访问交易所、下单或修改 Hermes memory。

## Architecture

新增第五个无 Agent Hermes Cron：

- Name: `tradingagents-feishu-notifier`
- Schedule: `*/5 * * * *`
- Delivery: `local`
- Mode: `no-agent`
- Workdir: `/home/ubuntu/workspace/TradingAgents-crypto`

数据流如下：

```text
Hermes durable executions ──────┐
                               ├─> notifier ─> signed webhook ─> Feishu group
report_batches and reports ─────┘      │
                                      └─> atomic local notification state
```

实现分为五个边界清晰的组件：

1. Bootstrap 只读取通知器允许的私有配置并启动 runner。
2. Execution source 通过 Hermes CLI 读取四个 job 的 durable execution 列表，使用
   严格、fail-closed 的解析器生成规范 execution records。
3. Report source 使用现有 Pydantic schema 验证
   `results/hermes/report_batches/<trade_date>.json`，并校验 archive 中的文件名和
   SHA-256 与 `results/hermes/reports/<trade_date>.md` 一致。消息字段来自结构化
   archive items，不解析 Markdown narrative。
4. Event/state engine 发现新事件、完成去重、维护 retry 状态并原子持久化。
5. Feishu client 生成签名卡片并执行受限 HTTPS POST。

Shell wrapper 只调用 bootstrap，不携带 Webhook URL、签名密钥或其它 secret。

## Event Discovery

### Execution Failures

通知器为每个受监控 job 读取最近的 durable executions，并按发生时间从旧到新处理。
只有 baseline 之后首次观察到的 `failed` execution 才创建事件。事件 ID 为 job ID 和
execution ID 的稳定组合。

Hermes CLI 输出若包含未知状态、缺失字段或格式变化，整个 discovery pass 返回非零，
且不得推进任何 execution cursor。告警只使用 CLI 提供的安全元数据；不复制原始异常、
Prompt、进程环境或 worker output。错误类型固定为 `CRON_EXECUTION_FAILED`，并提示
操作员在服务器上检查对应 execution。

每次扫描最多读取 CLI 支持的 500 条记录。按照当前每日频率，该窗口远大于 90 天；
若已有 cursor 不在窗口中，通知器安全失败，防止静默跳过执行记录。

### Archived Reports

通知器扫描已验证的 report batch。只有同时满足以下条件才创建日报事件：

- batch 有 archive metadata；
- archive 指向同交易日的 Markdown 文件；
- 文件实际 SHA-256 等于 archive SHA-256；
- 该 report event 不在 baseline 或 delivered 状态中。

日报事件 ID 由 trade date 和 archive SHA-256 组成。报告归档不可变，因此重试时可以
从 batch 重新生成相同卡片，无需把完整报告内容复制到通知状态文件。

### Missing Archive Warning

观察到新的 daily archive completed execution 后，通知器以该 execution 的上海日期检查
同日 batch 和 report。若 batch 存在但没有有效 archive，则创建一次黄色待归档事件。
该事件表示分析可能仍 active 或归档未产生结果，不将 archive job 错报为 failed。
若报告之后生成，正常日报事件仍会独立发送。

## First-Run Baseline

上线前必须显式运行 initialize 命令。初始化在持有状态锁时记录：

- 四个 job 当前可见的全部 execution IDs；
- 当前所有已验证 report event IDs；
- 初始化时间和 schema version。

initialize 不执行网络请求，也不创建待发送事件。状态已初始化后重复 initialize 必须
保持幂等；除非使用单独的、明确的迁移命令，否则不得重置 baseline。Cron runner 在
状态未初始化时安全失败，避免历史报告或验收 execution 被批量发送。

## Message Design

### Daily Report Card

绿色成功卡片包含：

- `TradingAgents 日报 | <trade_date>` 标题；
- archive state：`ready` 或 `degraded`；
- BTC、ETH、SOL 各自的 status、processed signal 和 final trade decision；
- 最近一份更早 archive 的交易日期及每个币种的简要对比；
- 本地不可变报告路径；
- “仅用于研究和模拟交易，不构成交易建议”声明；
- 可用于识别罕见重复投递的 event ID。

字段使用现有 runner 的有界、脱敏规则，每个自由文本字段最多 500 个 Unicode 字符，
最终 JSON 请求体最多 20,000 UTF-8 bytes。缺失字段显示“不可用”，不得以实时价格、
其它日期或新的 LLM 内容补全。

### Failure Card

红色告警卡片包含任务名称、job ID、execution ID、发生时间、
`CRON_EXECUTION_FAILED` 和安全检查建议。它不包含原始 stderr、异常、Prompt、API Key、
Webhook、环境变量或 session/report 正文。

### Missing Archive Card

黄色告警卡片包含交易日期、archive job ID、execution ID 和 batch 的安全状态，提示
操作员检查 sessions 与下一次归档。每个 archive execution 最多创建一个此类事件。

## Secrets And Network Security

私有配置路径固定为：

```text
/home/ubuntu/.hermes/secrets/feishu-notifier.yaml
```

`secrets` 目录属于 `ubuntu` 且权限为 `700`；配置文件属于 `ubuntu` 且权限为 `600`。
配置 schema 只允许 version、Webhook URL、signing secret 和四个 monitored job ID/name
映射。配置必须是普通文件且不得为 symlink。未知字段、重复 job ID、空密钥或 job
集合不完整都会安全失败。

网络客户端执行以下约束：

- URL scheme 必须为 `https`；
- host 必须精确等于 `open.feishu.cn`；
- path 必须匹配飞书群机器人 Webhook 路径；
- 禁止重定向；
- 使用有限的 connect/read timeout 和有界响应体；
- 同时检查 HTTP 2xx 和飞书响应中的成功码；
- 使用机器人 signing secret 为每个请求生成 timestamp/signature；
- Webhook URL、secret 和签名不得写入日志、状态文件、Cron 参数或 Git。

日志只输出 event ID、event type、尝试次数、结果类别和安全 HTTP 状态。任何异常文本
必须经过 secret-token 和 secret-assignment 脱敏后才能写入。

通知器不得读取、输出或修改 `/home/ubuntu/.hermes/memories/MEMORY.md`。

## State And Concurrency

通知状态固定保存在：

```text
results/hermes/feishu_notifications/state.json
```

目录权限为 `700`，状态文件权限为 `600`。状态包含 schema version、baseline、受监控
execution IDs、report event IDs、pending/delivered event metadata、attempt count、
next retry time 和最后一次安全结果。它不保存 Webhook、签名密钥或完整报告正文。

每次运行先取得独占文件锁。状态通过同目录 temporary file、flush、`fsync` 和
`os.replace` 原子更新，并在替换后保持 `600` 权限。并发的第二个 notifier 不做网络
请求并正常退出，避免重复发送。

完整的已投递 event metadata 保留 90 天后可裁剪；紧凑的 delivered event ID、
baseline/cursor 和尚未成功的事件不得裁剪。持久保留 report event ID，避免不可变历史
报告在 metadata 裁剪后被重新发现和发送。

## Delivery And Retry Semantics

投递采用 at-least-once 语义。discovery 必须先把新事件以 pending 状态原子持久化，
成功后才能发起网络请求；状态写入失败时不得发送。常规重试由 event ID 去重；但若
飞书已经接收请求，而
进程在持久化 delivered 状态前崩溃，同一 event 可能重新发送。卡片显示相同 event ID，
以便识别这种罕见重复。由于飞书自定义机器人 Webhook 不提供幂等键，本设计选择罕见
重复而不是潜在漏报。

失败退避为 5、10、20、40、60 分钟，之后每 60 分钟重试直到成功。HTTP 429 在安全
范围内遵循 `Retry-After`；timeout、连接错误、非 2xx、响应过大、非法 JSON 和飞书
业务错误都保持 pending。一个事件失败不阻止同一 pass 尝试其它已到期事件。

runner 在无事件或全部到期事件成功时返回 0；配置、discovery、解析、状态持久化或
任一到期投递失败时返回非零。notifier 的失败只出现在其自身 Hermes execution 中，
且 notifier 不监控自己，避免递归告警。

## Testing

单元测试覆盖：

- 飞书 timestamp/signature 的固定向量；
- URL allowlist、重定向禁用和配置权限检查；
- report/failure/missing-archive 卡片渲染和长度边界；
- secret、token、assignment 和异常文本脱敏；
- Hermes execution 严格解析、倒序输入规范化和 cursor gap；
- report batch schema、archive 文件名和 SHA-256 校验；
- initialize 幂等和未初始化 fail-closed；
- event ID、正常去重、crash-window 重试语义；
- 5/10/20/40/60 分钟退避、429 和每小时持续重试；
- 文件锁、原子状态、90 天 delivered 裁剪和 pending 保留；
- 任一来源损坏时不推进 cursor。

集成测试使用本地 fake HTTP server 覆盖成功、timeout、429、5xx、非法/过大响应和飞书
业务错误。自动化测试不得访问真实飞书、真实 Hermes memory 或外部市场 API。

## Deployment And Acceptance

1. 在本地完成测试和文档后，将已评审提交部署到服务器的精确 commit。
2. 安装 owner-only wrapper，并创建 owner-only secret directory/config。
3. 从 `hermes cron list --all` 获取并逐项核对四个生产 job ID；不得按名称猜测 ID。
4. 运行 initialize，验证状态权限和 baseline counts，确认没有网络请求。
5. 创建 `tradingagents-feishu-notifier` 后立即暂停；Hermes 默认创建即 enabled，create
   与 pause 之间不得执行其它命令。
6. 在 paused 状态运行本地 fake endpoint 验收，确认四个生产 job 和项目 artifact
   hashes 不变。
7. 使用专用 test 子命令向真实群发送一条标题明确为“TradingAgents 飞书通知配置验收”
   的卡片。test event 使用独立 event type，不伪造 production report 或 failed run。
8. 确认飞书只收到一条测试卡片，并复核日志未包含 URL、secret 或 signature。
9. 恢复 notifier，检查 `hermes cron status`、`hermes cron list --all` 和 notifier 的
   durable execution。
10. 下一份真实日报作为端到端验收；确认报告卡片一次、内容与已验证 batch/archive
    一致，并确认原四个 Cron 状态和数据不变。

## Rollback

先 pause notifier 并确认不再产生新 execution，再 remove notifier job。删除已安装的
notifier wrapper 可以作为最后一步，但默认保留状态和私有配置供审计与重新启用。
不得删除或修改 sessions、logs、report batches、reports、review schedules、reviews、
learning indexes、report memories、retirement journals 或 Hermes memory。回滚通知器
不需要暂停四个生产 job。

## Non-Goals

- 不发送个人私聊，不实现飞书企业自建应用。
- 不上传 Markdown 文件，不建立公网报告链接。
- 不通过飞书接收命令、审批或交易指令。
- 不改变日报、T+1/T+7/T+15、bounded retention 或 memory 容量规则。
- 不为 notifier 自身建立递归通知渠道。
