# Hermes Feishu Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a signed Feishu group-bot notifier that reports new daily archives and new failures from the four existing Hermes production Cron jobs without changing those jobs' outcomes or project memory behavior.

**Architecture:** A fifth deterministic `--no-agent` Cron polls Hermes durable execution history and validates immutable report batches every five minutes. New events are persisted atomically before any outbound request, rendered as bounded signed Feishu cards, and retried independently with durable at-least-once semantics. A narrow bootstrap loads an owner-only YAML secret, while the notifier never reads Hermes `MEMORY.md`, raw execution errors, or external market data.

**Tech Stack:** Python 3.12, Pydantic v2, PyYAML, Requests, stdlib `fcntl`/`hashlib`/`hmac`/`subprocess`/`zoneinfo`, Bash, Hermes Agent Cron, `unittest`.

---

## Working Rules

At execution time, use `superpowers:using-git-worktrees` to create this exact worktree and branch:

```bash
git worktree add .worktrees/hermes-feishu-notifications -b feat/hermes-feishu-notifications
```

Run repository commands from:

```text
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.worktrees/hermes-feishu-notifications
```

Use this interpreter for all local tests:

```text
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python
```

Read the approved design first:

```bash
sed -n '1,320p' docs/superpowers/specs/2026-08-17-hermes-feishu-notifications-design.md
```

Never put a Webhook URL or signing secret in Git, command history, Cron arguments, fixtures, logs,
or commit messages. Never read, print, hash, or modify
`/home/ubuntu/.hermes/memories/MEMORY.md`. Preserve all existing reports, reviews, sessions,
schedules, indexes, journals, and memory artifacts.

## File Map

| File | Responsibility |
| --- | --- |
| `tradingagents/integrations/hermes_feishu_state.py` | Strict event/state models, non-blocking lock, atomic owner-only persistence, pruning, retries. |
| `tradingagents/integrations/hermes_feishu_client.py` | Config validation, redaction, Feishu signing/cards, bounded HTTPS transport. |
| `tradingagents/integrations/hermes_feishu_notifier.py` | Execution parser, report validation, discovery, baseline, delivery orchestration and CLI. |
| `tradingagents/integrations/hermes_feishu_bootstrap.py` | Owner/mode/symlink config checks, late runner import and safe failure envelope. |
| `tests/test_hermes_feishu_state.py` | State, locking, atomicity, permissions, pruning and retry tests. |
| `tests/test_hermes_feishu_client.py` | Config, signing, redaction, cards and transport tests. |
| `tests/test_hermes_feishu_notifier.py` | Execution/report discovery, initialization, orchestration, retry and CLI tests. |
| `deploy/hermes/scripts/tradingagents-feishu-notifier.sh` | Fixed owner-executable no-agent wrapper. |
| `tests/test_hermes_review_verifier.py` | Static deployment and runbook contracts. |
| `docs/hermes_integration.md` | Provisioning, baseline, paused acceptance, activation, observation and rollback. |

## Shared Runtime Contracts

Use these exact model fields throughout the tasks:

```python
EventKind = Literal["report", "execution_failure", "missing_archive"]

class NotificationEvent:
    event_id: str
    kind: EventKind
    created_at: datetime
    trade_date: date | None
    report_sha256: str | None
    batch_state: str | None
    job_name: str | None
    job_id: str | None
    execution_id: str | None

class DeliveryRecord:
    event: NotificationEvent
    attempt_count: int
    next_attempt_at: datetime
    delivered_at: datetime | None
    last_result: str | None

class NotificationState:
    schema_version: Literal[1]
    initialized_at: datetime
    execution_cursors: dict[str, str | None]
    seen_execution_ids: dict[str, list[str]]
    seen_report_event_ids: list[str]
    deliveries: dict[str, DeliveryRecord]

@dataclass(frozen=True)
class ReportCardItem:
    symbol: str
    status: str
    processed_signal: str | None
    final_trade_decision: str | None
    error_code: str | None

@dataclass(frozen=True)
class ReportCardData:
    event_id: str
    trade_date: date
    state: str
    items: tuple[ReportCardItem, ...]
    report_path: Path
```

The private YAML contains exactly `version`, `webhook_url`, `signing_secret`, and a `jobs`
mapping whose keys are `daily_submit`, `daily_archive`, `review_processor`, and `review_memory`.

### Task 1: Add Strict Notification State And Atomic Storage

**Files:**
- Create: `tradingagents/integrations/hermes_feishu_state.py`
- Create: `tests/test_hermes_feishu_state.py`

- [ ] **Step 1: Write failing state, atomic-write and pruning tests**

Create a `FeishuNotificationStateTests` class with this fixture and core cases:

```python
def report_event(event_id="report:2026-08-18:" + "a" * 64):
    return NotificationEvent(
        event_id=event_id,
        kind="report",
        created_at=datetime(2026, 8, 18, 4, 0, tzinfo=timezone.utc),
        trade_date=date(2026, 8, 18),
        report_sha256="a" * 64,
        batch_state="ready",
    )


def test_state_rejects_event_with_wrong_fields(self):
    with self.assertRaises(ValidationError):
        NotificationEvent(
            event_id="bad",
            kind="execution_failure",
            created_at=datetime.now(timezone.utc),
            trade_date=date(2026, 8, 18),
        )


def test_store_writes_mode_600_and_round_trips(self):
    with TemporaryDirectory() as directory:
        store = NotificationStateStore(Path(directory) / "feishu_notifications")
        state = initialized_state(
            datetime(2026, 8, 18, tzinfo=timezone.utc), {"a" * 12: []}, []
        )
        store.save(state)
        self.assertEqual(store.load(), state)
        self.assertEqual(store.load_optional(), state)
        self.assertEqual(S_IMODE(store.path.stat().st_mode), 0o600)


def test_atomic_failure_preserves_valid_bytes(self):
    with TemporaryDirectory() as directory:
        store = NotificationStateStore(Path(directory) / "feishu_notifications")
        state = initialized_state(
            datetime(2026, 8, 18, tzinfo=timezone.utc), {"a" * 12: []}, []
        )
        store.save(state)
        before = store.path.read_bytes()
        with patch("os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(NotificationStateError):
                store.save(state.model_copy(update={"seen_report_event_ids": ["new"]}))
        self.assertEqual(store.path.read_bytes(), before)


def test_prune_keeps_compact_ids_and_pending_records(self):
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    old = DeliveryRecord(
        event=report_event("old"), attempt_count=1, next_attempt_at=now,
        delivered_at=now - timedelta(days=91), last_result="delivered",
    )
    pending = DeliveryRecord(event=report_event("pending"), next_attempt_at=now)
    state = initialized_state(now, {"a" * 12: []}, ["old", "pending"])
    state = state.model_copy(update={"deliveries": {"old": old, "pending": pending}})

    pruned = prune_delivered(state, now)

    self.assertNotIn("old", pruned.deliveries)
    self.assertIn("old", pruned.seen_report_event_ids)
    self.assertIn("pending", pruned.deliveries)
```

