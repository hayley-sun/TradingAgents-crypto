import json
import operator
import os
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

import requests
import yaml
from pydantic import ValidationError

from tradingagents.integrations.hermes_feishu_client import (
    FeishuClient,
    FeishuConfigError,
    FeishuDeliveryError,
    FeishuNotifierConfig,
    ReportCardData,
    ReportCardItem,
    RequestsTransport,
    TransportResponse,
    feishu_signature,
    load_private_config,
    parse_bounded_retry_after,
    render_failure_card,
    render_missing_archive_card,
    render_report_card,
    render_test_card,
)
from tradingagents.integrations.hermes_feishu_state import (
    NotificationEvent,
)


VALID_JOBS = {
    "daily_submit": "2d445dfc1a8a",
    "daily_archive": "5b7f7906306a",
    "review_processor": "d6c0e087e5a8",
    "review_memory": "e93cfab5f78e",
}


def config_payload():
    return {
        "version": 1,
        "webhook_url": (
            "https://open.feishu.cn/open-apis/bot/v2/hook/"
            "00000000-0000-0000-0000-000000000000"
        ),
        "signing_secret": "unit-test-signing-secret",
        "jobs": VALID_JOBS,
    }


def config_fixture():
    return FeishuNotifierConfig.model_validate(config_payload())


def report_card_fixture(signal="BUY", decision="Hold risk limit"):
    return ReportCardData(
        event_id="report:2026-08-18:" + "a" * 64,
        trade_date=date(2026, 8, 18),
        state="ready",
        items=(
            ReportCardItem(
                symbol="BTC",
                status="completed",
                processed_signal=signal,
                final_trade_decision=decision,
                error_code=None,
            ),
        ),
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


class LocalFeishuHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _write_response(self, status, body=b"", headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(content_length)
        self.server.requests.append(
            SimpleNamespace(
                path=self.path,
                body=request_body,
                content_type=self.headers.get("Content-Type"),
            )
        )

        if self.path == "/ok":
            self._write_response(200, b'{"code":0}')
        elif self.path == "/redirect":
            self._write_response(302, headers={"Location": "/redirect-target"})
        elif self.path == "/redirect-target":
            self.server.redirect_target_requests += 1
            self._write_response(200, b'{"code":0}')
        elif self.path == "/rate-limited":
            self._write_response(
                429,
                b"rate limited",
                headers={"Retry-After": "17"},
            )
        elif self.path == "/server-error":
            self._write_response(500, b"internal details")
        elif self.path == "/timeout":
            time.sleep(0.15)
            self._write_response(200, b'{"code":0}')
        elif self.path == "/stream-timeout":
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            try:
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.15)
                self.wfile.write(b"x" * 9)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/invalid-json":
            self._write_response(200, b"not json")
        elif self.path == "/too-large":
            self._write_response(200, b"x" * 65_537)
        else:
            self._write_response(404, b"not found")

    def log_message(self, _format, *_args):
        pass


@contextmanager
def local_feishu_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalFeishuHandler)
    server.requests = []
    server.redirect_target_requests = 0
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def card_body(payload):
    bodies = []

    def visit(value):
        if isinstance(value, dict):
            if value.get("tag") == "lark_md":
                bodies.append(value.get("content"))
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    if len(bodies) != 1:
        raise AssertionError(f"expected one lark_md body, found {len(bodies)}")
    return bodies[0]


