# Hermes MCP 云主机运行手册

## 范围和安全边界

本手册在云主机上将 `TradingAgents Crypto` MCP 服务器作为本地 Hermes stdio 子进程部署。所有工具结果仅用于研究和模拟交易，严禁据此下达真实订单。本部署不会引入交易所私钥、真实下单、公共 HTTP MCP 端点、新端口或 nginx 变更。

公开 Web UI 保持为 `http://124.222.79.66/`。MCP 仅在本机进程边界内运行，并与 Web UI 分离。

主机前提：

- 主机：`124.222.79.66`
- 项目：`/home/ubuntu/workspace/TradingAgents-crypto`
- Hermes 配置：`/home/ubuntu/.hermes/config.yaml`
- MCP 会话目录：`/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions`
- MCP 复盘目录：`/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/reviews`
- 按币种学习目录：`/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/memories`
- 每日报告批次目录：`/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/report_batches`
- 每日报告归档目录：`/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/reports`
- Hermes 长期记忆：`/home/ubuntu/.hermes/memories/MEMORY.md`
- 会话 schema 版本：`1`

不得提交密钥、在本文档中写入真实密钥，或经 nginx、公共 URL、日志或 shell 历史暴露密钥。

## 部署专用虚拟环境

`MCP SDK` 需要 `Python 3.10` 或更新版本。先检查主机解释器：

```bash
ssh ubuntu@124.222.79.66
python3 --version
python3 -c "import sys; assert sys.version_info >= (3, 10), sys.version"
```

部署前，工作树必须干净。将下方两个占位值分别替换为已评审的集成提交 SHA 和包含该提交、已推送到 `origin` 的远程跟踪引用；不得使用未评审分支、强制检出、重置或丢弃本地改动。该流程会分离检出指定提交，不会修改现有 Web `.venv`。

```bash
cd /home/ubuntu/workspace/TradingAgents-crypto
set -e
git status --short
test -z "$(git status --short)" || { echo "working tree must be clean" >&2; exit 1; }
reviewed_integration_commit="<replace-with-reviewed-integration-commit-already-pushed-to-origin>"
reviewed_integration_ref="origin/main"
git fetch origin --tags
git rev-parse --verify "$reviewed_integration_ref^{commit}"
git cat-file -e "$reviewed_integration_commit^{commit}"
git merge-base --is-ancestor "$reviewed_integration_commit" "$reviewed_integration_ref" || { echo "reviewed commit is not reachable from $reviewed_integration_ref" >&2; exit 1; }
git switch --detach "$reviewed_integration_commit"
git log -1 --oneline

python3 -m venv /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip install --upgrade pip

requirements_file="$(mktemp /tmp/tradingagents-requirements-hermes-mcp.XXXXXX)"
grep -v '^chainlit$' requirements.txt > "$requirements_file"
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip install -r "$requirements_file"
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip install -r requirements_hermes.txt
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip check
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m unittest discover -s tests
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -c "from tradingagents.integrations.hermes_mcp import MCP; assert MCP.name == 'tradingagents_crypto'"
rm -- "$requirements_file"
```

`set -e` 确保仅在所有安装和验证成功后才执行 `rm -- "$requirements_file"`；任一步骤失败都会保留每次运行独有的临时文件以便诊断。确认 `git status --short` 无输出后才可继续。操作员必须将 `reviewed_integration_commit` 替换为已评审提交 SHA，并将 `reviewed_integration_ref` 替换为包含该提交的已推送 `origin` 远程跟踪引用。拉取后会验证该引用可解析为提交，并验证该提交可从该引用到达；未推送或不可达的提交会立即失败，不会执行检出。

`mcp==1.28.1` 需要 AnyIO 4 或更新版本。可选 `chainlit` 依赖为 Chainlit `1.1.202`，其 `asyncer` 约束 AnyIO 低于 4。将 MCP 安装到现有项目 `.venv` 会破坏 `pip check` 和 FastAPI 构造。仅在 `.venv-hermes-mcp` 中排除精确的 `chainlit` 行可解决已验证的冲突；Web `.venv` 不作任何改动，继续保留 Chainlit。

## Hermes 配置

以下命令可在全新主机上创建所需的受限目录。Hermes 必须以 `ubuntu` 用户运行；若实际服务用户不同，必须由该服务用户拥有这些目录和配置文件。

```bash
install -d -m 700 /home/ubuntu/.hermes
install -d -m 700 /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions
install -d -m 700 /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/logs
install -d -m 700 /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/reviews
install -d -m 700 /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/memories
install -d -m 700 /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/report_batches
install -d -m 700 /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/reports
touch /home/ubuntu/.hermes/config.yaml
chmod 600 /home/ubuntu/.hermes/config.yaml
hermes config edit
```

在标准顶层 `mcp_servers` 映射中加入以下条目。每个占位值必须替换为真实密钥，或完全删除对应环境变量；不得将占位值当作凭据。

```yaml
mcp_servers:
  tradingagents_crypto:
    command: "/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python"
    args: ['-m','tradingagents.integrations.hermes_mcp']
    env:
      PYTHONPATH: "/home/ubuntu/workspace/TradingAgents-crypto"
      TRADINGAGENTS_RESULTS_DIR: "/home/ubuntu/workspace/TradingAgents-crypto/results"
      DEEPSEEK_API_KEY: "<replace-with-real-deepseek-secret-or-remove>"
      FINNHUB_API_KEY: "<replace-with-real-finnhub-secret-or-remove>"
      COINGECKO_DEMO_API_KEY: "<optional-replace-with-real-coingecko-secret-or-remove>"
      CRYPTOCOMPARE_API_KEY: "<optional-replace-with-real-cryptocompare-secret-or-remove>"
    timeout: 900
    connect_timeout: 60
```