Also test a second `store.lock()` raises `NotificationAlreadyRunning`, malformed JSON is rejected
without rewrite, and `retry_delay(1..6)` returns 5, 10, 20, 40, 60, 60 minutes.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_state -v
```

Expected: import failure because `hermes_feishu_state` does not exist.

- [ ] **Step 3: Implement models, retry policy and store**

Use frozen Pydantic models with `ConfigDict(extra="forbid")`. A model validator enforces these
exact field groups:

```python
REQUIRED_FIELDS = {
    "report": {"trade_date", "report_sha256", "batch_state"},
    "execution_failure": {"job_name", "job_id", "execution_id"},
    "missing_archive": {"trade_date", "batch_state", "job_name", "job_id", "execution_id"},
}
RETRY_MINUTES = (5, 10, 20, 40, 60)


def retry_delay(attempt_count: int) -> timedelta:
    if attempt_count < 1:
        raise ValueError("attempt count must be positive")
    return timedelta(minutes=RETRY_MINUTES[min(attempt_count - 1, 4)])


def initialized_state(now, execution_ids, report_event_ids):
    return NotificationState(
        initialized_at=now,
        execution_cursors={job: ids[0] if ids else None for job, ids in execution_ids.items()},
        seen_execution_ids=execution_ids,
        seen_report_event_ids=sorted(set(report_event_ids)),
        deliveries={},
    )
```

Store state at `<root>/state.json`, lock at `<root>/.state.lock`, create the root mode `700`, and
use `fcntl.flock(LOCK_EX | LOCK_NB)`. Write ASCII JSON via `NamedTemporaryFile`, `flush`, `fsync`,
`os.replace`, and mode `600`. Convert read/validation/write failures to a constant safe
`NotificationStateError`. `load_optional()` returns `None` only when `state.json` does not exist;
an unreadable or invalid existing file still raises. Prune only delivered metadata older than 90 days; never prune compact
report IDs, cursors, seen execution IDs, or pending deliveries.

- [ ] **Step 4: Run tests and verify GREEN**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_state -v
```

Expected: all state tests pass.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_feishu_state.py tests/test_hermes_feishu_state.py
git commit -m "feat: add durable Feishu notification state"
```

### Task 2: Validate Private Configuration And Feishu Signatures

**Files:**
- Create: `tradingagents/integrations/hermes_feishu_client.py`
- Create: `tests/test_hermes_feishu_client.py`

- [ ] **Step 1: Write failing config, permissions, URL and signing tests**

```python
VALID_JOBS = {
    "daily_submit": "2d445dfc1a8a",
    "daily_archive": "5b7f7906306a",
    "review_processor": "d6c0e087e5a8",
    "review_memory": "e93cfab5f78e",
}


def config_payload():
    return {
        "version": 1,
        "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000",
        "signing_secret": "unit-test-signing-secret",
        "jobs": VALID_JOBS,
    }


def test_signature_matches_fixed_vector(self):
    self.assertEqual(
        feishu_signature(1599360473, "test-secret"),
        "wSds2BzzFIIGf/WrhUO+NI1q/9j+FRJd3JNHKAq0NZY=",
    )


def test_config_rejects_non_feishu_urls(self):
    for url in (
        "http://open.feishu.cn/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000",
        "https://example.com/open-apis/bot/v2/hook/00000000-0000-0000-0000-000000000000",
        "https://open.feishu.cn@evil.example/open-apis/bot/v2/hook/x",
    ):
        with self.subTest(url=url), self.assertRaises(ValidationError):
            FeishuNotifierConfig.model_validate({**config_payload(), "webhook_url": url})


def test_private_config_requires_regular_owner_only_file(self):
    with TemporaryDirectory() as directory:
        secret_root = Path(directory) / "secrets"
        secret_root.mkdir(mode=0o700)
        path = secret_root / "feishu-notifier.yaml"
        path.write_text(yaml.safe_dump(config_payload()), encoding="utf-8")
        path.chmod(0o644)
        with self.assertRaises(FeishuConfigError):
            load_private_config(path)
        path.chmod(0o600)
        self.assertEqual(load_private_config(path).jobs, VALID_JOBS)
        link = Path(directory) / "link.yaml"
        link.symlink_to(path)
        with self.assertRaises(FeishuConfigError):
            load_private_config(link)
```

Also reject query/fragment, non-default port, empty/control-character secret, unknown YAML fields,
missing/duplicate job IDs, wrong file or parent-directory owner, parent mode other than `700`, and a
noncanonical Webhook path.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_client -v
```

Expected: import failure because `hermes_feishu_client` does not exist.

- [ ] **Step 3: Implement secure loading, validation and signing**

Before opening, use `lstat` to require the parent is an owner-matching real directory with exact mode
`700`. Read with `os.open(path, O_RDONLY | O_NOFOLLOW)` and `os.fstat`. Require a regular file owned by
`os.geteuid()` with exact mode `600`, then parse through `yaml.safe_load`. Every failure becomes
`FeishuConfigError("Feishu notifier configuration unavailable")`.

```python
EXPECTED_JOB_NAMES = frozenset(
    {"daily_submit", "daily_archive", "review_processor", "review_memory"}
)
WEBHOOK_PATH = re.compile(r"^/open-apis/bot/v2/hook/[A-Za-z0-9-]{16,128}$")


class FeishuNotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    webhook_url: str
    signing_secret: str = Field(min_length=1, max_length=512)
    jobs: dict[str, str]

    @model_validator(mode="after")
    def validate_boundary(self):
        parts = urlsplit(self.webhook_url)
        invalid = (
            parts.scheme != "https" or parts.hostname != "open.feishu.cn"
            or parts.port not in (None, 443) or parts.username is not None
            or parts.password is not None or bool(parts.query) or bool(parts.fragment)
            or WEBHOOK_PATH.fullmatch(parts.path) is None
            or set(self.jobs) != EXPECTED_JOB_NAMES or len(set(self.jobs.values())) != 4
            or any(re.fullmatch(r"[0-9a-f]{12}", value) is None for value in self.jobs.values())
        )
        if invalid:
            raise ValueError("invalid Feishu notifier configuration")
        return self


def feishu_signature(timestamp: int, secret: str) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode("ascii")
```

- [ ] **Step 4: Run tests and verify GREEN**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_client -v
```

Expected: config/signing tests pass.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_feishu_client.py tests/test_hermes_feishu_client.py
git commit -m "feat: validate signed Feishu webhook config"
```

### Task 3: Render Bounded Cards And Deliver Safely