class FeishuCardRenderingTests(unittest.TestCase):
    def assert_card(self, payload, color, title, event_id):
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(payload["card"]["header"]["template"], color)
        self.assertEqual(
            payload["card"]["header"]["title"],
            {"tag": "plain_text", "content": title},
        )
        body = card_body(payload)
        self.assertIn(event_id, body)
        return body

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

    def test_report_card_redacts_short_sk_tokens(self):
        report = report_card_fixture(decision="credential sk-abc")

        text = json.dumps(render_report_card(report, previous=None))

        self.assertNotIn("sk-abc", text)

    def test_report_card_redacts_secrets_without_word_boundary_assumptions(self):
        cases = {
            "unicode assignment": (
                "前DEEPSEEK_API_KEY=visible-fragment",
                "visible-fragment",
            ),
            "underscore assignment": (
                "prefix_DEEPSEEK_API_KEY=underscore-fragment",
                "underscore-fragment",
            ),
            "ASCII assignment": (
                "prefixDEEPSEEK_API_KEY=ascii-fragment",
                "ascii-fragment",
            ),
            "unicode token": (
                "前sk-unicode-fragment",
                "sk-unicode-fragment",
            ),
            "underscore token": (
                "prefix_sk-underscore-fragment",
                "sk-underscore-fragment",
            ),
            "ASCII token": (
                "prefixsk-ascii-fragment",
                "sk-ascii-fragment",
            ),
        }

        for case, (value, secret_fragment) in cases.items():
            payload = render_report_card(
                report_card_fixture(decision=value), previous=None
            )
            rendered = json.dumps(payload, ensure_ascii=False, allow_nan=False)

            with self.subTest(case=case):
                self.assertNotIn(secret_fragment, rendered)
                self.assertIn("[REDACTED]", rendered)

    def test_report_card_redacts_escaped_and_unclosed_quoted_assignments(self):
        cases = {
            "escaped double quote": (
                r'API_KEY="double-fragment-a\"double-fragment-b"',
                ("double-fragment-a", "double-fragment-b"),
            ),
            "escaped single quote": (
                r"TOKEN='single-fragment-a\'single-fragment-b'",
                ("single-fragment-a", "single-fragment-b"),
            ),
            "escaped backslash": (
                r'PASSWORD="slash-fragment-a\\slash-fragment-b"',
                ("slash-fragment-a", "slash-fragment-b"),
            ),
            "unclosed double quote": (
                'SECRET="unclosed-double-fragment',
                ("unclosed-double-fragment",),
            ),
            "unclosed single quote": (
                "API_KEY='unclosed-single-fragment",
                ("unclosed-single-fragment",),
            ),
        }

        for case, (value, secret_fragments) in cases.items():
            payload = render_report_card(
                report_card_fixture(decision=value), previous=None
            )
            rendered = json.dumps(payload, ensure_ascii=False, allow_nan=False)

            with self.subTest(case=case):
                for secret_fragment in secret_fragments:
                    self.assertNotIn(secret_fragment, rendered)
                self.assertIn("[REDACTED]", rendered)

    def test_report_card_contains_current_items_path_and_prior_comparison(self):
        report = report_card_fixture()
        previous = replace(
            report_card_fixture(signal="SELL", decision=None),
            event_id="report:2026-08-17:" + "b" * 64,
            trade_date=date(2026, 8, 17),
            state="degraded",
            report_path=Path("results/hermes/reports/2026-08-17.md"),
        )

        payload = render_report_card(report, previous=previous)

        body = self.assert_card(
            payload,
            "green",
            "TradingAgents 日报 | 2026-08-18",
            report.event_id,
        )
        for expected in (
            "ready",
            "BTC",
            "completed",
            "BUY",
            "Hold risk limit",
            "results/hermes/reports/2026-08-18.md",
            "2026-08-17",
            "SELL",
            "不可用",
            "仅用于研究和模拟交易，不构成交易建议",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, body)

    def test_report_card_normalizes_and_caps_each_free_item_field(self):
        fields = {
            "state": "state-" + "a" * 600,
            "symbol": "symbol-" + "b" * 600,
            "status": "status-" + "c" * 600,
            "signal": "signal-" + "d" * 600,
            "decision": "decision-" + "e" * 600,
            "error": "error-" + "f" * 600,
            "path": "path-" + "g" * 600,
        }
        report = ReportCardData(
            event_id="report:bounded-fields",
            trade_date=date(2026, 8, 18),
            state=fields["state"],
            items=(
                ReportCardItem(
                    symbol="symbol-" + "b" * 300 + "\r\n" + "b" * 300,
                    status=fields["status"],
                    processed_signal=fields["signal"],
                    final_trade_decision=fields["decision"],
                    error_code=fields["error"],
                ),
            ),
            report_path=Path(fields["path"]),
        )

        body = card_body(render_report_card(report, previous=None))

        self.assertNotIn("\r", body)
        self.assertIn("b" * 300 + " " + "b" * 190, body)
        for name, value in fields.items():
            if name == "symbol":
                continue
            with self.subTest(field=name):
                self.assertNotIn(value, body)
                self.assertIn(value[:500], body)

    def test_report_card_replaces_lone_surrogates_before_serialization(self):
        report = report_card_fixture(decision="before\ud800after")

        try:
            payload = render_report_card(report, previous=None)
        except Exception as error:
            raised = error
            payload = None
        else:
            raised = None

        self.assertIsNone(raised)
        body = card_body(payload)
        self.assertIn("before\ufffdafter", body)
        self.assertNotIn("\ud800", body)
        rendered = json.dumps(
            payload, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self.assertLessEqual(len(rendered), 20_000)

    def test_report_card_stays_bounded_with_many_items(self):
        items = tuple(
            ReportCardItem(
                symbol=f"SYMBOL-{index}-" + "界" * 600,
                status="completed-" + "界" * 600,
                processed_signal="BUY-" + "界" * 600,
                final_trade_decision="decision-" + "界" * 600,
                error_code="error-" + "界" * 600,
            )
            for index in range(100)
        )
        report = replace(report_card_fixture(), items=items)

        payload = render_report_card(report, previous=None)

        rendered = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(rendered), 20_000)
        self.assertIn(report.event_id, card_body(payload))
        self.assertIn("仅用于研究和模拟交易，不构成交易建议", card_body(payload))

    def test_report_card_bounds_json_escaping_and_signed_envelope(self):
        escape_heavy = '\"\\' * 300
        items = tuple(
            ReportCardItem(
                symbol=escape_heavy,
                status=escape_heavy,
                processed_signal=escape_heavy,
                final_trade_decision=escape_heavy,
                error_code=escape_heavy,
            )
            for _index in range(20)
        )
        report = replace(report_card_fixture(), items=items)

        payload = render_report_card(report, previous=None)
        rendered = json.dumps(
            payload, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")

        self.assertLessEqual(len(rendered), 20_000)
        body = card_body(payload)
        self.assertIn(report.event_id, body)
        self.assertIn("仅用于研究和模拟交易，不构成交易建议", body)
        self.assertIn("_其余内容因长度限制已省略_", body)
        self.assertEqual(payload, render_report_card(report, previous=None))

        transport = FakeTransport(TransportResponse(200, b'{"code":0}', None))
        FeishuClient(
            config_fixture(), transport=transport, clock=lambda: 1599360473
        ).send(payload)
        signed = json.dumps(
            transport.calls[0].payload,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.assertLessEqual(len(signed), 20_000)

    def test_failure_card_is_red_and_contains_only_safe_failure_metadata(self):
        event = NotificationEvent(
            event_id="execution_failure:daily_submit:execution-1",
            kind="execution_failure",
            created_at=datetime(2026, 8, 18, 0, 5, tzinfo=timezone.utc),
            job_name="daily\r\nsubmit DEEPSEEK_API_KEY=secret-marker",
            job_id="2d445dfc1a8a",
            execution_id="execution-sk-1234567890abcdefgh",
        )

        payload = render_failure_card(event)

        body = self.assert_card(
            payload,
            "red",
            "TradingAgents 定时任务失败",
            event.event_id,
        )
        self.assertIn("CRON_EXECUTION_FAILED", body)
        self.assertIn("daily submit", body)
        self.assertIn("2d445dfc1a8a", body)
        self.assertIn("2026-08-18T00:05:00+00:00", body)
        self.assertIn("服务器", body)
        self.assertNotIn("secret-marker", body)
        self.assertNotIn("sk-1234567890abcdefgh", body)

    def test_missing_archive_card_is_orange_and_contains_safe_state(self):
        event = NotificationEvent(
            event_id="missing_archive:2026-08-18:execution-2",
            kind="missing_archive",
            created_at=datetime(2026, 8, 18, 4, 5, tzinfo=timezone.utc),
            trade_date=date(2026, 8, 18),
            batch_state="active",
            job_name="daily_archive",
            job_id="5b7f7906306a",
            execution_id="execution-2",
        )

        payload = render_missing_archive_card(event)

        body = self.assert_card(
            payload,
            "orange",
            "TradingAgents 日报待归档",
            event.event_id,
        )
        for expected in (
            "2026-08-18",
            "active",
            "daily_archive",
            "5b7f7906306a",
            "execution-2",
            "sessions",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, body)

    def test_test_card_has_exact_title_orange_header_and_stable_event_id(self):
        event_id = "test:2026-08-18T12:00:00+08:00"
        now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

        payload = render_test_card(event_id, now)

        body = self.assert_card(
            payload,
            "orange",
            "TradingAgents 飞书通知配置验收",
            event_id,
        )
        self.assertIn("2026-08-18T12:00:00+00:00", body)


class FeishuClientTests(unittest.TestCase):
    def test_client_accepts_only_http_2xx_with_feishu_code_zero(self):
        transport = FakeTransport(TransportResponse(200, b'{"code":0}', None))
        client = FeishuClient(
            config_fixture(), transport=transport, clock=lambda: 1599360473
        )

        client.send({"msg_type": "interactive", "card": {}})

        call = transport.calls[0]
        self.assertEqual(call.url, config_payload()["webhook_url"])
        self.assertEqual(call.payload["timestamp"], "1599360473")
        self.assertEqual(
            call.payload["sign"],
            feishu_signature(1599360473, "unit-test-signing-secret"),
        )

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

    def test_client_discards_unsafe_429_retry_after_values(self):
        for retry_after in (-1, 86_401, True):
            client = FeishuClient(
                config_fixture(),
                transport=FakeTransport(
                    TransportResponse(429, b"rate limited", retry_after)
                ),
            )

            with self.subTest(retry_after=retry_after), self.assertRaises(
                FeishuDeliveryError
            ) as caught:
                client.send({"msg_type": "interactive", "card": {}})

            self.assertEqual(caught.exception.result, "rate_limited")
            self.assertIsNone(caught.exception.retry_after_seconds)

    def test_client_maps_transport_failures_without_raw_exception_leakage(self):
        cases = {
            "timeout": requests.exceptions.Timeout("timeout-secret-marker"),
            "connection_error": requests.exceptions.ConnectionError(
                "connection-secret-marker"
            ),
            "response_too_large": FeishuDeliveryError("response_too_large"),
        }

        for expected, transport_error in cases.items():
            client = FeishuClient(
                config_fixture(), transport=FakeTransport(transport_error)
            )
            with self.subTest(expected=expected), self.assertRaises(
                FeishuDeliveryError
            ) as caught:
                client.send({"msg_type": "interactive", "card": {}})
            self.assertEqual(caught.exception.result, expected)
            self.assertNotIn("secret-marker", str(caught.exception))
            self.assertIsNone(caught.exception.__context__)

    def test_client_rejects_redirect_http_invalid_and_feishu_errors(self):
        cases = (
            (TransportResponse(302, b"redirect details", None), "redirect_rejected"),
            (TransportResponse(500, b"server details", None), "http_error"),
            (TransportResponse(200, b"not json", None), "invalid_response"),
            (TransportResponse(200, b"[]", None), "invalid_response"),
            (TransportResponse(200, b'{"code":false}', None), "feishu_error"),
            (TransportResponse(200, b'{"code":19021}', None), "feishu_error"),
        )

        for response, expected in cases:
            client = FeishuClient(config_fixture(), transport=FakeTransport(response))
            with self.subTest(expected=expected), self.assertRaises(
                FeishuDeliveryError
            ) as caught:
                client.send({"msg_type": "interactive", "card": {}})
            self.assertEqual(caught.exception.result, expected)
            self.assertNotIn(response.body.decode("utf-8"), str(caught.exception))

    def test_client_rejects_oversized_payload_before_transport(self):
        transport = FakeTransport(TransportResponse(200, b'{"code":0}', None))
        client = FeishuClient(config_fixture(), transport=transport)

        with self.assertRaises(FeishuDeliveryError) as caught:
            client.send(
                {
                    "msg_type": "interactive",
                    "card": {"content": "界" * 20_000},
                }
            )

        self.assertIn(
            caught.exception.result,
            {
                "timeout",
                "connection_error",
                "redirect_rejected",
                "rate_limited",
                "http_error",
                "response_too_large",
                "invalid_response",
                "feishu_error",
            },
        )
        self.assertEqual(transport.calls, [])

    def test_client_rejects_nonfinite_payload_before_transport(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            transport = FakeTransport(
                TransportResponse(200, b'{"code":0}', None)
            )
            client = FeishuClient(config_fixture(), transport=transport)

            with self.subTest(value=value):
                with self.assertRaises(FeishuDeliveryError) as caught:
                    client.send({"msg_type": "interactive", "value": value})

                self.assertEqual(caught.exception.result, "http_error")
                self.assertEqual(transport.calls, [])
                self.assertNotIn(str(value), str(caught.exception).lower())

    def test_client_maps_lone_surrogate_payload_to_safe_error(self):
        transport = FakeTransport(TransportResponse(200, b'{"code":0}', None))
        client = FeishuClient(config_fixture(), transport=transport)

        with self.assertRaises(FeishuDeliveryError) as caught:
            client.send(
                {"msg_type": "interactive", "value": "before\ud800after"}
            )

        self.assertEqual(caught.exception.result, "http_error")
        self.assertEqual(transport.calls, [])
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("surrogate", str(caught.exception).lower())

    def test_client_maps_deeply_nested_outbound_json_to_safe_error(self):
        nested = "leaf"
        for _depth in range(10_000):
            nested = [nested]
        transport = FakeTransport(TransportResponse(200, b'{"code":0}', None))
        client = FeishuClient(config_fixture(), transport=transport)

        try:
            client.send({"msg_type": "interactive", "nested": nested})
        except Exception as error:
            raised = error
        else:
            raised = None

        self.assertIsInstance(raised, FeishuDeliveryError)
        self.assertEqual(raised.result, "http_error")
        self.assertEqual(transport.calls, [])
        self.assertIsNone(raised.__context__)
        self.assertNotIn("recursion", str(raised).lower())

    def test_client_rejects_nonstandard_json_constants_in_response(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            response = b'{"code":0,"value":' + constant + b"}"
            client = FeishuClient(
                config_fixture(),
                transport=FakeTransport(TransportResponse(200, response, None)),
            )

            with self.subTest(constant=constant):
                with self.assertRaises(FeishuDeliveryError) as caught:
                    client.send({"msg_type": "interactive", "card": {}})

                self.assertEqual(caught.exception.result, "invalid_response")
                self.assertNotIn(
                    constant.decode("ascii"), str(caught.exception)
                )

    def test_client_maps_deeply_nested_response_json_to_safe_error(self):
        depth = 10_000
        response = (
            b'{"code":0,"value":'
            + b"[" * depth
            + b"0"
            + b"]" * depth
            + b"}"
        )
        self.assertLess(len(response), 65_536)
        transport = FakeTransport(TransportResponse(200, response, None))
        client = FeishuClient(config_fixture(), transport=transport)

        try:
            client.send({"msg_type": "interactive", "card": {}})
        except Exception as error:
            raised = error
        else:
            raised = None

        self.assertIsInstance(raised, FeishuDeliveryError)
        self.assertEqual(raised.result, "invalid_response")
        self.assertEqual(len(transport.calls), 1)
        self.assertIsNone(raised.__context__)
        self.assertNotIn("recursion", str(raised).lower())


class RequestsTransportTests(unittest.TestCase):
    def test_retry_after_parser_accepts_only_bounded_integer_seconds(self):
        expected = {
            None: None,
            "": None,
            "0": 0,
            "17": 17,
            "000017": 17,
            "86400": 86_400,
            "86401": None,
            "-1": None,
            "+1": None,
            " 17 ": None,
            "1.5": None,
            "Wed, 21 Oct 2026 07:28:00 GMT": None,
            "9" * 100: None,
            "0" * 11: None,
        }

        for raw_value, parsed in expected.items():
            with self.subTest(raw_value=raw_value):
                self.assertEqual(parse_bounded_retry_after(raw_value), parsed)

    def test_transport_disables_environment_proxy_and_posts_utf8_json(self):
        transport = RequestsTransport()
        self.assertIs(transport.session.trust_env, False)

        with local_feishu_server() as (base_url, server):
            response = transport.post(
                base_url + "/ok", {"message": "飞书", "value": 1}
            )

        self.assertEqual(response, TransportResponse(200, b'{"code":0}', None))
        request = server.requests[0]
        self.assertEqual(request.path, "/ok")
        self.assertEqual(
            json.loads(request.body.decode("utf-8")),
            {"message": "飞书", "value": 1},
        )
        self.assertEqual(
            request.content_type, "application/json; charset=utf-8"
        )

    def test_transport_rejects_nonfinite_json_before_request(self):
        transport = RequestsTransport()

        with local_feishu_server() as (base_url, server):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    transport.post(base_url + "/ok", {"value": value})

        self.assertEqual(server.requests, [])

    def test_transport_returns_status_without_following_redirects(self):
        transport = RequestsTransport()

        with local_feishu_server() as (base_url, server):
            redirect = transport.post(base_url + "/redirect", {})
            rate_limited = transport.post(base_url + "/rate-limited", {})
            server_error = transport.post(base_url + "/server-error", {})

        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(server.redirect_target_requests, 0)
        self.assertEqual(rate_limited.retry_after_seconds, 17)
        self.assertEqual(server_error.status_code, 500)

    def test_transport_leaves_invalid_json_for_client_validation(self):
        transport = RequestsTransport()

        with local_feishu_server() as (base_url, _server):
            response = transport.post(base_url + "/invalid-json", {})

        self.assertEqual(response.body, b"not json")

    def test_transport_enforces_read_timeout(self):
        transport = RequestsTransport(connect_timeout=0.1, read_timeout=0.02)

        with local_feishu_server() as (base_url, _server), self.assertRaises(
            requests.exceptions.Timeout
        ):
            transport.post(base_url + "/timeout", {})

    def test_transport_maps_streamed_body_read_timeout_without_raw_context(self):
        transport = RequestsTransport(connect_timeout=0.1, read_timeout=0.02)

        with local_feishu_server() as (base_url, _server):
            try:
                transport.post(base_url + "/stream-timeout", {})
            except Exception as error:
                raised = error
            else:
                raised = None

        self.assertIsInstance(raised, requests.exceptions.Timeout)
        self.assertIsNone(raised.__context__)
        self.assertEqual(str(raised), "")

    def test_transport_rejects_response_over_65536_bytes(self):
        transport = RequestsTransport()

        with local_feishu_server() as (base_url, _server), self.assertRaises(
            FeishuDeliveryError
        ) as caught:
            transport.post(base_url + "/too-large", {})

        self.assertEqual(caught.exception.result, "response_too_large")


def write_private_config(directory, payload=None):
    secret_root = Path(directory) / "secrets"
    secret_root.mkdir(mode=0o700)
    path = secret_root / "feishu-notifier.yaml"
    path.write_text(
        yaml.safe_dump(config_payload() if payload is None else payload),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return secret_root, path


def other_owner(metadata):
    return SimpleNamespace(st_mode=metadata.st_mode, st_uid=metadata.st_uid + 1)


def exception_chain(error):
    chain = []
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend(
            related
            for related in (current.__cause__, current.__context__)
            if related is not None
        )
    return chain


def load_private_config_in_subprocess(path, sender):
    try:
        load_private_config(path)
    except Exception as error:
        sender.send(
            (
                type(error).__name__,
                str(error),
                error.__cause__ is None,
                error.__context__ is None,
            )
        )
    else:
        sender.send(("accepted",))
    finally:
        sender.close()


class FeishuNotifierConfigTests(unittest.TestCase):
    def test_signature_matches_fixed_vector(self):
        self.assertEqual(
            feishu_signature(1599360473, "test-secret"),
            "wSds2BzzFIIGf/WrhUO+NI1q/9j+FRJd3JNHKAq0NZY=",
        )

    def test_valid_config_is_frozen(self):
        config = FeishuNotifierConfig.model_validate(config_payload())

        self.assertEqual(config.jobs, VALID_JOBS)
        with self.assertRaises(ValidationError):
            config.version = 2

    def test_valid_config_jobs_preserve_dict_contract(self):
        config = FeishuNotifierConfig.model_validate(config_payload())

        with self.subTest("annotation"):
            self.assertEqual(
                FeishuNotifierConfig.model_fields["jobs"].annotation,
                dict[str, str],
            )
        with self.subTest("runtime type"):
            self.assertIsInstance(config.jobs, dict)
        copied = config.jobs.copy()
        self.assertIs(type(copied), dict)
        self.assertEqual(copied, VALID_JOBS)
        copied["daily_submit"] = "copy-can-change"
        self.assertEqual(config.jobs["daily_submit"], VALID_JOBS["daily_submit"])
        self.assertEqual(config.jobs, VALID_JOBS)
        self.assertEqual(set(config.jobs.values()), set(VALID_JOBS.values()))
        self.assertEqual(config.model_dump(mode="json"), config_payload())
        self.assertEqual(json.loads(config.model_dump_json()), config_payload())

    def test_valid_config_jobs_reject_all_normal_dict_mutators(self):
        operations = {
            "item assignment": lambda jobs: operator.setitem(
                jobs, "daily_submit", "not-valid"
            ),
            "item deletion": lambda jobs: operator.delitem(
                jobs, "daily_submit"
            ),
            "clear": lambda jobs: jobs.clear(),
            "pop": lambda jobs: jobs.pop("daily_submit"),
            "popitem": lambda jobs: jobs.popitem(),
            "setdefault": lambda jobs: jobs.setdefault("extra", "not-valid"),
            "update": lambda jobs: jobs.update(
                {"daily_submit": "not-valid"}
            ),
            "in-place union": lambda jobs: operator.ior(
                jobs, {"daily_submit": "not-valid"}
            ),
        }

        for operation_name, operation in operations.items():
            config = FeishuNotifierConfig.model_validate(config_payload())
            try:
                operation(config.jobs)
            except Exception as error:
                mutation_error = error
            else:
                mutation_error = None

            with self.subTest(operation=operation_name):
                self.assertIsInstance(mutation_error, TypeError)
                self.assertEqual(config.jobs, VALID_JOBS)

    def test_input_jobs_mutation_does_not_affect_validated_config(self):
        source_jobs = dict(VALID_JOBS)
        config = FeishuNotifierConfig.model_validate(
            {**config_payload(), "jobs": source_jobs}
        )

        source_jobs["daily_submit"] = "not-valid"
        source_jobs.clear()

        self.assertEqual(config.jobs, VALID_JOBS)

    def test_config_rejects_non_feishu_urls(self):
        for url in (
            "http://open.feishu.cn/open-apis/bot/v2/hook/"
            "00000000-0000-0000-0000-000000000000",
            "https://example.com/open-apis/bot/v2/hook/"
            "00000000-0000-0000-0000-000000000000",
            "https://open.feishu.cn@evil.example/open-apis/bot/v2/hook/x",
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                FeishuNotifierConfig.model_validate(
                    {**config_payload(), "webhook_url": url}
                )

    def test_config_rejects_userinfo_on_otherwise_valid_urls(self):
        path = "/open-apis/bot/v2/hook/0000000000000000"
        for userinfo in ("user@", "user:pass@"):
            with self.subTest(userinfo=userinfo), self.assertRaises(
                ValidationError
            ):
                FeishuNotifierConfig.model_validate(
                    {
                        **config_payload(),
                        "webhook_url": f"https://{userinfo}open.feishu.cn{path}",
                    }
                )

    def test_config_rejects_explicit_empty_port(self):
        url = config_payload()["webhook_url"].replace(
            "open.feishu.cn", "open.feishu.cn:"
        )

        with self.assertRaises(ValidationError):
            FeishuNotifierConfig.model_validate(
                {**config_payload(), "webhook_url": url}
            )

    def test_config_rejects_query_fragment_and_non_default_port(self):
        base_url = config_payload()["webhook_url"]
        for url in (
            f"{base_url}?token=not-allowed",
            f"{base_url}#fragment",
            base_url.replace("open.feishu.cn", "open.feishu.cn:8443"),
        ):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                FeishuNotifierConfig.model_validate(
                    {**config_payload(), "webhook_url": url}
                )

    def test_config_rejects_empty_query_or_fragment_delimiters(self):
        base_url = config_payload()["webhook_url"]
        for suffix in ("?", "#", "?#"):
            with self.subTest(suffix=suffix), self.assertRaises(ValidationError):
                FeishuNotifierConfig.model_validate(
                    {**config_payload(), "webhook_url": f"{base_url}{suffix}"}
                )

    def test_config_rejects_raw_url_spaces_and_ascii_controls(self):
        base_url = config_payload()["webhook_url"]
        invalid_urls = {
            "leading space": f" {base_url}",
            "leading NUL": f"\x00{base_url}",
            "leading tab": f"\t{base_url}",
            "embedded newline": base_url.replace("open-apis", "open-\napis"),
            "embedded tab": base_url.replace("/hook/", "/hook/\t"),
        }

        for case, url in invalid_urls.items():
            with self.subTest(case=case), self.assertRaises(ValidationError):
                FeishuNotifierConfig.model_validate(
                    {**config_payload(), "webhook_url": url}
                )

    def test_config_rejects_noncanonical_webhook_paths(self):
        prefix = "https://open.feishu.cn"
        for path in (
            "/open-apis/bot/v2/hook/short",
            "/open-apis/bot/v2/hook/0000000000000000/",
            "/open-apis//bot/v2/hook/0000000000000000",
            "/open-apis/bot/v2/hook/%30%30%30%30%30%30%30%30%30%30%30%30%30%30%30%30",
        ):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                FeishuNotifierConfig.model_validate(
                    {**config_payload(), "webhook_url": f"{prefix}{path}"}
                )

    def test_config_rejects_empty_or_control_character_secrets(self):
        for secret in ("", "line\nbreak", "tab\tsecret", "delete\x7fsecret"):
            with self.subTest(secret=repr(secret)), self.assertRaises(
                ValidationError
            ):
                FeishuNotifierConfig.model_validate(
                    {**config_payload(), "signing_secret": secret}
                )

    def test_config_rejects_boolean_version(self):
        with self.assertRaises(ValidationError):
            FeishuNotifierConfig.model_validate(
                {**config_payload(), "version": True}
            )

    def test_config_rejects_non_string_values_without_coercion(self):
        payload = config_payload()
        bytes_key_jobs = {
            (key.encode("ascii") if key == "daily_submit" else key): value
            for key, value in VALID_JOBS.items()
        }
        integer_key_jobs = {
            (1 if key == "daily_submit" else key): value
            for key, value in VALID_JOBS.items()
        }
        invalid_payloads = {
            "bytes webhook URL": {
                **payload,
                "webhook_url": payload["webhook_url"].encode("ascii"),
            },
            "bytes signing secret": {
                **payload,
                "signing_secret": b"unit-test-signing-secret",
            },
            "bytes job name": {**payload, "jobs": bytes_key_jobs},
            "bytes job ID": {
                **payload,
                "jobs": {
                    **VALID_JOBS,
                    "daily_submit": b"2d445dfc1a8a",
                },
            },
            "integer webhook URL": {**payload, "webhook_url": 1},
            "integer signing secret": {**payload, "signing_secret": 1},
            "integer job name": {**payload, "jobs": integer_key_jobs},
            "integer job ID": {
                **payload,
                "jobs": {**VALID_JOBS, "daily_submit": 1},
            },
        }

        for case, invalid_payload in invalid_payloads.items():
            with self.subTest(case=case), self.assertRaises(ValidationError):
                FeishuNotifierConfig.model_validate(invalid_payload)

    def test_config_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            FeishuNotifierConfig.model_validate(
                {**config_payload(), "unexpected": "not allowed"}
            )

    def test_config_rejects_missing_duplicate_or_malformed_job_ids(self):
        invalid_jobs = (
            {key: value for key, value in VALID_JOBS.items() if key != "review_memory"},
            {**VALID_JOBS, "review_memory": VALID_JOBS["review_processor"]},
            {**VALID_JOBS, "review_memory": "not-a-job-id"},
        )

        for jobs in invalid_jobs:
            with self.subTest(jobs=jobs), self.assertRaises(ValidationError):
                FeishuNotifierConfig.model_validate(
                    {**config_payload(), "jobs": jobs}
                )


class PrivateFeishuConfigTests(unittest.TestCase):
    def assert_config_unavailable(self, callback):
        with self.assertRaises(FeishuConfigError) as raised:
            callback()
        self.assertEqual(
            str(raised.exception),
            "Feishu notifier configuration unavailable",
        )
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)

    def test_private_config_requires_regular_owner_only_file(self):
        with TemporaryDirectory() as directory:
            _, path = write_private_config(directory)
            path.chmod(0o644)
            self.assert_config_unavailable(lambda: load_private_config(path))
            path.chmod(0o600)
            self.assertEqual(load_private_config(path).jobs, VALID_JOBS)
            link = Path(directory) / "link.yaml"
            link.symlink_to(path)
            self.assert_config_unavailable(lambda: load_private_config(link))

    def test_private_config_error_does_not_expose_secret_context(self):
        marker = "LEAKMARK1739"
        payload = {
            **config_payload(),
            "signing_secret": marker + "x" * 513,
        }
        with TemporaryDirectory() as directory:
            _, path = write_private_config(directory, payload)

            with self.assertRaises(FeishuConfigError) as raised:
                load_private_config(path)

        error = raised.exception
        self.assertEqual(
            str(error), "Feishu notifier configuration unavailable"
        )
        self.assertIsNone(error.__cause__)
        with self.subTest("context"):
            self.assertIsNone(error.__context__)
        with self.subTest("public representation"):
            self.assertNotIn(marker, repr(error))
        with self.subTest("recursive exception chain"):
            self.assertNotIn(
                marker,
                "\n".join(repr(item) for item in exception_chain(error)),
            )

    def test_private_config_rejects_boolean_version(self):
        with TemporaryDirectory() as directory:
            _, path = write_private_config(
                directory, {**config_payload(), "version": True}
            )

            self.assert_config_unavailable(lambda: load_private_config(path))

    def test_private_config_rejects_yaml_binary_string_values(self):
        payload = config_payload()
        binary_key_jobs = {
            (key.encode("ascii") if key == "daily_submit" else key): value
            for key, value in VALID_JOBS.items()
        }
        invalid_payloads = {
            "binary webhook URL": {
                **payload,
                "webhook_url": payload["webhook_url"].encode("ascii"),
            },
            "binary signing secret": {
                **payload,
                "signing_secret": b"unit-test-signing-secret",
            },
            "binary job name": {**payload, "jobs": binary_key_jobs},
            "binary job ID": {
                **payload,
                "jobs": {
                    **VALID_JOBS,
                    "daily_submit": b"2d445dfc1a8a",
                },
            },
        }

        for case, invalid_payload in invalid_payloads.items():
            with self.subTest(case=case), TemporaryDirectory() as directory:
                self.assertIn("!!binary", yaml.safe_dump(invalid_payload))
                _, path = write_private_config(directory, invalid_payload)

                self.assert_config_unavailable(
                    lambda: load_private_config(path)
                )

    def test_private_config_rejects_non_regular_file(self):
        with TemporaryDirectory() as directory:
            secret_root = Path(directory) / "secrets"
            secret_root.mkdir(mode=0o700)
            path = secret_root / "feishu-notifier.yaml"
            path.mkdir(mode=0o600)

            self.assert_config_unavailable(lambda: load_private_config(path))

    def test_private_config_rejects_fifo_without_blocking(self):
        with TemporaryDirectory() as directory:
            secret_root = Path(directory) / "secrets"
            secret_root.mkdir(mode=0o700)
            path = secret_root / "feishu-notifier.yaml"
            os.mkfifo(path, mode=0o600)
            context = get_context("fork")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=load_private_config_in_subprocess,
                args=(path, sender),
            )
            process.start()
            sender.close()
            process.join(timeout=2)
            blocked = process.is_alive()
            if blocked:
                process.terminate()
                process.join(timeout=2)
            try:
                result = receiver.recv() if receiver.poll() else None
            except EOFError:
                result = None
            has_result = result is not None
            receiver.close()
            process.close()

        self.assertFalse(blocked, "FIFO config open blocked without a writer")
        self.assertTrue(has_result)
        self.assertEqual(
            result,
            (
                "FeishuConfigError",
                "Feishu notifier configuration unavailable",
                True,
                True,
            ),
        )

    def test_private_config_requires_exact_parent_mode(self):
        for mode in (0o750, 0o701):
            with self.subTest(mode=oct(mode)), TemporaryDirectory() as directory:
                secret_root, path = write_private_config(directory)
                secret_root.chmod(mode)

                self.assert_config_unavailable(lambda: load_private_config(path))

    def test_private_config_rejects_symlink_parent(self):
        with TemporaryDirectory() as directory:
            real_root = Path(directory) / "real-secrets"
            real_root.mkdir(mode=0o700)
            real_path = real_root / "feishu-notifier.yaml"
            real_path.write_text(yaml.safe_dump(config_payload()), encoding="utf-8")
            real_path.chmod(0o600)
            linked_root = Path(directory) / "linked-secrets"
            linked_root.symlink_to(real_root, target_is_directory=True)

            self.assert_config_unavailable(
                lambda: load_private_config(linked_root / real_path.name)
            )

    def test_private_config_rejects_wrong_file_owner(self):
        with TemporaryDirectory() as directory:
            _, path = write_private_config(directory)
            real_fstat = os.fstat

            with patch(
                "tradingagents.integrations.hermes_feishu_client.os.fstat",
                side_effect=lambda descriptor: other_owner(real_fstat(descriptor)),
            ):
                self.assert_config_unavailable(lambda: load_private_config(path))

    def test_private_config_rejects_wrong_parent_owner(self):
        with TemporaryDirectory() as directory:
            _, path = write_private_config(directory)
            real_lstat = os.lstat

            with patch(
                "tradingagents.integrations.hermes_feishu_client.os.lstat",
                side_effect=lambda candidate: other_owner(real_lstat(candidate)),
            ):
                self.assert_config_unavailable(lambda: load_private_config(path))

    def test_private_config_wraps_open_parse_and_validation_failures(self):
        with TemporaryDirectory() as directory:
            secret_root = Path(directory) / "secrets"
            secret_root.mkdir(mode=0o700)
            missing_path = secret_root / "missing.yaml"
            self.assert_config_unavailable(
                lambda: load_private_config(missing_path)
            )

        for contents in (
            "signing_secret: [unterminated",
            yaml.safe_dump({**config_payload(), "secret-leaking-field": True}),
            yaml.safe_dump(
                {
                    key: value
                    for key, value in config_payload().items()
                    if key != "version"
                }
            ),
        ):
            with TemporaryDirectory() as directory:
                _, path = write_private_config(directory)
                path.write_text(contents, encoding="utf-8")
                path.chmod(0o600)

                self.assert_config_unavailable(lambda: load_private_config(path))


if __name__ == "__main__":
    unittest.main()
