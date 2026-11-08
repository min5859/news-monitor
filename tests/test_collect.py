"""Synthetic metadata fixtures only; no live API requests."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from test_discovery import CONFIG, NOW, article, run_search, tavily_opener

from news_monitor import collect as c


class CollectionTests(unittest.TestCase):
    def test_only_tavily_feeds_is_a_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            for provider in ("gdelt_doc", "codex_web_search", "other"):
                cfg = copy.deepcopy(CONFIG)
                cfg["collection"]["provider"] = provider
                path.write_text(json.dumps(cfg))
                with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "unsupported provider"):
                    c.load_config(path)
        self.assertEqual(c.load_config(c.ROOT / "config.json")["collection"]["provider"], "tavily_feeds")

    def test_collection_time_must_be_timezone_aware(self):
        with self.assertRaises(ValueError):
            c.collect(CONFIG, datetime(2026, 10, 18, 12), opener=tavily_opener([]), sleeper=lambda _: None)

    def test_title_ending_with_ampersand_word_is_kept(self):
        # HTMLParser holds back a trailing "&X" as a possibly unfinished entity until close().
        for title in ("효성 조현준 회장, 美 HVDC M&A", "Hyosung Chairman Cho Hyun-joon Expands R&D", "효성 조현준 회장 &amp; R&D"):
            with self.subTest(title=title):
                self.assertEqual(c.plain_title(title), title.replace("&amp;", "&"))
        self.assertEqual(c.plain_title("<b>효성</b> 조현준 &lt;회장&gt;"), "효성 조현준 <회장>")
        self.assertEqual(len(run_search([article("효성 조현준 회장, 美 HVDC M&A")])["articles"]), 1)

    def test_tracking_urls_deduplicate(self):
        result = run_search([article(url="https://example.com/news/1?utm_source=a#top"), article()])
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["statistics"]["duplicates"], 1)

    def test_meaningful_query_parameters_are_preserved(self):
        one = article(title="가상 테스트: 효성 조현준 회장 소식 하나", url="https://example.com/news?ref=1")
        two = article(title="가상 테스트: 효성 조현준 회장 소식 둘", url="https://example.com/news?ref=2")
        self.assertEqual(len(run_search([one, two])["articles"]), 2)

    def test_oversized_response_is_a_failure(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["max_response_bytes"] = 10
        self.assertEqual(run_search([article()], cfg)["status"], "failed")

    def test_different_publishers_are_not_collapsed_by_title(self):
        result = run_search([article(), article(url="https://other.example/news/1")])
        self.assertEqual(len(result["articles"]), 2)
        self.assertEqual(len(result["publishers"]), 2)

    def test_retry_after_is_respected_within_the_budget(self):
        sleeps, calls = [], []
        answer = tavily_opener([article()])

        def request(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError("https://example.com", 503, "Unavailable", {"Retry-After": "12"}, None)
            return answer(*args, **kwargs)

        result = c.collect(CONFIG, NOW, opener=request, sleeper=sleeps.append)
        self.assertEqual((result["status"], len(calls), sleeps), ("ok", 2, [12]))

    def test_long_retry_after_does_not_retry_early(self):
        calls = []

        def request(*args, **kwargs):
            calls.append(1)
            raise HTTPError("https://example.com", 503, "Unavailable", {"Retry-After": "3600"}, None)

        result = c.collect(CONFIG, NOW, opener=request, sleeper=lambda _: self.fail("must not sleep"))
        self.assertEqual((result["status"], len(calls)), ("failed", 1))
        self.assertEqual(result["errors"][0]["error"], "http_503:retry_after_exceeds_budget")

    def test_nonempty_collection_writes_only_explicit_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_search([{**article(), "socialimage": "secret", "body": "not collected"}])
            c.write_result(result, Path(temp))
            data = json.loads((Path(temp) / "articles.json").read_text())
            self.assertNotIn("body", data["articles"][0])
            self.assertNotIn("socialimage", data["articles"][0])

    def test_failure_and_empty_preserve_existing_articles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "articles.json"
            output.write_text('{"old":true}')
            for state in ("failed", "empty"):
                result = {"status": state, "articles": [], "statistics": {"selected": 0}}
                c.write_result(result, root)
                self.assertEqual(output.read_text(), '{"old":true}')
                self.assertEqual(json.loads((root / "collection_status.json").read_text())["status"], state)

    def test_private_and_credential_urls_are_rejected(self):
        for url in ("http://127.0.0.1/x", "https://user:pass@example.com/x", "http://localhost/x"):
            with self.subTest(url=url):
                self.assertRaises(ValueError, c.canonical_url, url, CONFIG["collection"])


class RunTests(unittest.TestCase):
    def test_429_fails_without_retry_and_preserves_articles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "articles.json").write_text('{"previous":true}')
            calls = []

            def request(*args, **kwargs):
                calls.append(1)
                raise HTTPError("https://example.com", 429, "Limited", {"Retry-After": "3600"}, None)

            result = c.run_collection(CONFIG, root, now=NOW, opener=request, sleeper=lambda _: self.fail("must not retry"))
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["errors"][0]["error"], "http_429")
            self.assertEqual((root / "articles.json").read_text(), '{"previous":true}')

    def test_completed_run_is_paced_across_invocations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "articles.json").write_text('{"previous":true}')
            first = c.run_collection(CONFIG, root, now=NOW, opener=tavily_opener([]))
            second = c.run_collection(CONFIG, root, now=NOW,
                                      opener=lambda *a, **k: self.fail("too soon"))
            self.assertEqual(first["status"], "empty")
            self.assertEqual(second["errors"][0]["error"], "request_cooldown")
            self.assertEqual(second["errors"][0]["reason"], "minimum_run_interval")
            self.assertEqual(second["source_queries"], {"planned": 1, "attempted": 0, "succeeded": 0})
            self.assertEqual(second["retry_not_before"], first["retry_not_before"])
            self.assertEqual((root / "articles.json").read_text(), '{"previous":true}')

    def test_expired_cooldown_allows_next_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            c.run_collection(CONFIG, root, now=NOW, opener=tavily_opener([]))
            later = NOW + timedelta(seconds=CONFIG["collection"]["minimum_run_interval_seconds"] + 1)
            result = c.run_collection(CONFIG, root, now=later, opener=tavily_opener([]))
            self.assertEqual(result["status"], "empty")

    def test_corrupt_request_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "request_state.json").write_text("bad json")
            self.assertRaises(ValueError, c.run_collection, CONFIG, root, now=NOW,
                              opener=lambda *a, **k: self.fail("must not request"))

    def test_lock_prevents_concurrent_writer(self):
        import fcntl

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status = root / "collection_status.json"
            status.write_text('{"active":true}')
            with (root / ".collection.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertRaises(BlockingIOError, c.run_collection, CONFIG, root,
                                  opener=lambda *a, **k: self.fail("concurrent request"))
            self.assertEqual(status.read_text(), '{"active":true}')

    def test_invalid_state_is_recorded_as_failure_by_cli(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "request_state.json").write_text("[]")
            with patch("sys.argv", ["collect", "--data-dir", str(root)]), redirect_stdout(io.StringIO()):
                self.assertEqual(c.main(), 1)
            result = json.loads((root / "collection_status.json").read_text())
            self.assertEqual(result["errors"][0]["error"], "ValueError")

    def test_nonfinite_and_unsafe_timing_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg_path = Path(temp) / "config.json"
            for value in (float("inf"), float("nan"), 0, 1):
                cfg = copy.deepcopy(CONFIG)
                cfg["collection"]["minimum_run_interval_seconds"] = value
                cfg_path.write_text(json.dumps(cfg))
                with self.subTest(value=value):
                    self.assertRaises(ValueError, c.load_config, cfg_path)

    def test_bad_config_replaces_stale_success_status(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = root / "bad.json"
            cfg.write_text("bad json")
            (root / "collection_status.json").write_text('{"status":"ok"}')
            (root / "articles.json").write_text('{"previous":true}')
            with patch("sys.argv", ["collect", "--config", str(cfg), "--data-dir", str(root)]), redirect_stdout(io.StringIO()):
                self.assertEqual(c.main(), 1)
            result = json.loads((root / "collection_status.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertEqual((root / "articles.json").read_text(), '{"previous":true}')


if __name__ == "__main__":
    unittest.main()