**Files:**
- Modify: `tradingagents/integrations/hermes_feishu_client.py`
- Modify: `tests/test_hermes_feishu_client.py`

- [ ] **Step 1: Write failing rendering and transport tests**

Use these exact client-test fixtures:

```python
def config_fixture():
    return FeishuNotifierConfig.model_validate(config_payload())


def report_card_fixture(signal="BUY", decision="Hold risk limit"):
    return ReportCardData(
        event_id="report:2026-08-18:" + "a" * 64,
        trade_date=date(2026, 8, 18),
        state="ready",
        items=(ReportCardItem(
            symbol="BTC", status="completed", processed_signal=signal,
            final_trade_decision=decision, error_code=None,
        ),),
        report_path=Path("results/hermes/reports/2026-08-18.md"),
    )


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, payload):
        self.calls.append(SimpleNamespace(url=url, payload=payload))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response
```

Cover bounded redaction and success/rate limiting:

```python
def test_report_card_is_bounded_redacted_and_contains_disclaimer(self):
    report = report_card_fixture(
        signal="DEEPSEEK_API_KEY=visible-secret " + "x" * 1000,
        decision="token sk-1234567890abcdefgh",
    )
    payload = render_report_card(report, previous=None)
    text = json.dumps(payload, ensure_ascii=False)
    self.assertLessEqual(len(text.encode("utf-8")), 20_000)
    self.assertNotIn("visible-secret", text)
    self.assertNotIn("sk-1234567890abcdefgh", text)
    self.assertIn("不构成交易建议", text)


def test_client_accepts_only_http_2xx_with_feishu_code_zero(self):
    transport = FakeTransport(TransportResponse(200, b'{"code":0}', None))
    client = FeishuClient(config_fixture(), transport=transport, clock=lambda: 1599360473)
    client.send({"msg_type": "interactive", "card": {}})
    self.assertIn("timestamp", transport.calls[0].payload)
    self.assertIn("sign", transport.calls[0].payload)


def test_client_keeps_429_retry_after_safe(self):
    client = FeishuClient(
        config_fixture(),
        transport=FakeTransport(TransportResponse(429, b"rate limited", 17)),
    )
    with self.assertRaises(FeishuDeliveryError) as caught:
        client.send({"msg_type": "interactive", "card": {}})
    self.assertEqual(caught.exception.result, "rate_limited")
    self.assertEqual(caught.exception.retry_after_seconds, 17)
    self.assertNotIn("rate limited", str(caught.exception))
```

Also cover timeout, 3xx without following, 5xx, response over 64 KiB, invalid JSON, Feishu
`code != 0`, and `requests.Session().trust_env is False`.

Add a `ThreadingHTTPServer` context-manager fixture for `RequestsTransport` itself. Point the
low-level transport directly at `http://127.0.0.1:<ephemeral-port>` while keeping
`FeishuNotifierConfig` restricted to production HTTPS. The handler must exercise 200, 302, 429,
500, delayed timeout, invalid JSON and a 65,537-byte response. For 302, assert the redirect target
handler receives zero requests. No test may contact `open.feishu.cn`.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_client -v
```

Expected: failures because rendering and transport are absent.

- [ ] **Step 3: Implement bounded rendering and HTTPS transport**

Define these exact transport contracts:

```python
@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    body: bytes
    retry_after_seconds: int | None


class FeishuDeliveryError(RuntimeError):
    def __init__(self, result: str, retry_after_seconds: int | None = None):
        super().__init__(result)
        self.result = result
        self.retry_after_seconds = retry_after_seconds


class RequestsTransport:
    def __init__(self, connect_timeout=3.05, read_timeout=10):
        self.session = requests.Session()
        self.session.trust_env = False
        self.timeout = (connect_timeout, read_timeout)

    def post(self, url, payload):
        response = self.session.post(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self.timeout, allow_redirects=False, stream=True,
        )
        body = bytearray()
        for chunk in response.iter_content(4096):
            body.extend(chunk)
            if len(body) > 65_536:
                response.close()
                raise FeishuDeliveryError("response_too_large")
        return TransportResponse(
            response.status_code, bytes(body),
            parse_bounded_retry_after(response.headers.get("Retry-After")),
        )
```

Normalize CR/LF, redact secret assignments and `sk-` tokens, and cap every free field at 500
Unicode characters. `FeishuClient.send` adds timestamp/signature, rejects JSON over 20,000 UTF-8
bytes before transport, and exposes only: `timeout`, `connection_error`, `redirect_rejected`,
`rate_limited`, `http_error`, `response_too_large`, `invalid_response`, `feishu_error`.

Implement `render_report_card(ReportCardData, previous: ReportCardData | None)`,
`render_failure_card(NotificationEvent)`, `render_missing_archive_card(NotificationEvent)`, and
`render_test_card(event_id, now)` as Feishu
interactive cards with green/red/orange headers and one `lark_md` body. Every card includes its
stable event ID; report cards include the research/paper-trading disclaimer.

- [ ] **Step 4: Run tests and verify GREEN**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_client -v
```

Expected: all tests pass without real network access.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_feishu_client.py tests/test_hermes_feishu_client.py
git commit -m "feat: send bounded signed Feishu cards"
```

### Task 4: Parse Hermes Durable Executions Fail-Closed

**Files:**
- Create: `tradingagents/integrations/hermes_feishu_notifier.py`
- Create: `tests/test_hermes_feishu_notifier.py`

- [ ] **Step 1: Write failing parser and command tests**

```python
RUNS_OUTPUT = """\
d5f80f1f5694484f8282bc746277a277  completed  job=e93cfab5f78e  source=direct  2026-08-14T17:30:57.290949+08:00
f9691db864e34293b6a68ea082967e45  failed     job=e93cfab5f78e  source=schedule  2026-08-14T17:14:56.700588+08:00
    DEEPSEEK_API_KEY=must-never-escape
"""


def test_parse_keeps_headers_and_discards_error_detail(self):
    records = parse_cron_runs(RUNS_OUTPUT, "e93cfab5f78e")
    self.assertEqual([item.status for item in records], ["completed", "failed"])
    self.assertNotIn("must-never-escape", repr(records))


def test_parse_rejects_unknown_preface_or_wrong_job(self):
    with self.assertRaises(ExecutionDiscoveryError):
        parse_cron_runs("unexpected preface\n", "e93cfab5f78e")
    with self.assertRaises(ExecutionDiscoveryError):
        parse_cron_runs(RUNS_OUTPUT, "2d445dfc1a8a")


def test_loader_uses_absolute_cli_and_limit_500(self):
    seen = []
    def run(command, **kwargs):
        seen.append((command, kwargs))
        return CompletedProcess(command, 0, RUNS_OUTPUT, "")
    load_cron_runs(
        "e93cfab5f78e", run_command=run,
        hermes_cli=Path("/home/ubuntu/.local/bin/hermes"),
    )
    self.assertEqual(
        seen[0][0],
        ["/home/ubuntu/.local/bin/hermes", "cron", "runs", "e93cfab5f78e", "--limit", "500"],
    )