仅设置当前活动 LLM 提供商的密钥，并删除其余 LLM 密钥项。DeepSeek 使用 `DEEPSEEK_API_KEY`；可选替代项为 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GOOGLE_API_KEY` 和 `OPENROUTER_API_KEY`。数据提供商使用 `FINNHUB_API_KEY`。CoinGecko 为可选项，可使用 `COINGECKO_DEMO_API_KEY` 或 `COINGECKO_PRO_API_KEY`。`CRYPTOCOMPARE_API_KEY` 仅作为历史价格 fallback 使用。

不得加入 `cwd`、工具 include 列表或其他未经验证的字段。这是 stdio 配置，不是 HTTP 服务。

### No-Agent Cron 配置

Hermes 有意从 Cron 脚本子进程中剥离 provider 密钥，`EnvironmentFile` 和 `terminal.env_passthrough` 都不能改变这一安全边界。受版本控制的 `hermes_daily_report_bootstrap` 在导入 runner 前，从 `mcp_servers.tradingagents_crypto.env` 加载白名单值：`TRADINGAGENTS_RESULTS_DIR`、`DEEPSEEK_API_KEY`、`FINNHUB_API_KEY`、`COINGECKO_DEMO_API_KEY`、`COINGECKO_PRO_API_KEY` 和 `CRYPTOCOMPARE_API_KEY`。它只读取当前 `ubuntu` 用户的 `600` Hermes 配置，不写入项目 `.env`、第二份密钥文件、日志或 Cron 输出。

首次部署或 Gateway 未运行时，使用系统服务安装并启动 Gateway：

```bash
sudo hermes gateway install --system --run-as-user ubuntu --start-now
```

已经安装过旧版 systemd 凭据副本时，在部署包含 bootstrap 修复的已评审提交后移除它。重启会短暂中断交互式 Gateway，会话应在此之前结束；以下命令不输出任何密钥：

```bash
sudo rm -f /etc/systemd/system/hermes-gateway.service.d/tradingagents-env.conf
sudo rm -f /etc/tradingagents/hermes-gateway.env
sudo rmdir /etc/tradingagents 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl restart hermes-gateway.service
sudo systemctl is-active --quiet hermes-gateway.service
test "$(stat -c '%a' /home/ubuntu/.hermes/config.yaml)" = "600"
```

## 重载和验证

在 Hermes 会话中重载配置并查看已注册工具：

```text
/reload-mcp
/tools
```

预期工具名称必须精确为：

```text
mcp__tradingagents_crypto__health_check
mcp__tradingagents_crypto__analyze_crypto
mcp__tradingagents_crypto__get_analysis_result
mcp__tradingagents_crypto__review_paper_decision
mcp__tradingagents_crypto__start_daily_report_batch
mcp__tradingagents_crypto__get_daily_report_batch
mcp__tradingagents_crypto__archive_daily_report
```

健康检查提示：

> 请调用 mcp__tradingagents_crypto__health_check，确认 session_store_writable 为 true，并报告当前配置的密钥环境变量是否已设置且非空。不要输出任何密钥值；此检查不验证提供商认证或密钥是否实际可用。

首次使用下列有效的浅层 BTC 分析请求。它明确选择 DeepSeek，只能作为小范围研究请求：

> 请调用 mcp__tradingagents_crypto__analyze_crypto 对 BTC 进行浅层研究分析。使用 trade_date=2026-07-28，analysts=["market", "news"]，llm_provider=deepseek，quick_model=deepseek-v4-flash，deep_model=deepseek-v4-pro，research_depth=1。这是研究和模拟交易，不得提交真实交易或下单。

`analyze_crypto` 只接受请求并启动后台分析。它会立即返回 `session_id` 和当前
状态（通常为 `queued`，worker 已开始时可为 `running`），不会在首次调用中返回
交易决策。记录该 `session_id`，然后使用以下中文提示读取状态和结果：

> 请调用 mcp__tradingagents_crypto__get_analysis_result，使用会话 ID `<session_id>` 取回中文分析结果。请明确说明这些结果仅用于研究和模拟交易。

当返回状态为 `queued` 或 `running` 时，等待一段时间后使用同一 `session_id`
再次调用 `get_analysis_result`。状态为 `completed` 时才读取分析、信号和决策；
状态为 `failed` 时读取安全的错误代码并在修复原因后创建新的分析请求。不要对
同一个请求重复调用 `analyze_crypto`，否则会创建额外的模型任务。

完成分析后，使用晚于原 `trade_date` 且不晚于当前 UTC 日期的复盘日期；该工具只使用已持久化的分析和历史 USD 参考价，不会调用 LLM 或创建真实订单：

> 请调用 mcp__tradingagents_crypto__review_paper_decision，参数为 session_id="<session_id>"，review_date="<YYYY-MM-DD>"。这是研究和模拟交易，不得真实下单。

价格链严格按 `CoinGecko -> CryptoCompare -> Coinbase` 运行。每一次复盘的两个日期必须由同一个提供商给出精确 UTC 日线 USD 价格；不会把不同来源、实时价格或相邻日期混合。CoinGecko 失败后才使用配置了 `CRYPTOCOMPARE_API_KEY` 的 CryptoCompare，再失败才访问公开的 Coinbase 直连 `SYMBOL-USD` 日线。三个来源都不能给出完整成对价格时，工具以 `PRICE_DATA_UNAVAILABLE` 失败关闭。

复盘响应中的 `hermes_memory_entry` 是短期、可审计的记忆候选项。MCP 子进程只写项目 `results/hermes/reviews` 与 `results/hermes/memories`，不会读取或修改 Hermes 内置 memory 文件、用户资料或会话数据库。先在云主机上确认内置 memory 已启用：

```bash
hermes memory status
```

预期输出包含 `Memory injection: enabled` 和 `Memory tool: enabled`。然后在同一个 Hermes 会话中输入：

> 请使用刚才返回的 hermes_memory_entry 调用 Hermes 内置 memory 工具写入记忆。只记录该条交易对的研究和模拟交易经验，不得记录或输出任何密钥，也不得据此真实下单。

## 显式复盘 Skill 与一致性验收

只有用户明确调用 `/tradingagents-paper-review` 时才应创建或补齐 Hermes 长期记忆。普通的 `review_paper_decision` 调用只保存项目 review 和 learning index，不会自动污染长期记忆。

在云主机上安装受版本控制的 skill，然后重新打开一个 Hermes 会话以加载它：

```bash
cd /home/ubuntu/workspace/TradingAgents-crypto
install -d -m 700 /home/ubuntu/.hermes/skills/tradingagents-paper-review
install -m 600 deploy/hermes/skills/tradingagents-paper-review/SKILL.md /home/ubuntu/.hermes/skills/tradingagents-paper-review/SKILL.md
```

在新的 Hermes 会话中，用完成的会话 ID 和复盘日期明确调用：

```text
/tradingagents-paper-review session_id=<session_id> review_date=<YYYY-MM-DD>
```

该 skill 会调用 review MCP 工具，然后只调用一次 Hermes memory tool 的 `memory(action=add, target=memory, content=<hermes_memory_entry>)`。内置 memory tool 负责精确条目的去重和并发写入保护：`Entry added` 与 `Entry already exists` 都进入只读校验器；任何其它结果都停止，不得重试 add。它绝不允许通过终端直接编辑 `/home/ubuntu/.hermes/memories/MEMORY.md`。若需要在 Hermes 外部验收某一条 review，可在云主机上运行：

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_review_verifier \
  --review-id <review_id> \
  --results-dir /home/ubuntu/workspace/TradingAgents-crypto/results \
  --hermes-memory-path /home/ubuntu/.hermes/memories/MEMORY.md
```

