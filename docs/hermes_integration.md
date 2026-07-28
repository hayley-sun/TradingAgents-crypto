# Hermes MCP 云主机运行手册

## 范围和安全边界

本手册在云主机上将 `TradingAgents Crypto` MCP 服务器作为本地 Hermes stdio 子进程部署。所有工具结果仅用于研究和模拟交易，严禁据此下达真实订单。本部署不会引入交易所私钥、真实下单、公共 HTTP MCP 端点、新端口或 nginx 变更。

公开 Web UI 保持为 `http://124.222.79.66/`。MCP 仅在本机进程边界内运行，并与 Web UI 分离。

主机前提：

- 主机：`124.222.79.66`
- 项目：`/home/ubuntu/workspace/TradingAgents-crypto`
- Hermes 配置：`/home/ubuntu/.hermes/config.yaml`
- MCP 会话目录：`/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions`
- 会话 schema 版本：`1`

不得提交密钥、在本文档中写入真实密钥，或经 nginx、公共 URL、日志或 shell 历史暴露密钥。

## 部署专用虚拟环境

`MCP SDK` 需要 `Python 3.10` 或更新版本。先检查主机解释器：

```bash
ssh ubuntu@124.222.79.66
python3 --version
python3 -c "import sys; assert sys.version_info >= (3, 10), sys.version"
git_version="$(git --version)"
python3 -c 'import re, sys; raw = sys.argv[1]; match = re.search(r"([0-9]+)[.]([0-9]+)(?:[.]([0-9]+))?", raw); version = tuple(int(part or 0) for part in match.groups()) if match else (); sys.exit(0 if version >= (2, 29, 0) else f"Git 2.29+ is required; found: {raw}")' "$git_version"
```

部署需要 Git `2.29` 或更新版本，以支持所用的 fetch 安全选项；版本字符串后的发行版后缀会被忽略。

部署前，工作树必须干净。将下方两个占位值分别替换为已评审的完整 40 位十六进制 Phase 1 提交 SHA，以及包含该提交、已推送到 `origin` 的远程跟踪引用。远程引用只能使用 canonical `refs/remotes/origin/*` 形式，且必须是有效 Git 引用名；修订别名和表达式均会被拒绝。提交 SHA 本身必须标识一个 commit 对象。不得使用未评审分支、强制检出、重置或丢弃本地改动。该流程会分离检出指定提交，不会修改现有 Web `.venv`。