```

Also accept statuses `claimed`, `running`, `completed`, `failed`, `unknown`; accept the exact
no-records line; reject duplicate IDs, invalid/naive timestamps, nonzero exit, timeout, and a cursor
missing from a nonempty observed window.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_notifier -v
```

Expected: import failure because the notifier module does not exist.

- [ ] **Step 3: Implement parser, subprocess boundary and failure discovery**

```python
RUN_LINE = re.compile(
    r"^(?P<id>[0-9a-f]{32})  (?P<status>claimed|running|completed|failed|unknown)\s+"
    r"job=(?P<job_id>[0-9a-f]{12})  source=(?P<source>[^\s]+)  (?P<claimed_at>[^\s]+)$"
)
```

The first nonempty line must be a header or `No cron execution attempts recorded.`. After a valid
header, discard all non-header lines as an opaque error block. `load_cron_runs` uses
`subprocess.run(capture_output=True, text=True, timeout=20, check=False)` and raises only the safe
`ExecutionDiscoveryError("Hermes execution history unavailable")`.

`discover_execution_events` requires each existing cursor in the observed rows, processes rows
before it oldest-first, creates a failure event per new failed row, advances to the newest ID, and
stores all observed IDs up to 500. Return new completed daily-archive rows for Task 5. Record
`unknown` as seen but do not alert on it.

- [ ] **Step 4: Run tests and verify GREEN**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_notifier -v
```

Expected: parser/discovery tests pass.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_feishu_notifier.py tests/test_hermes_feishu_notifier.py
git commit -m "feat: discover failed Hermes cron executions"
```

### Task 5: Validate Reports And Discover Report Events

**Files:**
- Modify: `tradingagents/integrations/hermes_feishu_notifier.py`
- Modify: `tests/test_hermes_feishu_notifier.py`

- [ ] **Step 1: Write failing archive and warning tests**

Create `persisted_archive` from existing `DailyReport*` schemas, write Markdown, calculate SHA-256,
then persist matching ASCII batch JSON. Add:

```python
SHANGHAI = ZoneInfo("Asia/Shanghai")


def batch_request(trade_date):
    return DailyReportRequest(
        trade_date=trade_date, symbols=["BTC", "ETH", "SOL"],
        analysts=["market", "news", "fundamentals"], research_depth=1,
        llm_provider="deepseek", quick_model="deepseek-v4-flash",
        deep_model="deepseek-v4-pro",
    )


def persisted_archive(results_root, trade_date, state="ready"):
    hermes_root = results_root / "hermes"
    report_root = hermes_root / "reports"
    batch_root = hermes_root / "report_batches"
    report_root.mkdir(parents=True)
    batch_root.mkdir(parents=True)
    document = f"# TradingAgents Daily Report - {trade_date.isoformat()}\n"
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
    archive = DailyReportArchive(
        filename=f"{trade_date.isoformat()}.md", sha256=digest, state=state,
        archived_at=datetime(2026, 8, 18, 4, 0, tzinfo=timezone.utc),
        items=[DailyReportArchiveItem(
            symbol=symbol, status="completed", processed_signal="BUY",
            final_trade_decision=f"{symbol} paper decision", error_code=None,
        ) for symbol in ("BTC", "ETH", "SOL")],
        scheduled_review_version=2,
    )
    request = batch_request(trade_date)
    batch = DailyReportBatch(
        batch_id="report_" + "a" * 32, request=request,
        created_at=datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc),
        items=[DailyReportBatchItem(
            symbol=symbol, session_id=f"hermes_{index:032x}",
        ) for index, symbol in enumerate(("BTC", "ETH", "SOL"), start=1)],
        archive=archive,
    )
    (report_root / archive.filename).write_text(document, encoding="utf-8")
    (batch_root / f"{trade_date.isoformat()}.json").write_text(
        json.dumps(batch.model_dump(mode="json"), ensure_ascii=True, indent=2),
        encoding="ascii",
    )
    return archive


def persist_active_batch(results_root, trade_date):
    hermes_root = results_root / "hermes"
    batch_root = hermes_root / "report_batches"
    batch_root.mkdir(parents=True)
    request = batch_request(trade_date)
    batch = DailyReportBatch(
        batch_id="report_" + "b" * 32, request=request,
        created_at=datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc),
        items=[DailyReportBatchItem(
            symbol=symbol, session_id=f"hermes_{index:032x}",
        ) for index, symbol in enumerate(("BTC", "ETH", "SOL"), start=1)],
    )
    (batch_root / f"{trade_date.isoformat()}.json").write_text(
        json.dumps(batch.model_dump(mode="json"), ensure_ascii=True, indent=2),
        encoding="ascii",
    )


def cron_execution(job_id, status, claimed_at, execution_id="c" * 32):
    return CronExecution(
        execution_id=execution_id, job_id=job_id, status=status,
        source="schedule", claimed_at=claimed_at,
    )


def initialized_empty_state():
    return initialized_state(
        datetime(2026, 8, 18, tzinfo=timezone.utc),
        {job_id: [] for job_id in VALID_JOBS.values()},
        [],
    )
```

Then add the cases:

```python
def test_verified_archives_rejects_wrong_digest(self):
    with TemporaryDirectory() as directory:
        root = Path(directory) / "results"
        persisted_archive(root, date(2026, 8, 18))
        (root / "hermes" / "reports" / "2026-08-18.md").write_text(
            "tampered", encoding="utf-8"
        )
        with self.assertRaises(ReportDiscoveryError):
            load_verified_archives(root)


def test_report_event_uses_date_and_digest(self):
    with TemporaryDirectory() as directory:
        root = Path(directory) / "results"
        archive = persisted_archive(root, date(2026, 8, 18))
        events = discover_report_events(initialized_empty_state(), load_verified_archives(root))
    self.assertEqual(events[0].event_id, f"report:2026-08-18:{archive.sha256}")


def test_completed_archive_without_report_creates_warning(self):
    with TemporaryDirectory() as directory:
        root = Path(directory) / "results"
        persist_active_batch(root, date(2026, 8, 18))
        execution = cron_execution(
            job_id="5b7f7906306a", status="completed",
            claimed_at=datetime(2026, 8, 18, 12, 0, tzinfo=SHANGHAI),
        )
        events = discover_missing_archive_events(root, [execution], [])
    self.assertEqual([event.kind for event in events], ["missing_archive"])
```

