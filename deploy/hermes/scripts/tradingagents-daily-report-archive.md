这个脚本是每天 12:00 执行的日度研究报告归档器。它读取 08:00 提交的 BTC、ETH、SOL 分析结果，在条件满足时生成当天的 Markdown 报告。

脚本本身只是调用 Python 的包装器，见 deploy/hermes/scripts/tradingagents-daily-report-archive.sh:1：

python -m tradingagents.integrations.hermes_daily_report_bootstrap archive

实际流程是：

12:00 Hermes Cron
→ 找到当天的 report batch
→ 读取 BTC、ETH、SOL 三个 session
→ 判断任务状态
→ 生成并归档 Markdown 报告

有三种情况：

- 还有任务处于 queued/running

  返回 state: "active"，不会生成半成品报告。

- 三个任务全部成功

  状态为 ready，生成：

  results/hermes/reports/<YYYY-MM-DD>.md

- 所有任务都已结束，但部分失败

  状态为 degraded，仍然生成报告，并在对应币种中记录错误，不会自动重试。

报告内容包括：

- 当天批次配置
- BTC、ETH、SOL 各自的分析状态
- processed_signal 处理后的信号
- final_trade_decision 模拟交易决策
- 上一份已归档报告的结果对照
- 研究、模拟交易和风险声明

生成逻辑见 tradingagents/integrations/hermes_daily_report_runner.py:101。

这个归档过程是确定性的，不再调用 LLM。报告还会计算 SHA-256，同一天已经归档后：

- 内容相同：直接返回已有报告
- 内容不同：返回 REPORT_ARCHIVE_CONFLICT，不会覆盖历史报告

相关保护见 tradingagents/integrations/hermes_reports.py:342。

一个重要行为是：如果 12:00 执行时分析尚未完成，它只返回 active，当天不会自动再次尝试。需要之后手动指定日期重新归档，例如：

tradingagents-daily-report-archive.sh --trade-date 2026-08-05

它不会真实下单，也不会把报告发送到 Telegram、邮件或其他外部渠道。

/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/reports/<上海时区当天日期>.md

例如当天是 2026-08-05：

/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/reports/2026-08-05.md

同时它会更新对应的批次文件，写入归档状态、文件名和 SHA-256：

/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/report_batches/2026-08-05.json

需要注意：

- 如果 BTC、ETH、SOL 仍有任务处于 queued/running，脚本只输出 state: active，不会创建 Markdown 文件。
- 如果配置了 TRADINGAGENTS_RESULTS_DIR，上述 results 根目录会替换为该配置值。
- 脚本还会向 Hermes Cron 输出一行 JSON，但这只是运行结果，不是单独的报告文件。
