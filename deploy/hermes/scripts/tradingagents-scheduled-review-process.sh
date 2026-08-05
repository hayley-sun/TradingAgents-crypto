#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=/home/ubuntu/workspace/TradingAgents-crypto
exec "$PROJECT_DIR/.venv-hermes-mcp/bin/python" -m tradingagents.integrations.hermes_scheduled_review_bootstrap process-due "$@"