Also test filename/date mismatch, malformed batch JSON, invalid schema, missing report, nearest
previous report, degraded items, already-seen IDs, and valid report suppression of a warning.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_notifier -v
```

Expected: failures because report discovery is absent.

- [ ] **Step 3: Implement schema-backed archive discovery**

Define a frozen `VerifiedArchive` with trade date, batch ID, SHA/state/items, report path, and nearest
earlier snapshot. Validate every canonical `report_batches/*.json` with
`DailyReportBatch.model_validate_json`. For archived batches enforce:

```python
expected = f"{batch.request.trade_date.isoformat()}.md"
if batch.archive.filename != expected:
    raise ReportDiscoveryError("daily report archive unavailable")
path = results_root / "hermes" / "reports" / expected
if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != batch.archive.sha256:
    raise ReportDiscoveryError("daily report archive unavailable")
```

Create `report:<date>:<sha256>` only when absent from compact seen IDs. `VerifiedArchive` exposes a
`to_card_data(event_id)` method that maps immutable archive items to the `ReportCardData` contract
without copying them into notification state. For each new completed
archive execution, use its Shanghai date; if that batch exists without archive create
`missing_archive:<job_id>:<execution_id>` with `batch_state="unarchived"`. A valid archive
suppresses the warning. Any unreadable
source fails the whole discovery pass before state mutation.

- [ ] **Step 4: Run tests and verify GREEN**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_notifier -v
```

Expected: all execution/report discovery tests pass.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_feishu_notifier.py tests/test_hermes_feishu_notifier.py
git commit -m "feat: discover verified daily report notifications"
```

### Task 6: Orchestrate Baseline, Pending Events And Retries

**Files:**
- Modify: `tradingagents/integrations/hermes_feishu_notifier.py`
- Modify: `tests/test_hermes_feishu_notifier.py`

- [ ] **Step 1: Write failing initialization, write-ahead and retry tests**

Inject `execution_loader`, `archive_loader`, `client`, and `clock` dependencies. Add:

```python
def test_initialize_records_history_without_network(self):
    state, payload = initialize_notifier(
        store=self.store, config=config_fixture(),
        execution_loader=execution_fixture_loader(),
        archive_loader=lambda: [archive_fixture()], now=NOW,
    )
    self.assertEqual(payload["execution_count"], 4)
    self.assertEqual(payload["report_count"], 1)
    self.assertEqual(state.deliveries, {})
    self.assertFalse(self.client.calls)


def test_run_persists_pending_before_client_call(self):
    observations = []
    client = RecordingClient(
        before_send=lambda event_id: observations.append(
            self.store.load().deliveries[event_id].delivered_at
        )
    )
    execution_loader, archive_loader = loaders_with_one_failure()
    code, payload = run_notifier_once(
        self.store, config_fixture(), client,
        execution_loader, archive_loader, NOW,
    )
    self.assertEqual(observations, [None])
    self.assertEqual(code, 0)
    self.assertEqual(payload["delivered"], 1)


def test_failed_send_advances_retry_without_losing_other_events(self):
    client = SelectiveFailClient(fail_event_id="failure:a:b", result="timeout")
    execution_loader, archive_loader = loaders_with_two_events()
    code, payload = run_notifier_once(
        self.store, config_fixture(), client,
        execution_loader, archive_loader, NOW,
    )
    state = self.store.load()
    self.assertEqual(code, 1)
    self.assertEqual(payload["delivered"], 1)
    self.assertEqual(payload["pending"], 1)
    self.assertEqual(state.deliveries["failure:a:b"].attempt_count, 1)
    self.assertEqual(
        state.deliveries["failure:a:b"].next_attempt_at,
        NOW + timedelta(minutes=5),
    )
```

Also test repeated initialization is byte-stable, uninitialized run makes no network request, lock
collision is a safe no-op, not-yet-due events are skipped, 429 can extend but not shorten retry,
success sets `delivered_at`, normal reruns do not resend, and the documented crash window retries the
same event ID.

- [ ] **Step 2: Run orchestration tests and verify RED**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_notifier -v
```

Expected: failures because baseline/run orchestration is absent.

- [ ] **Step 3: Implement the locked two-phase runner**

Implement these interfaces and control flow:

```python
def initialize_notifier(store, config, execution_loader, archive_loader, now):
    with store.lock():
        existing = store.load_optional()
        if existing is not None:
            return existing, {
                "ok": True, "mode": "initialize", "already_initialized": True,
                "execution_count": sum(len(ids) for ids in existing.seen_execution_ids.values()),
                "report_count": len(existing.seen_report_event_ids),
            }
        histories = {
            job_id: execution_loader(job_id) for job_id in config.jobs.values()
        }
        archives = archive_loader()
        state = initialized_state(
            now,
            {job_id: [row.execution_id for row in rows] for job_id, rows in histories.items()},
            [snapshot.event_id for snapshot in archives],
        )
        store.save(state)
        return state, {
            "ok": True, "mode": "initialize", "already_initialized": False,
            "execution_count": sum(len(rows) for rows in histories.values()),
            "report_count": len(archives),
        }


def run_notifier_once(
    store, config, client, execution_loader, archive_loader, now
):
    try:
        with store.lock():
            state = store.load()
            histories = {
                job_id: execution_loader(job_id) for job_id in config.jobs.values()
            }
            archives = archive_loader()
            state, new_events = discover_all_events(
                state, config, histories, archives, now
            )
            state = add_pending_events(state, new_events, now)
            store.save(state)
            delivered = 0
            failed = 0
            for event_id in due_event_ids(state, now):
                state = begin_attempt(state, event_id, now)
                store.save(state)
                try:
                    card = render_persisted_event(
                        state.deliveries[event_id].event, archives
                    )
                    client.send(card)
                except FeishuDeliveryError as error:
                    state = record_delivery_failure(state, event_id, error, now)
                    failed += 1
                else:
                    state = record_delivery_success(state, event_id, now)
                    delivered += 1
                store.save(state)
            state = prune_delivered(state, now)
            store.save(state)
            pending = sum(
                record.delivered_at is None for record in state.deliveries.values()
            )
            return (1 if failed else 0), {
                "ok": failed == 0, "mode": "run", "discovered": len(new_events),
                "delivered": delivered, "pending": pending,
            }
    except NotificationAlreadyRunning:
        return 0, {
            "ok": True, "mode": "run", "discovered": 0,
            "delivered": 0, "pending": 0, "result": "already_running",
        }


def send_test_card(config, client, now):
    event_id = f"test:{now.astimezone(timezone.utc).isoformat()}"
    client.send(render_test_card(event_id, now))
    return {"ok": True, "mode": "test", "event_id": event_id}
```

`initialize_notifier` locks, loads all four 500-row histories and verified archives, and stores only
cursors/seen IDs. If valid state already exists, return unchanged counts with no network call.

`run_notifier_once` performs this exact order while holding the non-blocking lock:

1. Load initialized state.
2. Load every source; on any failure return nonzero without saving.
3. Discover all events/cursors in memory.
4. Add all events as pending and atomically save before network.
5. Before each send, atomically increment attempt count and set next retry.
6. Render using persisted event metadata and freshly validated report snapshot.
7. On success atomically set `delivered_at` and `last_result="delivered"`.
8. On failure retain pending and store only the safe result category.
9. Prune old delivered metadata and return nonzero if any due event failed.

Use `max(calculated_retry, Retry-After)`, capped at 24 hours. Output only `ok`, `mode`,
`discovered`, `delivered`, `pending`, and safe categories. `send_test_card` sends one orange card
titled `TradingAgents 飞书通知配置验收` with ID `test:<UTC timestamp>` and never mutates production
state.

- [ ] **Step 4: Run all notification tests and verify GREEN**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_state tests.test_hermes_feishu_client tests.test_hermes_feishu_notifier -v
```

Expected: all tests pass and no real network call occurs.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_feishu_notifier.py tests/test_hermes_feishu_notifier.py
git commit -m "feat: orchestrate durable Feishu notification retries"
```

### Task 7: Add Safe CLI Bootstrap And No-Agent Wrapper

**Files:**
- Create: `tradingagents/integrations/hermes_feishu_bootstrap.py`
- Modify: `tradingagents/integrations/hermes_feishu_notifier.py`
- Modify: `tests/test_hermes_feishu_notifier.py`
- Create: `deploy/hermes/scripts/tradingagents-feishu-notifier.sh`

- [ ] **Step 1: Write failing CLI/bootstrap/wrapper tests**

```python
def test_main_rejects_test_without_explicit_confirmation(self):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = notifier.main(["test"], config=config_fixture())
    self.assertEqual(code, 1)
    self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "INVALID_NOTIFY_REQUEST")


def test_bootstrap_loads_config_before_importing_runner(self):
    order = []
    fake_runner = SimpleNamespace(main=lambda argv, config: 0)
    with patch.object(
        bootstrap, "load_private_config",
        side_effect=lambda path: order.append("load") or config_fixture(),
    ), patch.object(
        bootstrap, "import_module",
        side_effect=lambda name: order.append("import") or fake_runner,
    ):
        code = bootstrap.main(["run"])
    self.assertEqual(code, 0)
    self.assertEqual(order, ["load", "import"])


def test_bootstrap_redacts_startup_failure(self):
    stdout = io.StringIO()
    with patch.object(
        bootstrap, "load_private_config", side_effect=OSError("secret URL")
    ), redirect_stdout(stdout):
        code = bootstrap.main(["run"])
    self.assertEqual(code, 1)
    self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "FEISHU_NOTIFIER_FAILED")
    self.assertNotIn("secret URL", stdout.getvalue())
```

Also assert the wrapper uses `.venv-hermes-mcp/bin/python`, invokes only
`hermes_feishu_bootstrap run`, and contains no `API_KEY`, `webhook`, `secret`, `MEMORY.md`, or
`hermes cron` text.

- [ ] **Step 2: Run CLI tests and verify RED**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_notifier -v
```

Expected: failures because CLI/bootstrap/wrapper are absent.

- [ ] **Step 3: Implement CLI, bootstrap and wrapper**

Support exactly:

```text
hermes_feishu_bootstrap initialize
hermes_feishu_bootstrap run
hermes_feishu_bootstrap test --confirm-external-send
```

Use a parser subclass that raises `ValueError`. Defaults are:

```python
CONFIG_PATH = Path.home() / ".hermes" / "secrets" / "feishu-notifier.yaml"
RESULTS_ROOT = Path.cwd() / "results"
STATE_ROOT = RESULTS_ROOT / "hermes" / "feishu_notifications"
HERMES_CLI = Path.home() / ".local" / "bin" / "hermes"
```

The bootstrap validates config before runner import and calls `runner.main(arguments, config)`.
Every startup exception prints only:

```json
{"ok": false, "mode": "run", "error": {"code": "FEISHU_NOTIFIER_FAILED", "message": "The Feishu notifier could not complete.", "suggested_action": "Inspect the safe notifier Cron result and private configuration."}}
```

Create executable wrapper content:

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=/home/ubuntu/workspace/TradingAgents-crypto
exec "$PROJECT_DIR/.venv-hermes-mcp/bin/python" -m tradingagents.integrations.hermes_feishu_bootstrap run "$@"
```

- [ ] **Step 4: Run tests and verify GREEN**

```bash
chmod +x deploy/hermes/scripts/tradingagents-feishu-notifier.sh
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_feishu_state tests.test_hermes_feishu_client tests.test_hermes_feishu_notifier -v
```

Expected: all notification tests pass without real network access.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_feishu_bootstrap.py tradingagents/integrations/hermes_feishu_notifier.py tests/test_hermes_feishu_notifier.py deploy/hermes/scripts/tradingagents-feishu-notifier.sh
git commit -m "feat: add no-agent Feishu notifier command"
```

### Task 8: Add Static Deployment Guards And Runbook

**Files:**
- Modify: `tests/test_hermes_review_verifier.py`
- Modify: `docs/hermes_integration.md`

- [ ] **Step 1: Write failing static contract tests**

Add `FEISHU_NOTIFIER_SCRIPT` beside existing wrapper constants, then:

```python
def test_feishu_notifier_wrapper_and_runbook_preserve_security_boundary(self):
    script = FEISHU_NOTIFIER_SCRIPT.read_text(encoding="ascii")
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    self.assertIn("hermes_feishu_bootstrap run", script)
    self.assertIn(".venv-hermes-mcp/bin/python", script)
    self.assertNotIn("webhook", script.lower())
    self.assertNotIn("secret", script.lower())
    self.assertNotIn("MEMORY.md", script)
    self.assertIn("/home/ubuntu/.hermes/secrets/feishu-notifier.yaml", runbook)
    self.assertIn("install -d -m 700", runbook)
    self.assertIn("*/5 * * * *", runbook)
    self.assertIn("--no-agent --script tradingagents-feishu-notifier.sh", runbook)
    self.assertIn('hermes cron pause "$feishu_notifier_job_id"', runbook)
    self.assertIn("test --confirm-external-send", runbook)
    self.assertIn("不得读取、输出或修改 `MEMORY.md`", runbook)