成功输出仅包含 `ok`、review ID、项目 review/index 状态和 `hermes_memory_occurrences: 1`。失败返回退出码 `1` 且不打印记忆正文、文件路径或密钥；先保留文件并人工调查。修复时只能经 Hermes memory tool 的目标 `replace` 或 `remove` 操作，再次运行校验器；不要通过 shell 改写 memory。

## 定时维护

维护任务每 15 分钟检查已持久化的异步会话。它只会将带有已记录且已死亡 PID 的 `queued` 或 `running` 会话标记为 `WORKER_EXITED`；没有 PID 的活动会话只报告不修改。它只删除 `results/hermes/logs/*.log` 中超过 14 天的 worker 日志，绝不删除 sessions、reviews、learning indexes 或 Hermes memory。

在云主机安装和启用无密钥的 systemd timer：

```bash
cd /home/ubuntu/workspace/TradingAgents-crypto
sudo install -m 644 deploy/systemd/tradingagents-hermes-maintenance.service /etc/systemd/system/tradingagents-hermes-maintenance.service
sudo install -m 644 deploy/systemd/tradingagents-hermes-maintenance.timer /etc/systemd/system/tradingagents-hermes-maintenance.timer
sudo systemctl daemon-reload
sudo systemctl enable --now tradingagents-hermes-maintenance.timer
systemctl list-timers tradingagents-hermes-maintenance.timer
sudo systemctl start tradingagents-hermes-maintenance.service
sudo journalctl -u tradingagents-hermes-maintenance.service -n 50 --no-pager
```

模板以 `ubuntu` 用户和项目专用虚拟环境运行，启用 `NoNewPrivileges=true`、`PrivateTmp=true` 与 `UMask=0077`，且没有 `EnvironmentFile`。安装前可使用只读预演：

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_maintenance \
  --results-dir /home/ubuntu/workspace/TradingAgents-crypto/results \
  --dry-run
