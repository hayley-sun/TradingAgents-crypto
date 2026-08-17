import json
import operator
import os
import unittest
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from tradingagents.integrations.hermes_feishu_client import (
    FeishuConfigError,
    FeishuNotifierConfig,
    feishu_signature,
    load_private_config,
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