def test_feishu_runbook_initializes_before_test_and_resume(self):
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    section = text[text.index("## 飞书群机器人通知") :]
    initialize = section.index("hermes_feishu_bootstrap initialize")
    create = section.index("hermes cron create --name tradingagents-feishu-notifier")
    pause = section.index('hermes cron pause "$feishu_notifier_job_id"')
    test_send = section.index("test --confirm-external-send")
    resume = section.index('hermes cron resume "$feishu_notifier_job_id"')
    self.assertLess(initialize, create)
    self.assertLess(create, pause)
    self.assertLess(pause, test_send)
    self.assertLess(test_send, resume)
```

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_review_verifier.HermesReviewVerifierTests.test_feishu_notifier_wrapper_and_runbook_preserve_security_boundary tests.test_hermes_review_verifier.HermesReviewVerifierTests.test_feishu_runbook_initializes_before_test_and_resume -v
```

Expected: failures because the constant/runbook section are absent.

- [ ] **Step 3: Add the complete runbook section**

Append `## 飞书群机器人通知` to `docs/hermes_integration.md`. Include the exact four monitored
IDs; owner-only `getpass` secret provisioning with atomic YAML write; source/wrapper verification;
initialize before create; paused tests; one explicit real test card; resume/status/runs checks;
next real report acceptance; retry diagnosis; rollback; and the memory/artifact prohibitions.