```

## 每日研究报告 Cron

每日报告由两个无 agent 的 Hermes Cron job 组成。08:00 任务只提交 BTC、ETH、SOL 的异步研究批次；12:00 任务读取已持久化的会话并在所有会话终态时归档一份 Markdown 报告。它们均使用云主机 `Asia/Shanghai` 时区、Hermes 本地投递和项目结果目录，不配置 Telegram、Discord、邮件或其他外部投递。

报告批次持久化到 `results/hermes/report_batches/<YYYY-MM-DD>.json`，归档保存为 `results/hermes/reports/<YYYY-MM-DD>.md`。同一天、同一配置的提交会返回既有批次，不会重复启动 worker。报告只能在所有会话已完成或失败时生成；有失败的终态批次生成标记为 degraded 的报告，不会自动重试。报告文件权限为 `600`，相同内容重试返回既有归档，不同内容不能覆盖历史报告。

两个 Cron job 使用 Hermes `--no-agent` 直接运行项目内的确定性 runner。它们不会调用 Hermes 控制模型、不会启动 MCP stdio client，也不会使用 daily-report skill；submit 按既有设计仅创建后台 DeepSeek 分析 worker。runner 只复用项目既有的批次、会话和不可变归档实现；归档 narrative 由安全的持久化结果确定性生成。交互式 `/tradingagents-daily-report` skill 仅供人工 Hermes 会话使用，保留但不由 Cron 附加。

定时任务依赖上节的 `600` Hermes 配置。每个 wrapper 只调用 bootstrap；bootstrap 在导入 runner 前读取 `mcp_servers.tradingagents_crypto.env` 的固定白名单，因此不会依赖被 Hermes 清洗的 Gateway 子进程环境。安装交互式 skill 和无 agent scripts 后，以 `ubuntu` 用户确认 Gateway 已运行。

```bash
cd /home/ubuntu/workspace/TradingAgents-crypto
install -d -m 700 /home/ubuntu/.hermes/skills/tradingagents-daily-report
install -m 600 deploy/hermes/skills/tradingagents-daily-report/SKILL.md /home/ubuntu/.hermes/skills/tradingagents-daily-report/SKILL.md
install -d -m 700 /home/ubuntu/.hermes/scripts
install -m 700 deploy/hermes/scripts/tradingagents-daily-report-submit.sh /home/ubuntu/.hermes/scripts/tradingagents-daily-report-submit.sh
install -m 700 deploy/hermes/scripts/tradingagents-daily-report-archive.sh /home/ubuntu/.hermes/scripts/tradingagents-daily-report-archive.sh
sudo systemctl is-active --quiet hermes-gateway.service
hermes cron status
```

替换旧任务前使用 `hermes cron list --all` 记录 job ID，并确认旧任务均为 paused。先创建并暂停 replacement job，再移除旧的 agent 驱动 job；这样创建失败时旧配置仍可保留。Hermes 仅接受相对于 `~/.hermes/scripts/` 的 `--script` 文件名，不能传安装时使用的绝对路径。不得删除 batch、reports、sessions、reviews、learning indexes 或 Hermes memory。创建与暂停之间不可执行其它命令。

```bash
PROJECT_DIR=/home/ubuntu/workspace/TradingAgents-crypto
old_submit_job_id='<replace-with-paused-agent-submit-job-id>'
old_archive_job_id='<replace-with-paused-agent-archive-job-id>'
hermes cron create --name tradingagents-daily-report-submit --deliver local --no-agent --script tradingagents-daily-report-submit.sh --workdir "$PROJECT_DIR" '0 8 * * *'
hermes cron create --name tradingagents-daily-report-archive --deliver local --no-agent --script tradingagents-daily-report-archive.sh --workdir "$PROJECT_DIR" '0 12 * * *'
hermes cron list --all

submit_job_id='<replace-with-new-submit-job-id-from-cron-list-all>'
archive_job_id='<replace-with-new-archive-job-id-from-cron-list-all>'
hermes cron pause "$submit_job_id"
hermes cron pause "$archive_job_id"
hermes cron list --all
hermes cron remove "$old_submit_job_id"
hermes cron remove "$old_archive_job_id"
```

在首次启用前依次手动验证。`hermes cron run` 只把任务安排到下一次 scheduler tick，不会同步等待；因此必须等待新的 durable execution 达到终态后再暂停 job。无 agent submit 成功只会创建批次和后台 worker；它不是实时分析等待器。runner 对成功、active archive 等正常状态输出单行 JSON，其他安全错误返回非零退出码。等待所有会话达到终态后，再运行 terminal archive。使用 `hermes cron runs` 查看持久化执行记录；当 archive job 输出 active 时，不得手工创建部分报告或让 agent 使用终端写文件。

```bash
set -e
run_once_and_pause() {
  local job_id="$1" before current
  before="$(hermes cron runs "$job_id" --limit 1 2>&1 || true)"
  hermes cron resume "$job_id"
  hermes cron run "$job_id"
  for _ in $(seq 1 60); do
    current="$(hermes cron runs "$job_id" --limit 1 2>&1 || true)"
    if [ "$current" != "$before" ] && printf '%s\n' "$current" | grep -Eq '[[:space:]](completed|failed)[[:space:]]'; then
      hermes cron pause "$job_id"
      printf '%s\n' "$current"
      if printf '%s\n' "$current" | grep -Eq '[[:space:]]failed[[:space:]]'; then return 1; fi
      return 0
    fi
    sleep 2
  done
  hermes cron pause "$job_id"
  echo "Cron run did not reach a terminal state before timeout" >&2
  return 1
}

run_once_and_pause "$submit_job_id"

# 提交后立即验证批次已创建，并确认此时没有报告。
find /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/report_batches -maxdepth 1 -type f -name '*.json' -printf '%m %f\n'
find /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/reports -maxdepth 1 -type f -name '*.md' -printf '%m %f\n'

# 在 session 仍为 queued 或 running 时验证 active archive 不写报告。
run_once_and_pause "$archive_job_id"

# 等待已提交的异步 session 全部终态后执行 terminal archive。
run_once_and_pause "$archive_job_id"