```bash
cd /home/ubuntu/workspace/TradingAgents-crypto
set -e
working_tree_status="$(git status --porcelain=v1 --untracked-files=all)"
test -z "$working_tree_status" || { echo "working tree must be clean" >&2; exit 1; }
reviewed_phase1_commit="<replace-with-full-40-hex-reviewed-phase-1-commit>"
# Recommended ref: refs/remotes/origin/feature/hermes-mcp-phase-1
reviewed_phase1_ref="<replace-with-refs-remotes-origin-tracking-ref>"
case "$reviewed_phase1_commit" in
  *[!0-9A-Fa-f]*|'') echo "reviewed_phase1_commit must be a full 40-character hexadecimal SHA" >&2; exit 1 ;;
esac
[ "${#reviewed_phase1_commit}" -eq 40 ] || { echo "reviewed_phase1_commit must be a full 40-character hexadecimal SHA" >&2; exit 1; }
reviewed_phase1_commit="$(printf '%s' "$reviewed_phase1_commit" | tr 'A-F' 'a-f')"
case "$reviewed_phase1_ref" in
  refs/remotes/origin/*) ;;
  *) echo "reviewed_phase1_ref must be an origin remote-tracking ref" >&2; exit 1 ;;
esac
git check-ref-format "$reviewed_phase1_ref" || { echo "reviewed_phase1_ref must be a valid origin remote-tracking ref" >&2; exit 1; }
remote_branch="${reviewed_phase1_ref#refs/remotes/origin/}"
git ls-remote --exit-code --heads origin "refs/heads/$remote_branch" >/dev/null || { echo "reviewed_phase1_ref is not present on origin" >&2; exit 1; }
git -c fetch.prune=false -c fetch.pruneTags=false \
  -c remote.origin.prune=false -c remote.origin.pruneTags=false \
  -c fetch.recurseSubmodules=false -c fetch.writeCommitGraph=false \
  -c maintenance.auto=false \
  fetch --no-prune --no-tags --no-write-fetch-head \
  --no-recurse-submodules --no-write-commit-graph --no-auto-maintenance \
  origin "+refs/heads/$remote_branch:$reviewed_phase1_ref"
git rev-parse --verify "$reviewed_phase1_ref^{commit}"
git cat-file -e "$reviewed_phase1_commit"
test "$(git cat-file -t "$reviewed_phase1_commit")" = "commit" || { echo "reviewed_phase1_commit must identify a commit object" >&2; exit 1; }
git merge-base --is-ancestor "$reviewed_phase1_commit" "$reviewed_phase1_ref" || { echo "reviewed commit is not reachable from $reviewed_phase1_ref" >&2; exit 1; }
git -c core.hooksPath=/dev/null -c submodule.recurse=false \
  switch --no-overwrite-ignore --no-recurse-submodules --detach "$reviewed_phase1_commit"
test "$(git rev-parse --verify HEAD)" = "$reviewed_phase1_commit" || { echo "HEAD does not match reviewed_phase1_commit" >&2; exit 1; }
test -z "$(git symbolic-ref -q HEAD || true)" || { echo "HEAD must be detached" >&2; exit 1; }
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

`set -e` 确保仅在所有安装和验证成功后才执行 `rm -- "$requirements_file"`；任一步骤失败都会保留每次运行独有的临时文件以便诊断。仅当 `git status --porcelain=v1 --untracked-files=all` 产生空结果时才可继续，该检查不受隐藏未跟踪文件的用户配置影响。该门禁仍允许已忽略的运行时文件存在，例如持久化的 `.venv-hermes-mcp`；但 `git switch --no-overwrite-ignore` 会在新跟踪文件将覆盖已忽略本地文件时失败。检出命令以 `core.hooksPath=/dev/null` 禁用仓库配置的 post-checkout hooks，并以 `submodule.recurse=false` 和 `--no-recurse-submodules` 禁止任何仓库配置驱动的子模块工作树更新；随后同时确认 `HEAD` 精确等于已评审 SHA 且不指向分支。操作员必须将 `reviewed_phase1_commit` 替换为完整 40 位十六进制已评审提交 SHA，而不是分支、标签或其他修订别名；大写十六进制字符可接受，流程会在任何 Git 检查或 `HEAD` 比较前将其规范化为小写。该 SHA 本身必须标识 commit 对象。并将 `reviewed_phase1_ref` 替换为包含该提交的 canonical `refs/remotes/origin/*` 远程跟踪引用；只接受有效 Git 引用名，修订别名和表达式会被拒绝。流程会从该引用导出分支名，先确认该分支当前存在于 `origin`，再以明确 refspec 将该远程分支拉取到选定跟踪引用。该命令同时禁用 `fetch.*`、`remote.origin.*` 两层 prune 与 pruneTags 设置、子模块递归、commit-graph 写入及自动维护，并显式使用 `--no-prune --no-tags --no-write-fetch-head --no-recurse-submodules --no-write-commit-graph --no-auto-maintenance`。在显式分离检出前，除为选定远程跟踪引用取得所需对象并刻意刷新该引用外，不会修改工作树、本地分支、本地标签、无关引用或 `.git/FETCH_HEAD`，也不会递归获取子模块、写入 commit-graph 或运行维护。前导 `+` 仅用于允许该远程跟踪引用被当前 `origin` 头部非快进覆盖。这不依赖 `remote.origin.fetch`。随后会验证远程引用、精确 SHA 对象类型及该提交从该引用的可达性。格式不符、本地伪造或滞后的跟踪引用不会通过，因为选定引用会由当前 `origin` 头部刷新；未推送或无法从已刷新引用到达的提交对象也会失败，不会执行检出。

`requirements_hermes.txt` 将 MCP 精确固定为已验证的 `mcp==1.28.1`，避免兼容范围引入未经验证的 FastMCP 行为变化。该版本需要 AnyIO 4 或更新版本。可选 `chainlit` 依赖为 Chainlit `1.1.202`，其 `asyncer` 约束 AnyIO 低于 4。将 MCP 安装到现有项目 `.venv` 会破坏 `pip check` 和 FastAPI 构造。仅在 `.venv-hermes-mcp` 中排除精确的 `chainlit` 行可解决已验证的冲突；Web `.venv` 不作任何改动，继续保留 Chainlit。

## Hermes 配置

以下命令可在全新主机上创建所需的受限目录。Hermes 必须以 `ubuntu` 用户运行；若实际服务用户不同，必须由该服务用户拥有这些目录和配置文件。

```bash
install -d -m 700 /home/ubuntu/.hermes
install -d -m 700 /home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions
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
    timeout: 900
    connect_timeout: 60
```

仅设置当前活动 LLM 提供商的密钥，并删除其余 LLM 密钥项。DeepSeek 使用 `DEEPSEEK_API_KEY`；可选替代项为 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GOOGLE_API_KEY` 和 `OPENROUTER_API_KEY`。数据提供商使用 `FINNHUB_API_KEY`。CoinGecko 为可选项，可使用 `COINGECKO_DEMO_API_KEY` 或 `COINGECKO_PRO_API_KEY`。所有值只能保存在权限为 `600` 的 Hermes 配置中，绝不可写入仓库。

不得加入 `cwd`、工具 include 列表或其他未经验证的字段。这是 stdio 配置，不是 HTTP 服务。

严禁在终端手工运行 `/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_mcp`。它是仅由 Hermes 启动的 stdio 协议子进程，手工启动不是健康检查；请仅使用下方的 `health_check` 工具验证。

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
```

健康检查提示：

> 请调用 mcp__tradingagents_crypto__health_check，确认 session_store_writable 为 true，并报告当前配置的密钥环境变量是否已设置且非空。不要输出任何密钥值；此检查不验证提供商认证或密钥是否实际可用。

首次使用下列有效的浅层 BTC 分析请求。它明确选择 DeepSeek，只能作为小范围研究请求：

> 请调用 mcp__tradingagents_crypto__analyze_crypto 对 BTC 进行浅层研究分析。使用 trade_date=2026-07-28，analysts=["market", "news"]，llm_provider=deepseek，quick_model=deepseek-v4-flash，deep_model=deepseek-v4-pro，research_depth=1。这是研究和模拟交易，不得提交真实交易或下单。

Phase 1 的 `analyze_crypto` 为同步、串行操作，可能需要数分钟；Hermes 超时为 `900` 秒。不得为了解决超时而提高并发度或并行发起分析。

记录返回的 `session_id`，再用以下中文提示读取结果：

> 请调用 mcp__tradingagents_crypto__get_analysis_result，使用会话 ID `<session_id>` 取回中文分析结果。请明确说明这些结果仅用于研究和模拟交易。

## 会话存储和故障处理

成功和失败的分析会话均以 schema 版本 1 的 JSON 文件持久化到 `/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions`。正常回滚和事件排查期间必须保留该目录。

| 错误代码 | 操作员处理 |
| --- | --- |
| `INVALID_REQUEST` | 更正必填分析字段、提供商、模型、交易对或日期后重试。 |
| `MISSING_API_KEY` | 仅向私有 Hermes 配置添加已选提供商的真实密钥，然后重载 MCP。 |
| `SESSION_STORE_UNAVAILABLE` | 检查 `TRADINGAGENTS_RESULTS_DIR`、目录所有者、可用空间及结果目录是否可创建。 |
| `SESSION_WRITE_FAILED` | 检查 `results/hermes/sessions` 的写权限和存储健康状况，然后重试。 |
| `INVALID_SESSION_ID` | 使用 `analyze_crypto` 返回的不透明 `hermes_<hex>` 会话 ID；不得手工猜测或修改该 ID。 |
| `SESSION_NOT_FOUND` | 核对分析工具返回的不透明 `session_id`，或启动新的分析。 |
| `SESSION_UNREADABLE` | 保留会话文件，检查文件系统健康状况和文件权限，然后重试或创建新会话。 |
| 工具超时（`900` 秒） | 不得自动或立即重试。先检查 Hermes/MCP 进程与提供商/数据可用性，确认原分析已不再运行后，才能有意识地提交新请求。可能尚未返回 `session_id`；此时不得使用 `get_analysis_result` 猜测或恢复该请求，也不得提高并发度或并行分析。 |
| `ANALYSIS_FAILED` | 工具已返回该错误时，查看安全的工具错误、提供商或数据可用性，稍后重试；不得提高并发度或并行分析。 |

## 静态校验

本手册为面向操作员的 UTF-8 中文文档，不执行 ASCII 源码检查。提交前在项目根目录运行以下校验：

```bash
set -e
git diff --check HEAD -- docs/hermes_integration.md
local_path_pattern="$(printf '%s' '/User' 's/|local' 'host:|0\.0\.0\.0')"
if rg -n "$local_path_pattern" docs/hermes_integration.md; then exit 1; fi
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python - <<'PY'
from pathlib import Path
import re
import yaml

text = Path("docs/hermes_integration.md").read_text(encoding="utf-8")

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

yaml_blocks = re.findall(
    r"(?ims)^\`\`\`(?:yaml|yml)[ \t]*\r?\n(.*?)^\`\`\`[ \t]*$",
    text,
)
require(bool(yaml_blocks), "expected at least one YAML or YML code block")

class UniqueKeyLoader(yaml.SafeLoader):
    pass

def construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise RuntimeError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping

UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)

configs = [yaml.load(block, Loader=UniqueKeyLoader) for block in yaml_blocks]

def strip_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value

def is_placeholder(value):
    value = strip_quotes(value.strip())
    return value.startswith("<") and value.endswith(">")

assignment_pattern = re.compile(
    r"(?im)\b(?:[A-Za-z][A-Za-z0-9_-]*(?:api[_-]?key|token|secret|"
    r"password|passwd|credential|authorization)|api[_-]?key|token|secret|"
    r"password|passwd|credential|authorization)\b\s*[:=]\s*(?P<value>[^\s#\x60]+)"
)
for match in assignment_pattern.finditer(text):
    require(is_placeholder(match.group("value")), "raw credential assignment is not a placeholder")

bearer_pattern = re.compile(r"(?i)\bbearer\s+(?P<value>[^\s#\x60]+)")
for match in bearer_pattern.finditer(text):
    require(is_placeholder(match.group("value")), "authorization credential is not a placeholder")

basic_pattern = re.compile(r"(?i)\bbasic\s+(?P<value>[^\s#\x60]+)")
for match in basic_pattern.finditer(text):
    require(is_placeholder(match.group("value")), "HTTP authorization value is not a placeholder")

for match in re.finditer(r"(?i)(?P<value><sk-[A-Za-z0-9_-]{8,}>|sk-[A-Za-z0-9_-]{8,})", text):
    require(is_placeholder(match.group("value")), "OpenAI-style credential is not a placeholder")

def validate_api_keys(value):
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if isinstance(key, str) and key.endswith("_API_KEY"):
                require(
                    isinstance(nested_value, str)
                    and nested_value.startswith("<")
                    and nested_value.endswith(">"),
                    f"{key} must use an angle-bracket placeholder",
                )
            validate_api_keys(nested_value)
    elif isinstance(value, list):
        for item in value:
            validate_api_keys(item)

for config in configs:
    validate_api_keys(config)

target_occurrences = []

def collect_target_occurrences(value):
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if key == "mcp_servers" and isinstance(nested_value, dict):
                if "tradingagents_crypto" in nested_value:
                    target_occurrences.append(nested_value["tradingagents_crypto"])
            collect_target_occurrences(nested_value)
    elif isinstance(value, list):
        for item in value:
            collect_target_occurrences(item)

for config in configs:
    collect_target_occurrences(config)

require(len(target_occurrences) == 1, "expected exactly one tradingagents_crypto MCP server")
mcp_config = target_occurrences[0]
require(isinstance(mcp_config, dict), "tradingagents_crypto MCP server must be a mapping")
require(
    set(mcp_config) == {"command", "args", "env", "timeout", "connect_timeout"},
    "tradingagents_crypto MCP server has unsupported or missing fields",
)
require(mcp_config["command"] == (
    "/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python"
), "tradingagents_crypto command path is invalid")
require(mcp_config["args"] == ["-m", "tradingagents.integrations.hermes_mcp"], "tradingagents_crypto args are invalid")
require(mcp_config["timeout"] == 900, "tradingagents_crypto timeout is invalid")
require(mcp_config["connect_timeout"] == 60, "tradingagents_crypto connect_timeout is invalid")
env = mcp_config["env"]
require(isinstance(env, dict), "tradingagents_crypto env must be a mapping")
require(env == {
    "PYTHONPATH": "/home/ubuntu/workspace/TradingAgents-crypto",
    "TRADINGAGENTS_RESULTS_DIR": "/home/ubuntu/workspace/TradingAgents-crypto/results",
    "DEEPSEEK_API_KEY": "<replace-with-real-deepseek-secret-or-remove>",
    "FINNHUB_API_KEY": "<replace-with-real-finnhub-secret-or-remove>",
    "COINGECKO_DEMO_API_KEY": "<optional-replace-with-real-coingecko-secret-or-remove>",
}, "tradingagents_crypto env sample is invalid")
PY
```

## 回滚

如需禁用 Hermes 访问，仅从 `/home/ubuntu/.hermes/config.yaml` 删除 `tradingagents_crypto` 条目，保持该文件权限为 `600`，然后在 Hermes 中执行 `/reload-mcp`。这不会改变 Web UI、打开或关闭网络端口、修改 nginx、删除 `.venv-hermes-mcp`，也不会删除已持久化会话。