Use this exact create/pause block:

```bash
create_output="$(hermes cron create --name tradingagents-feishu-notifier --deliver local --no-agent --script tradingagents-feishu-notifier.sh --workdir "$PROJECT_DIR" '*/5 * * * *')"
feishu_notifier_job_id="$(printf '%s\n' "$create_output" | sed -n 's/.*Created job: \([0-9a-f]\{12\}\).*/\1/p')"
test "${#feishu_notifier_job_id}" -eq 12
hermes cron pause "$feishu_notifier_job_id"
```

- [ ] **Step 4: Run static and notification tests and verify GREEN**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_review_verifier tests.test_hermes_feishu_state tests.test_hermes_feishu_client tests.test_hermes_feishu_notifier -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add docs/hermes_integration.md tests/test_hermes_review_verifier.py
git commit -m "docs: add Feishu notifier deployment runbook"
```

### Task 9: Run Complete Local Verification

**Files:**
- Verify only; modify only to fix a demonstrated failure, then rerun that test first.

- [ ] **Step 1: Compile runtime and tests**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m compileall -q tradingagents tests
```

Expected: exit 0, no output.

- [ ] **Step 2: Run the complete suite**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest discover -s tests -v
```

Expected: exit 0, zero failures/errors; existing explicitly skipped provider tests may remain skipped.

- [ ] **Step 3: Run diff and secret-source checks**

```bash
git diff --check
test -z "$(git status --porcelain)"
! rg -n "open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9-]{16,}|signing_secret:\s*[^[:space:]]" tradingagents deploy
! rg -n "MEMORY\.md" tradingagents/integrations/hermes_feishu_* deploy/hermes/scripts/tradingagents-feishu-notifier.sh
```

Expected: clean status and no real Webhook/secret/memory-file access in runtime sources.

- [ ] **Step 4: Review the final range**

```bash
git log --oneline bed738b..HEAD
git diff --stat bed738b..HEAD
git diff --check bed738b..HEAD
```

Expected: only planned modules, tests, wrapper and runbook changed; diff check exits 0.

- [ ] **Step 5: Request code review**

Invoke `superpowers:requesting-code-review`, address only verified findings, and rerun Steps 1-4.
Do not deploy an unreviewed commit.

### Task 10: Deploy Paused, Send One Test Card, Then Activate

**Files:**
- Install: `/home/ubuntu/.hermes/scripts/tradingagents-feishu-notifier.sh`
- Create privately: `/home/ubuntu/.hermes/secrets/feishu-notifier.yaml`
- Create through initialize: `/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/feishu_notifications/state.json`

- [ ] **Step 1: Record pre-deployment evidence**

```bash
cd /home/ubuntu/workspace/TradingAgents-crypto
test -z "$(git status --porcelain)"
/home/ubuntu/.local/bin/hermes cron status
/home/ubuntu/.local/bin/hermes cron list --all
python - <<'PY'
import hashlib
import stat
from pathlib import Path

root = Path('results/hermes').resolve(strict=True)
rows = []
for dirname in ('report_batches', 'reports'):
    allowed_root = root / dirname
    if not allowed_root.exists():
        continue
    assert stat.S_ISDIR(allowed_root.lstat().st_mode)
    assert allowed_root.resolve(strict=True).parent == root
    for path in allowed_root.rglob('*'):
        if stat.S_ISREG(path.lstat().st_mode):
            relative = path.relative_to(root)
            rows.append(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative.as_posix()}')
print(hashlib.sha256(('\n'.join(sorted(rows)) + '\n').encode('ascii')).hexdigest())
PY
```

Expected: Gateway healthy, four production jobs active, and one artifact manifest. Record the hash
only in the operator transcript; never include `MEMORY.md`.

- [ ] **Step 2: Deploy the reviewed exact commit and wrapper**

```bash
git push -u origin feat/hermes-feishu-notifications
DEPLOY_COMMIT="$(git rev-parse HEAD)"
ssh ubuntu@119.28.49.81 "cd /home/ubuntu/workspace/TradingAgents-crypto && git fetch origin && git cat-file -e '$DEPLOY_COMMIT^{commit}' && git switch --detach '$DEPLOY_COMMIT' && install -m 700 deploy/hermes/scripts/tradingagents-feishu-notifier.sh /home/ubuntu/.hermes/scripts/tradingagents-feishu-notifier.sh"
```

Expected: server `HEAD` is the reviewed SHA and wrapper mode is `700`; stop if either worktree is
dirty or the commit cannot be verified.

- [ ] **Step 3: Provision secrets through non-echoing TTY prompts**

Open `ssh -t ubuntu@119.28.49.81`, then run this exact remote block. `getpass` reads from the TTY
without echo; the script writes atomically and prints no values:

```bash
set -e
install -d -m 700 /home/ubuntu/.hermes/secrets
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python - <<'PY'
import os
import tempfile
from getpass import getpass
from pathlib import Path

import yaml

destination = Path("/home/ubuntu/.hermes/secrets/feishu-notifier.yaml")
payload = {
    "version": 1,
    "webhook_url": getpass("Feishu Webhook URL: "),
    "signing_secret": getpass("Feishu signing secret: "),
    "jobs": {
        "daily_submit": "2d445dfc1a8a",
        "daily_archive": "5b7f7906306a",
        "review_processor": "d6c0e087e5a8",
        "review_memory": "e93cfab5f78e",
    },
}
temporary_path = None
try:
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=destination.parent,
        prefix=".feishu-notifier.", suffix=".tmp", delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o600)
        yaml.safe_dump(payload, temporary, allow_unicode=False, sort_keys=True)
        temporary.flush()
        os.fsync(temporary.fileno())
    os.replace(temporary_path, destination)
    os.chmod(destination, 0o600)
    temporary_path = None
finally:
    if temporary_path is not None:
        temporary_path.unlink(missing_ok=True)