find /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/report_batches -maxdepth 1 -type f -name '*.json' -printf '%m %f\n'
find /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/reports -maxdepth 1 -type f -name '*.md' -printf '%m %f\n'
test "$(find /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/reports -maxdepth 1 -type f -name '*.md' -printf '%m\n' | sort -u)" = "600"
```

确认 submit、active archive 与 terminal archive 记录均正常，归档文件权限为 `600`，且报告包含研究和模拟交易声明后，恢复两个 job：

```bash
hermes cron resume "$submit_job_id"
hermes cron resume "$archive_job_id"
hermes cron status
hermes cron list --all
```

首次修复凭据加载时，不得修改已经创建的当天 batch。保留两个生产 job 为 paused，并使用不晚于当前日期、且 `report_batches` 中不存在的历史日期创建临时历史日期无 agent job。临时 wrapper 只向现有 bootstrap 传入 `--trade-date`，不包含密钥；临时 job 必须在每次运行后暂停。依次验证 submit 创建三个 session、session 为 queued 或 running 时 archive 输出 active 且不创建报告、全部终态后 archive 创建一份权限 `600` 且包含研究和模拟交易声明的报告。完成后删除临时 job 和临时 wrapper；只有全部通过后才恢复生产 job。

日常观测使用 `hermes cron status`、`hermes cron list --all` 与 `hermes cron runs <job-id>`。暂停某个 job 不会删除既有 batches、reports、sessions、reviews、learning indexes 或 Hermes memory。需要撤销调度时先 `hermes cron pause <job-id>`，确认后再使用 `hermes cron remove <job-id>`；不要删除报告目录作为回滚手段。

## T+1/T+7/T+15 自动复盘与长期记忆

新版本归档的每个 BTC、ETH、SOL completed session 会注册 T+1、T+7、T+15 三个复盘项，计划保存在 `results/hermes/review_schedules/<trade_date>.json`。新归档必须带有 `scheduled_review_version: 2`；旧归档属于 `旧 v1`，保持原有 review 和 learning index 连续性，不会自动回填旧报告。T+N 表示精确 UTC 复盘价格日期，只有该日期完整结束（`review_date` 严格早于当前 UTC 日期）才会执行。

复盘使用一个共享的确定性 processor 和一个 Hermes Agent job，时区均为 `Asia/Shanghai`：08:15 processor 同时处理 v1 legacy review 与 v2 report fact，更新 `results/hermes/reviews`、`results/hermes/memories/<SYMBOL>.json` 和 `results/hermes/report_memories/<session_id>.json`；该项目索引持久保留全部复盘索引项，后续分析仍只注入最近 5 条 lesson。processor 绝不读取或写入 Hermes 长期 memory。08:30 Agent job 只加载专用 skill，通过内置 memory tool 完成旧 v1 add 以及 v2 的 report-level add/replace；任何脚本都不得通过脚本直接修改 `/home/ubuntu/.hermes/memories/MEMORY.md`。

### 暂停旧任务、安装来源并创建替换任务

覆盖 wrapper 或 skill 之前，先记录旧 v1 job ID，暂停两项并用第二次 list 验证状态
确实为 paused。全新部署没有旧 job 时记录该事实并跳过对应 pause；不得用名称猜测 ID：

```bash
set -e
cd /home/ubuntu/workspace/TradingAgents-crypto
hermes cron list --all
old_process_job_id='<old-v1-processor-job-id-from-list>'
old_memory_job_id='<old-v1-memory-job-id-from-list>'
hermes cron pause "$old_process_job_id"
hermes cron pause "$old_memory_job_id"
hermes cron list --all
# 人工确认两个 old_* ID 均显示 paused 后才继续。
```

然后从已评审、已推送的集成提交安装 owner-only wrapper 和 skill；不要创建新 API
key 或第二份密钥文件：

```bash
set -e
install -d -m 700 /home/ubuntu/.hermes/scripts
install -m 700 deploy/hermes/scripts/tradingagents-scheduled-review-process.sh /home/ubuntu/.hermes/scripts/tradingagents-scheduled-review-process.sh
install -d -m 700 /home/ubuntu/.hermes/skills/tradingagents-scheduled-paper-reviews
install -m 600 deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md /home/ubuntu/.hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md
hermes memory status
```

Hermes 创建 Cron job 后默认 enabled，所以必须逐项创建和立即暂停。处理完第一项的
create 输出、记录 ID 并 pause 后，才能创建第二项；两次 create/pause 之间不得执行
其它命令。旧 job 继续保持 paused 并保留，直到全部 v2 验收成功：

```bash
set -e
PROJECT_DIR=/home/ubuntu/workspace/TradingAgents-crypto
MEMORY_PROMPT='Run the tradingagents-scheduled-paper-reviews skill once. Process bounded legacy and report items independently; never edit memory files directly.'
hermes cron create --name tradingagents-scheduled-review-process --deliver local --no-agent --script tradingagents-scheduled-review-process.sh --workdir "$PROJECT_DIR" '15 8 * * *'
scheduled_review_process_job_id='<job-id-from-immediately-preceding-create>'
hermes cron pause "$scheduled_review_process_job_id"
hermes cron create --name tradingagents-scheduled-review-memory --deliver local --skill tradingagents-scheduled-paper-reviews --workdir "$PROJECT_DIR" '30 8 * * *' "$MEMORY_PROMPT"
scheduled_review_memory_job_id='<job-id-from-immediately-preceding-create>'
hermes cron pause "$scheduled_review_memory_job_id"
hermes cron list --all
# 人工确认两个 scheduled_review_* ID 均显示 paused，旧 v1 ID 仍保留且 paused。
```

processor bootstrap 只从 `mcp_servers.tradingagents_crypto.env` 加载结果目录和价格
提供商白名单值，不加载 DeepSeek key；配置缺失时安全失败。安装前确认 memory
tool/injection 均 enabled，且工作树、结果目录和 Hermes 目录权限正确。

### 新 v2 验收与 T+ 检查

先定义验收 helper，此时不要创建或归档 v2 acceptance report。helper 会临时 resume
指定 job，等待新 durable run 到达 completed/failed 后立即 pause；超时和 failed
都会返回非零。只读 verifier 调用项目现有函数，不修改 Hermes memory，并且只输出
安全字段：

```bash
set -e
run_scheduled_job_once_and_pause() {
  local job_id="$1" before current
  shift
  before="$(hermes cron runs "$job_id" --limit 1 2>&1 || true)"
  hermes cron resume "$job_id"
  if ! hermes cron run "$job_id" "$@"; then
    hermes cron pause "$job_id"
    return 1
  fi
  for _ in $(seq 1 60); do
    current="$(hermes cron runs "$job_id" --limit 1 2>&1 || true)"
    if [ "$current" != "$before" ] && printf '%s\n' "$current" | grep -Eq '[[:space:]](completed|failed)[[:space:]]'; then
      hermes cron pause "$job_id"
      printf '%s\n' "$current"
      if printf '%s\n' "$current" | grep -Eq '[[:space:]]failed[[:space:]]'; then return 1; fi
      return 0
    fi
    sleep 2
  done
  hermes cron pause "$job_id"
  echo "Cron run did not reach a terminal state before timeout" >&2
  return 1
}

