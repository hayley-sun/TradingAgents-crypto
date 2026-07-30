#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=/home/ubuntu/workspace/TradingAgents-crypto
exec "$PROJECT_DIR/.venv-hermes-mcp/bin/python" -m tradingagents.integrations.hermes_daily_report_runner archive "$@"