PY
```

Check metadata only:

```bash
test "$(stat -c '%U %a' /home/ubuntu/.hermes/secrets)" = "ubuntu 700"
test "$(stat -c '%U %a' /home/ubuntu/.hermes/secrets/feishu-notifier.yaml)" = "ubuntu 600"
```

Expected: both checks exit 0. Never print the YAML.

- [ ] **Step 4: Initialize baseline without network**

```bash
cd /home/ubuntu/workspace/TradingAgents-crypto
.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_feishu_bootstrap initialize
stat -c '%U %a %n' results/hermes/feishu_notifications/state.json
```

Expected: safe counts, state `ubuntu 600`, no Feishu message.

- [ ] **Step 5: Create and immediately pause the fifth Cron**

Run this complete block. The normal path has no Hermes command between create and
pause; if create-output parsing fails, the only permitted recovery command is an
immediate `cron list --all` lookup by exact notifier name so the new job can
still be paused before any later checks:

```bash
set -e
PROJECT_DIR=/home/ubuntu/workspace/TradingAgents-crypto
HERMES=/home/ubuntu/.local/bin/hermes
PYTHON="$PROJECT_DIR/.venv-hermes-mcp/bin/python"
resolve_created_notifier_job_id() {
  local create_output="$1" parsed_job_id create_list_after_parse_failure
  parsed_job_id="$(printf '%s\n' "$create_output" | sed -n 's/.*Created job: \([0-9a-f]\{12\}\).*/\1/p')"
  if [ "${#parsed_job_id}" -eq 12 ]; then
    printf '%s\n' "$parsed_job_id"
    return 0
  fi
  create_list_after_parse_failure="$(mktemp /tmp/tradingagents-feishu-create-list.XXXXXX)"
  "$HERMES" cron list --all > "$create_list_after_parse_failure"
  "$PYTHON" - "$create_list_after_parse_failure" <<'PY'
import sys
from pathlib import Path

records = []
current = {'raw': []}
for raw_line in Path(sys.argv[1]).read_text(encoding='utf-8').splitlines():
    line = raw_line.strip()
    if not line:
        if current['raw']:
            records.append(current)
            current = {'raw': []}
        continue
    line_parts = line.split()
    candidate_status = line_parts[1].strip('[]') if len(line_parts) > 1 else ''
    starts_record = (
        len(line_parts) > 1
        and len(line_parts[0]) == 12
        and all(character in '0123456789abcdef' for character in line_parts[0])
        and candidate_status in {'active', 'paused'}
    )
    if starts_record and current['raw']:
        records.append(current)
        current = {'raw': []}
    current['raw'].append(line)
    if starts_record:
        current['id'] = line_parts[0]
    if ':' in line:
        key, value = line.split(':', 1)
        current[key.strip().lower().replace(' ', '_').replace('-', '_')] = value.strip()
if current['raw']:
    records.append(current)
matches = [
    record for record in records
    if record.get('name') == 'tradingagents-feishu-notifier'
]
if len(matches) != 1:
    raise SystemExit('expected exactly one tradingagents-feishu-notifier job after create')
print(matches[0]['id'])
PY
}
create_output="$("$HERMES" cron create --name tradingagents-feishu-notifier --deliver local --no-agent --script tradingagents-feishu-notifier.sh --workdir "$PROJECT_DIR" '*/5 * * * *')"
feishu_notifier_job_id="$(resolve_created_notifier_job_id "$create_output")" || {
  printf 'Safe create output follows; pause the new tradingagents-feishu-notifier job before continuing:\n%s\n' "$create_output" >&2
  exit 1
}
test "${#feishu_notifier_job_id}" -eq 12
"$HERMES" cron pause "$feishu_notifier_job_id"
```

Then verify:

```bash
/home/ubuntu/.local/bin/hermes cron list --all
/home/ubuntu/.local/bin/hermes cron runs "$feishu_notifier_job_id" --limit 5
```

Expected: notifier paused with no unexpected run; four existing jobs remain active.

- [ ] **Step 6: Run paused acceptance and one real test card**

```bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_feishu_state tests.test_hermes_feishu_client tests.test_hermes_feishu_notifier -v
.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_feishu_bootstrap test --confirm-external-send
```

Expected: tests pass, safe success JSON, and exactly one orange
`TradingAgents 飞书通知配置验收` card. Confirm receipt before continuing.

- [ ] **Step 7: Recheck immutability and activate only notifier**

Recompute Step 1's manifest excluding `feishu_notifications`; require an exact match. Confirm the
four job definitions are unchanged, then:

```bash
/home/ubuntu/.local/bin/hermes cron resume "$feishu_notifier_job_id"
/home/ubuntu/.local/bin/hermes cron status
/home/ubuntu/.local/bin/hermes cron list --all
```

Expected: five active jobs; notifier next run within five minutes; no historical notification.

- [ ] **Step 8: Verify first durable notifier run**

```bash
/home/ubuntu/.local/bin/hermes cron runs "$feishu_notifier_job_id" --limit 5
```

Expected: newest execution completed, zero historical events sent, no secret text in safe output.

- [ ] **Step 9: Complete next-report end-to-end acceptance**

After the next real archive, run this without printing report or memory content:

```bash
set -e
cd /home/ubuntu/workspace/TradingAgents-crypto
TRADE_DATE="$(TZ=Asia/Shanghai date +%F)"
.venv-hermes-mcp/bin/python - "$TRADE_DATE" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

trade_date = sys.argv[1]
root = Path("results/hermes")
batch = json.loads((root / "report_batches" / f"{trade_date}.json").read_text("ascii"))
archive = batch["archive"]
report = root / "reports" / archive["filename"]
assert archive["filename"] == f"{trade_date}.md"
assert hashlib.sha256(report.read_bytes()).hexdigest() == archive["sha256"]
assert [item["symbol"] for item in archive["items"]] == ["BTC", "ETH", "SOL"]
print(json.dumps({
    "trade_date": trade_date,
    "state": archive["state"],
    "symbols": [item["symbol"] for item in archive["items"]],
    "sha256": archive["sha256"],
}, sort_keys=True))
PY
/home/ubuntu/.local/bin/hermes cron runs "$feishu_notifier_job_id" --limit 5
```

Expected: SHA and symbols validate, notifier completed, and exactly one green group card matches the
printed safe fields and structured BTC/ETH/SOL items. Do not display or modify `MEMORY.md`.

- [ ] **Step 10: Preserve rollback instructions without executing them**

Record the resolved notifier job ID privately. Save these commands in the operator transcript but do
not execute them during a successful rollout:

```bash
/home/ubuntu/.local/bin/hermes cron pause "$feishu_notifier_job_id"
/home/ubuntu/.local/bin/hermes cron runs "$feishu_notifier_job_id" --limit 5
/home/ubuntu/.local/bin/hermes cron remove "$feishu_notifier_job_id"
```

Rollback retains notifier state/config by default and never deletes existing TradingAgents artifacts.