verify_report_memory_stage() {
  local session_id="$1" revision="$2"
  /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python - "$session_id" "$revision" <<'PY'
import json
import sys
from pathlib import Path

from tradingagents.integrations.hermes_report_memory_verifier import (
    verify_report_memory_consistency,
)

result = verify_report_memory_consistency(
    sys.argv[1],
    int(sys.argv[2]),
    Path("/home/ubuntu/workspace/TradingAgents-crypto/results"),
    Path("/home/ubuntu/.hermes/memories/MEMORY.md"),
)
print(json.dumps({
    "ok": result.ok,
    "session_id": result.session_id,
    "revision": result.revision,
    "marker_occurrences": result.marker_occurrences,
    "exact_content_occurrences": result.exact_content_occurrences,
    "index_matches_latest_reflection": result.index_matches_latest_reflection,
}, sort_keys=True))
raise SystemExit(0 if result.ok else 1)
PY
}
```

先手动执行一次 replacement 08:15 processor job，证明安装后的 wrapper、workdir 和
Cron path 可运行；helper 返回后该 job 必须重新处于 paused：

```bash
run_scheduled_job_once_and_pause "$scheduled_review_process_job_id"
```

#### Create and archive v2 acceptance report

processor smoke 完成并重新 paused 后，才创建一个结果目录中从未使用过的历史日期
report batch。submit 创建三个异步 session；重复 archive 直到不再返回 active，随后
用结构化 JSON 检查归档版本和九个尚未到期的 schedule 项。不要复用已有 batch/date，
且所选 trade date 必须至少早于当前 UTC 日期 16 天，保证 T+15 已完整结束。以下
guard 在 submit 前检查全部条件，失败时明确返回非零；不要在此时运行 scheduled
review processor：

```bash
set -e
ACCEPTANCE_TRADE_DATE='<unused-historical-YYYY-MM-DD>'
# Test-only overrides are consumed only by extracted guard tests, never deployment.
unset TRADINGAGENTS_ACCEPTANCE_GUARD_TESTING
unset TRADINGAGENTS_ACCEPTANCE_TODAY_UTC
unset TRADINGAGENTS_ACCEPTANCE_RESULTS_DIR
validate_acceptance_trade_date() {
  /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python - "$1" <<'PY'
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

value = sys.argv[1]
try:
    parsed = date.fromisoformat(value)
except ValueError as error:
    raise SystemExit("ACCEPTANCE_TRADE_DATE must be an ISO YYYY-MM-DD date") from error
if parsed.isoformat() != value:
    raise SystemExit("ACCEPTANCE_TRADE_DATE must use canonical ISO YYYY-MM-DD form")

testing = os.environ.get("TRADINGAGENTS_ACCEPTANCE_GUARD_TESTING") == "1"
if testing:
    today_value = os.environ.get("TRADINGAGENTS_ACCEPTANCE_TODAY_UTC", "")
    results_value = os.environ.get("TRADINGAGENTS_ACCEPTANCE_RESULTS_DIR", "")
    try:
        today = date.fromisoformat(today_value)
    except ValueError as error:
        raise SystemExit("test UTC today must be an ISO YYYY-MM-DD date") from error
    if today.isoformat() != today_value or not results_value:
        raise SystemExit("test guard overrides are invalid")
    results_root = Path(results_value)
else:
    today = datetime.now(timezone.utc).date()
    results_root = Path("/home/ubuntu/workspace/TradingAgents-crypto/results")
if not results_root.is_absolute():
    raise SystemExit("acceptance results root must be absolute")
if parsed > today - timedelta(days=16):
    raise SystemExit("ACCEPTANCE_TRADE_DATE must have a fully elapsed T+15 UTC date")

root = results_root / "hermes"
batch_path = root / "report_batches" / f"{value}.json"
schedule_path = root / "review_schedules" / f"{value}.json"
if batch_path.exists():
    raise SystemExit("ACCEPTANCE_TRADE_DATE already has a report batch")
if schedule_path.exists():
    raise SystemExit("ACCEPTANCE_TRADE_DATE already has a review schedule")
print(f"acceptance date ready: {value}; latest allowed: {today - timedelta(days=16)}")
PY
}
validate_acceptance_trade_date "$ACCEPTANCE_TRADE_DATE"
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_daily_report_bootstrap submit --trade-date "$ACCEPTANCE_TRADE_DATE"

archive_state=active
for _ in $(seq 1 180); do
  archive_output="$(/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_daily_report_bootstrap archive --trade-date "$ACCEPTANCE_TRADE_DATE")"
  printf '%s\n' "$archive_output"
  archive_state="$(printf '%s\n' "$archive_output" | /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -c 'import json, sys; print(json.load(sys.stdin).get("state", "archived"))')"
  if [ "$archive_state" != "active" ]; then break; fi
  sleep 10
done
test "$archive_state" != "active"

/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python - "$ACCEPTANCE_TRADE_DATE" <<'PY'
import json
import sys
from pathlib import Path

