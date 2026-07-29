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

仅设置当前活动 LLM 提供商的密钥，并删除其余 LLM 密钥项。DeepSeek 使用 `DEEPSEEK_API_KEY`；可选替代项为 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GOOGLE_API_KEY` 和 `OPENROUTER_API_KEY`。数据提供商使用 `FINNHUB_API_KEY`。CoinGecko 为可选项，可使用 `COINGECKO_DEMO_API_KEY` 或 `COINGECKO_PRO_API_KEY`。`CRYPTOCOMPARE_API_KEY` 仅作为历史价格 fallback 使用；它已在 Hermes 私有配置中时，不要再写入项目 `.env`、`crypto-trader.service` 或任何 systemd `EnvironmentFile`。所有值只能保存在权限为 `600` 的 Hermes 配置中，绝不可写入仓库。

不得加入 `cwd`、工具 include 列表或其他未经验证的字段。这是 stdio 配置，不是 HTTP 服务。

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

## 会话存储和故障处理

所有分析会话均以 schema 版本 1 的 JSON 文件持久化到 `/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions`。后台 worker 的标准输出和错误输出保存到同级的 `results/hermes/logs`，其中 `.log` 文件由 `tradingagents-hermes-maintenance.timer` 按 14 天保留期清理。确定性复盘记录保存在 `results/hermes/reviews`，每个币种最近 20 条学习项保存在 `results/hermes/memories/<SYMBOL>.json`；后续同币种分析最多加载最近 5 条。会话、复盘、学习索引和 Hermes 长期 memory 永不由维护任务删除。

| 错误代码 | 操作员处理 |
| --- | --- |
| `INVALID_REQUEST` | 更正必填分析字段、提供商、模型、交易对或日期后重试。 |
| `MISSING_API_KEY` | 仅向私有 Hermes 配置添加已选提供商的真实密钥，然后重载 MCP。 |
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
