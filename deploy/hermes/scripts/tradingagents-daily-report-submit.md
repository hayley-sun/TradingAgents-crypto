这个脚本的作用是：每天 08:00 为 BTC 提交当天的异步研究任务。它只负责启动分析，不会等待分析完成，也不会在 08:00 直接生成或发送报告。

执行链路是：

Hermes Cron 08:00
→ tradingagents-daily-report-submit.sh
→ hermes_daily_report_bootstrap submit
→ hermes_daily_report_runner
→ 创建日度批次
→ 启动 BTC 后台分析 worker

脚本本身非常薄，见 deploy/hermes/scripts/tradingagents-daily-report-submit.sh:1：

PROJECT_DIR=/home/ubuntu/workspace/TradingAgents-crypto
exec "$PROJECT_DIR/.venv-hermes-mcp/bin/python" \
    -m tradingagents.integrations.hermes_daily_report_bootstrap submit "$@"

实际行为如下：

- 从 ~/.hermes/config.yaml 读取 TradingAgents 所需的白名单环境变量，包括 DeepSeek、Finnhub、CoinGecko 等 API Key，见 tradingagents/integrations/hermes_daily_report_bootstrap.py:11。
- 使用 Asia/Shanghai 当天日期作为 trade_date。
- 固定分析 BTC。
- 使用 market、news、fundamentals 三类分析师。
- 使用 DeepSeek：
  - 快速模型：deepseek-v4-flash
  - 深度模型：deepseek-v4-pro
  - 研究深度：1

固定配置见 tradingagents/integrations/hermes_daily_report_runner.py:24。

提交后会产生：

results/hermes/report_batches/<当天日期>.json
results/hermes/sessions/<BTC会话ID>.json
results/hermes/logs/<会话ID>.log

后台 worker 独立完成分析，08:00 的 Cron 不会一直等待它。提交命令只输出类似：

{
"ok": true,
"mode": "submit",
"trade_date": "2026-08-05",
"batch_id": "..."
}

同一天重复执行时，如果配置相同，会直接返回已经存在的批次，不会重复启动任务；如果配置不同，则返回 REPORT_BATCH_CONFLICT。

最终 Markdown 报告由另一个 12:00 Cron 脚本 tradingagents-daily-report-archive.sh 生成：

- BTC 任务还没结束：返回 active，不生成半成品报告。
- 全部结束：生成 results/hermes/reports/<日期>.md。
- 部分任务失败：仍生成 degraded 报告，但不会自动重试。

这个流程仅用于研究和模拟交易，不会真实下单，不会调用 Hermes agent，也不会通过 Telegram、邮件等渠道发送报告。