root = Path("/home/ubuntu/workspace/TradingAgents-crypto/results/hermes")
trade_date = sys.argv[1]
batch = json.loads((root / "report_batches" / f"{trade_date}.json").read_text(encoding="ascii"))
schedule = json.loads((root / "review_schedules" / f"{trade_date}.json").read_text(encoding="ascii"))
assert batch["archive"].get("scheduled_review_version") == 2
assert schedule.get("workflow_version") == 2
assert len(schedule["items"]) == 9
assert all(item["state"] == "review_pending" for item in schedule["items"])
print(json.dumps({
    "scheduled_review_version": 2,
    "workflow_version": 2,
    "schedule_count": len(schedule["items"]),
    "all_review_pending": True,
}, sort_keys=True))
PY
```

只有上述检查通过后，才用同一个 `ACCEPTANCE_TRADE_DATE` 计算并替换以下 T+1、T+7、
T+15 日期占位值。这样三个显式 `process-due` 调用会依次创建 revision 1、2、3，而
processor smoke 不可能提前消费 acceptance schedule。

T+1 使用 add，后续 `T+7/T+15 replace` 同一稳定 marker。每个阶段都必须先运行
processor，再运行 08:30 Agent，并从安全 pending 输出记录三个 session ID。Agent
内部的 `confirm-report-memory` 成功后再运行只读 verifier；不得手工编辑 memory。

#### T+1 add acceptance

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap process-due --current-utc-date <T+1-next-UTC-date>
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap memory-pending --limit 18
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap report-reflection-pending --limit 18
T1_SESSION_IDS='<three-space-separated-session-ids-from-pending-output>'
run_scheduled_job_once_and_pause "$scheduled_review_memory_job_id" --accept-hooks
for session_id in $T1_SESSION_IDS; do verify_report_memory_stage "$session_id" 1; done
```

确认 Agent run 中每项 `confirm-report-memory` 成功，verifier 每行均为
`marker_occurrences: 1`、`exact_content_occurrences: 1` 和
`index_matches_latest_reflection: true`；此时每份报告只有一个 T+1 add 条目。

#### T+7 replace acceptance

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap process-due --current-utc-date <T+7-next-UTC-date>
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap report-reflection-pending --limit 18
run_scheduled_job_once_and_pause "$scheduled_review_memory_job_id" --accept-hooks
for session_id in $T1_SESSION_IDS; do verify_report_memory_stage "$session_id" 2; done
```

确认 Agent run 中每项 `confirm-report-memory` 成功，replace 后仍为
`marker_occurrences: 1`、`exact_content_occurrences: 1` 和
`index_matches_latest_reflection: true`，不存在第二个 Hermes memory 条目。

#### T+15 replace acceptance

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap process-due --current-utc-date <T+15-next-UTC-date>
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap report-reflection-pending --limit 18
run_scheduled_job_once_and_pause "$scheduled_review_memory_job_id" --accept-hooks
for session_id in $T1_SESSION_IDS; do verify_report_memory_stage "$session_id" 3; done
find /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/review_schedules -maxdepth 1 -type f -name '*.json' -printf '%m %f\n'
find /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/report_memories -maxdepth 1 -type f -name '*.json' -printf '%m %f\n'
```

确认 Agent run 中每项 `confirm-report-memory` 成功，最终仍为
`marker_occurrences: 1`、`exact_content_occurrences: 1` 和
`index_matches_latest_reflection: true`，即每份报告 `只有一个 Hermes memory 条目`。
任一阶段失败都保持新旧 jobs paused，保留 artifact 调查，不得进入下一阶段或移除旧
jobs。验收输出只能包含 ID、symbol、revision、state、count 和安全错误码，不能包含
evidence 或 memory 正文。

`unavailable_count` 及其最多 18 个 ID 样本不得触发 memory mutation。只有 Hermes
memory tool 返回 ambiguous、unaccepted 或与 action 不匹配的结果时，才调用
`quarantine-report-memory`；`--error-code` 必须来自代码中的 `MEMORY_ERROR_CODES`，
不明确的返回使用 `REPORT_MEMORY_RESULT_AMBIGUOUS`。

`confirm-report-memory` 的 verifier 若发现 marker 缺失/重复、精确内容或项目 index
不一致，会先更新 revision；返回安全失败状态时已持久化 `attention_required`。此时
只报告安全状态并由 operator 调查，不得再次调用 `quarantine-report-memory`，也
不得用 shell 修复。若确认已把状态持久化为 `verification_pending` 后进程崩溃，
恢复时直接再次调用 `confirm-report-memory`，不得执行第二次 Hermes mutation。

全部旧 v1 与新 v2 验收通过后，才移除仍为 paused 的旧 job，然后恢复两个
replacement job：

```bash
hermes cron remove "$old_process_job_id"
hermes cron remove "$old_memory_job_id"
hermes cron resume "$scheduled_review_process_job_id"
hermes cron resume "$scheduled_review_memory_job_id"
hermes cron status
```

回滚只暂停 replacement job，不删除数据：

```bash
hermes cron pause "$scheduled_review_process_job_id"
hermes cron pause "$scheduled_review_memory_job_id"
hermes cron list --all
```

若旧 job 尚未移除，确认 replacements 已暂停后可恢复旧 v1 job；若旧 job 已在验收
成功后移除，则部署上一个已评审版本并重新创建其 paused jobs，验证后再恢复。不要
同时运行旧、新两组 job，也不要通过删除 artifact 回滚。

确认不再产生新 run 后，原样保留项目 sessions、reviews、learning indexes、
`report_batches`、`report_memories/<session_id>.json`、reports、logs、schedules 和
Hermes memory 供审计。不得删除 artifact 或编辑 memory 文件。本部署不新增 API
key，不访问交易所、不真实下单，也不发送外部消息。

## 会话存储和故障处理

