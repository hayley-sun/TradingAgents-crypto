# Hermes Review Automation and Operations Design

## Goal

Make the existing paper-trading review loop repeatable without allowing the MCP
server to modify Hermes-owned memory, keep asynchronous session storage
operable over time, and make historical USD review prices resilient to a single
provider outage. The system remains research and paper trading only.

## Scope

This design has four coordinated parts:

1. A versioned, explicit Hermes skill that writes an accepted review lesson to
   built-in Hermes memory.
2. A read-only verifier that confirms the review file, project learning index,
   and Hermes memory agree.
3. A scheduled maintenance command that repairs demonstrably dead workers and
   prunes only expired worker logs.
4. A same-provider historical USD reference-price fallback chain.

No component places orders, creates a public endpoint, writes API keys to the
repository, or changes the existing paper-trading disclaimer.

## Explicit Hermes Review Skill

The source-controlled skill lives at:

```text
deploy/hermes/skills/tradingagents-paper-review/SKILL.md
```

The cloud deployment copies it to:

```text
/home/ubuntu/.hermes/skills/blockchain/tradingagents-paper-review/SKILL.md
```

It is invoked explicitly as `/tradingagents-paper-review`; an ordinary direct
call to `review_paper_decision` does not trigger a memory write. The skill
accepts a completed analysis `session_id` and a valid later `review_date`, then
performs this fixed sequence:

1. Call `mcp__tradingagents_crypto__review_paper_decision`.
2. Stop on a structured MCP error and do not write memory.
3. Take only `data.hermes_memory_entry` from a successful response.
4. Use Hermes's enabled `memory` tool to store that exact entry once. It must
   not use a terminal command to edit `MEMORY.md` directly.
5. Run the project verifier with the returned `review_id`.
6. Report the review ID and the three verification booleans, without printing
   a secret or treating the result as trading advice.

The MCP subprocess continues to own only `results/hermes`. It never reads or
writes `/home/ubuntu/.hermes/memories/MEMORY.md`.

## Review Consistency Verifier

`tradingagents.integrations.hermes_review_verifier` provides a small CLI:

```text
python -m tradingagents.integrations.hermes_review_verifier \
  --review-id review_<hex> \
  [--results-dir <path>] \
  [--hermes-memory-path <path>]
```

The defaults are the existing `TRADINGAGENTS_RESULTS_DIR` resolution and
`~/.hermes/memories/MEMORY.md`. The CLI validates the opaque review ID, loads
the immutable review through `ReviewStore`, and returns a JSON-safe report. It
exits zero only when all of the following are true:

- The canonical review file exists and validates.
- The matching symbol index contains the review ID.
- The review's exact `hermes_memory_entry` occurs exactly once in the provided
  Hermes memory file.

Missing, corrupt, ambiguous, or duplicate records produce a nonzero exit and
a concise error message without printing a lesson or a secret. Its underlying
verification function accepts explicit paths, so tests use temporary files and
do not touch a real Hermes profile.

## Session Maintenance and Retention

`tradingagents.integrations.hermes_maintenance` is a no-network CLI run with
the same Python environment as the MCP server. Its default results root is the
existing Hermes results root and its default log retention is 14 days.

For every valid persisted session in `queued` or `running` state:

- A session with a recorded PID that no longer exists becomes `failed` with the
  existing `WORKER_EXITED` error shape.
- A session without a PID is reported as untracked. It is not automatically
  failed, because the MCP launcher and serialized worker can briefly overlap.
- A queued session whose PID is alive remains queued, including while it waits
  behind the deliberate single-worker file lock.

The command deletes only regular or symlinked `results/hermes/logs/*.log`
entries whose modification time is more than 14 days old. It never deletes
sessions, reviews, learning indexes, or Hermes memory. It writes a compact
JSON report to stdout for the system journal and supports `--dry-run` for
operator inspection.

Two source-controlled unit templates deploy the command without secrets:

```text
deploy/systemd/tradingagents-hermes-maintenance.service
deploy/systemd/tradingagents-hermes-maintenance.timer
```

The one-shot service runs as `ubuntu` in the project directory. The timer runs
five minutes after boot and every 15 minutes thereafter with `Persistent=true`.
It reports through `journalctl -u tradingagents-hermes-maintenance.service`;
this iteration intentionally adds no external notification channel.

The existing `get_analysis_result` liveness repair and the maintenance command
share one worker-liveness reconciliation helper so their terminal state and
error code cannot diverge.

## Historical USD Reference Fallback

Reviews require two valid prices: one on the analysis trade date and one on the
later review date. A provider is usable only when it resolves both exact UTC
calendar dates. The chain never combines providers within one review:

1. CoinGecko: existing historical USD coin endpoint.
2. CryptoCompare: authenticated historical daily USD candles using
   `CRYPTOCOMPARE_API_KEY` from Hermes's private MCP environment.
3. Coinbase Exchange: public, direct `<SYMBOL>-USD` daily candles for symbols
   and dates the exchange exposes.

Coinbase replaces the previously considered USDT/USDC exchange fallback so the
field remains an actual USD reference rather than a stablecoin proxy. A missing
CryptoCompare key skips that provider. Any provider response missing an exact
date, containing a non-finite/non-positive price, or failing transport or
schema validation is rejected and the next provider is tried. If all providers
fail, the public MCP behavior remains `PRICE_DATA_UNAVAILABLE`.

`PriceReference.source` expands from `coingecko` to the explicit values
`coingecko`, `cryptocompare`, and `coinbase`. The immutable review records the
actual source for both prices. The lookup API returns the pair of references in
one call, which makes same-provider enforcement structural rather than a
best-effort convention.

The fallback module must not log request headers, API keys, URLs containing a
key, or raw third-party exceptions. `health_check` exposes only a boolean that
states whether a usable CryptoCompare key is configured.

## Testing and Deployment

All Python tests use temporary directories and mocked HTTP responses. They
cover the skill verifier's success, missing index, and duplicate-memory cases;
dead-PID repair and log-retention boundaries; provider ordering; same-provider
two-date selection; invalid fallback values; source persistence; and the
unchanged fail-closed error contract.

Cloud deployment installs the versioned skill and systemd templates after the
application branch is merged. It confirms `hermes memory status`, enables the
timer, executes one manual maintenance dry run, and uses an existing completed
paper review to confirm the skill's idempotent memory and three-store
verification behavior. The operator runbook documents the non-secret
CryptoCompare configuration already present in `/home/ubuntu/.hermes/config.yaml`.