所有分析会话均以 schema 版本 1 的 JSON 文件持久化到 `/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions`。后台 worker 的标准输出和错误输出保存到同级的 `results/hermes/logs`，其中 `.log` 文件由 `tradingagents-hermes-maintenance.timer` 按 14 天保留期清理。确定性复盘记录保存在 `results/hermes/reviews`，每个币种的全部复盘学习项持久化到 `results/hermes/memories/<SYMBOL>.json`，后续同币种分析最多加载最近 5 条。会话、复盘、学习索引和 Hermes 长期 memory 永不由维护任务删除。

| 错误代码 | 操作员处理 |
| --- | --- |
| `INVALID_REQUEST` | 更正必填分析字段、提供商、模型、交易对或日期后重试。 |
| `MISSING_API_KEY` | 更新私有 Hermes 配置中 `mcp_servers.tradingagents_crypto.env` 的必需值，保持配置权限为 `600`，然后使用新的交易日期重试。无 agent bootstrap 会在每次运行前读取该白名单；不得写入项目 `.env` 或创建第二份密钥文件。 |
| `SESSION_STORE_UNAVAILABLE` | 检查 `TRADINGAGENTS_RESULTS_DIR`、目录所有者、可用空间及结果目录是否可创建。 |
| `SESSION_WRITE_FAILED` | 检查 `results/hermes/sessions` 的写权限和存储健康状况，然后重试。 |
| `INVALID_SESSION_ID` | 使用 `analyze_crypto` 返回的不透明 `hermes_<hex>` 会话 ID；不得手工猜测或修改该 ID。 |
| `SESSION_NOT_FOUND` | 核对分析工具返回的不透明 `session_id`，或启动新的分析。 |
| `SESSION_UNREADABLE` | 保留会话文件，检查文件系统健康状况和文件权限，然后重试或创建新会话。 |
| `WORKER_START_FAILED` | 检查 MCP Python 环境、`results/hermes` 的写权限和可执行文件路径，然后创建新的分析请求。 |
| `WORKER_EXITED` | 后台 worker 在完成前退出；保留会话日志，检查数据或模型提供商后创建新的分析请求。定时维护和 `get_analysis_result` 都会执行这一相同判定。 |
| `ANALYSIS_FAILED` | 查看安全的工具错误、提供商或数据可用性及模型请求，稍后重试。 |
| `SESSION_NOT_COMPLETED` | 仅已完成的分析可复盘；失败、排队中或运行中的会话不能复盘。 |
| `INVALID_REVIEW_REQUEST` | 使用 `analyze_crypto` 返回的会话 ID，并选择晚于原 `trade_date`、不晚于当前 UTC 日期的 ISO `review_date`。 |
| `PRICE_DATA_UNAVAILABLE` | CoinGecko、CryptoCompare 和 Coinbase 都未能提供同一来源的精确历史 USD 价格。不得以实时价、其他日期或混合来源替代；稍后重试并检查私有 Hermes 配置中的可选数据提供商密钥。 |
| `REVIEW_STORE_UNAVAILABLE` | 检查 `results/hermes/reviews`、`results/hermes/memories` 的所有者、可用空间与 `TRADINGAGENTS_RESULTS_DIR`。 |
| `REVIEW_WRITE_FAILED` | 保留已有文件，检查复盘目录写权限后重试。 |
| `LEARNING_WRITE_FAILED` | 规范复盘可能已保存但学习索引未更新；检查学习目录写权限，然后以相同 `session_id` 和 `review_date` 重试以修复索引。 |
| `INVALID_REPORT_REQUEST` | 使用 ISO `trade_date`、唯一支持币种和分析师、合法模型配置；不要传入额外字段。 |
| `REPORT_BATCH_NOT_FOUND` | 先运行提交 job，或核对所用日期是否与批次日期一致。 |
| `REPORT_BATCH_CONFLICT` | 同一日期已存在不同配置的批次；使用既有配置或选择新的日期，不得删除旧批次重建。 |
| `REPORT_BATCH_ACTIVE` | 等待所有 session 终态后再归档；禁止创建部分报告。 |
| `REPORT_BATCH_UNREADABLE` | 保留 batch/session 文件，检查 `report_batches`、`sessions` 目录权限和文件系统健康状况。 |
| `REPORT_ARCHIVE_INVALID` | 使用非空且不超过 20,000 字符的报告 narrative，日期必须是 ISO 格式。 |
| `REPORT_ARCHIVE_CONFLICT` | 历史报告不可变；保留已有归档，不得尝试覆盖。 |

## 静态校验

本手册为面向操作员的 UTF-8 中文文档，不执行 ASCII 源码检查。提交前在项目根目录运行以下校验：

```bash
set -e
git diff --check
local_path_pattern="$(printf '%s' '/User' 's/|local' 'host:|0\.0\.0\.0')"
if rg -n "$local_path_pattern" docs/hermes_integration.md; then exit 1; fi
secret_pattern="$(printf '%s' 'API_KEY: ' '"' '[^<][^"]*"')"
if rg -n "$secret_pattern" docs/hermes_integration.md; then exit 1; fi
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python - <<'PY'
from pathlib import Path
import re
import yaml

text = Path("docs/hermes_integration.md").read_text(encoding="utf-8")
config = re.search(r"\`\`\`yaml\n(.*?)\n\`\`\`", text, re.S).group(1)
assert isinstance(yaml.safe_load(config), dict)
PY
```

## 回滚

如需禁用 Hermes 访问，仅从 `/home/ubuntu/.hermes/config.yaml` 删除 `tradingagents_crypto` 条目，保持该文件权限为 `600`，然后在 Hermes 中执行 `/reload-mcp`。这不会改变 Web UI、打开或关闭网络端口、修改 nginx、删除 `.venv-hermes-mcp`，也不会删除已持久化的会话、复盘、学习索引或 worker 日志。
