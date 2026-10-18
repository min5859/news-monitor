"""Synthetic metadata fixtures only; no live API requests."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from news_monitor import collect as c

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def article(title="효성 조현준 회장, 테스트 행사 참석", url="https://example.com/news/1", seen="20261016T010000Z"):
    return {"title": title, "url": url, "domain": "example.com", "seendate": seen, "language": "Korean"}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def opener_for(rows):
    return lambda *args, **kwargs: Response(json.dumps({"articles": rows}).encode())


class CollectionTests(unittest.TestCase):
    def run_collection(self, rows, config=None):
        return c.collect(config or CONFIG, NOW, opener=opener_for(rows), sleeper=lambda _: None)

    def test_current_target_is_selected_and_observation_is_not_publication(self):
        result = self.run_collection([article()])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["statistics"]["selected"], 1)
        self.assertIsNone(result["articles"][0]["published_at"])
        self.assertEqual(result["articles"][0]["analysis_scope"], "headline_only")
        self.assertNotIn("body", result["articles"][0])

    def test_person_and_company_are_both_required(self):
        result = self.run_collection([article("다른 회사 조현준 소식"), article("효성 회사 일반 소식")])
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["statistics"]["not_target"], 2)

    def test_english_alias(self):
        result = self.run_collection([article("Hyosung chairman Cho Hyun-joon visits plant")])
        self.assertEqual(len(result["articles"]), 1)

    def test_tracking_urls_deduplicate(self):
        result = self.run_collection([article(url="https://example.com/news/1?utm_source=a#top"), article()])
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["statistics"]["duplicates"], 1)

    def test_meaningful_query_parameters_are_preserved(self):
        one = article(url="https://example.com/news?ref=1")
        two = article(url="https://example.com/news?ref=2")
        self.assertEqual(len(self.run_collection([one, two])["articles"]), 2)

    def test_oversized_response_is_a_failure(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["max_response_bytes"] = 10
        self.assertEqual(self.run_collection([article()], cfg)["status"], "failed")

    def test_different_publishers_are_not_collapsed_by_title(self):
        row = {**article(url="https://other.example/news/1"), "domain": "other.example"}
        result = self.run_collection([article(), row])
        self.assertEqual(len(result["articles"]), 2)
        self.assertEqual(len(result["publishers"]), 2)

    def test_old_and_future_observations_are_excluded(self):
        result = self.run_collection([article(seen="20260901T010000Z"), article(seen="20261018T010000Z")])
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["statistics"]["out_of_window"], 2)

    def test_missing_or_invalid_metadata_is_not_normal_empty(self):
        for field, value in (("seendate", "bad"), ("url", "javascript:alert(1)"), ("title", ""), ("domain", "wrong.example")):
            with self.subTest(field=field):
                result = self.run_collection([{**article(), field: value}])
                self.assertEqual(result["status"], "failed")

    def test_partial_invalid_rows_are_visible(self):
        result = self.run_collection([article(), {**article(), "url": "bad"}])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["statistics"]["invalid_metadata"], 1)

    def test_missing_articles_envelope_is_failure(self):
        result = c.collect(CONFIG, NOW, opener=lambda *a, **k: Response(b'{"error":"rate limited"}'), sleeper=lambda _: None)
        self.assertEqual(result["status"], "failed")

    def test_html_response_is_failure(self):
        result = c.collect(CONFIG, NOW, opener=lambda *a, **k: Response(b"<html>challenge</html>"), sleeper=lambda _: None)
        self.assertEqual(result["status"], "failed")

    def test_valid_empty_response_is_explicit(self):
        result = self.run_collection([])
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["articles"], [])

    def test_max_articles_selects_latest_deterministically(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["max_articles"] = 1
        older = article(url="https://example.com/old", seen="20261014T010000Z")
        newer = article(url="https://example.com/new", seen="20261016T010000Z")
        result = self.run_collection([older, newer], cfg)
        self.assertEqual(result["articles"][0]["url"], newer["url"])
        self.assertEqual(result["statistics"]["limit_excluded"], 1)

    def test_upstream_limit_is_a_coverage_warning(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["max_results_per_query"] = 1
        result = self.run_collection([article()], cfg)
        self.assertEqual(result["status"], "partial")
        self.assertIn("upstream_limit_reached", result["warnings"])

    def test_query_failure_does_not_hide_successful_other_query(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["queries"] = ["Hyosung", "other"]
        cfg["collection"]["attempts"] = 1
        count = 0

        def request(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise HTTPError("https://example.com", 400, "Bad query", {}, None)
            return opener_for([article()])()

        result = c.collect(cfg, NOW, opener=request, sleeper=lambda _: None)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["articles"]), 1)

    def test_final_server_failure_preserves_retry_after(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["attempts"] = 1
        cfg["collection"]["queries"] = ["first", "second"]
        calls = []

        def request(*args, **kwargs):
            calls.append(1)
            raise HTTPError("https://example.com", 503, "Unavailable", {"Retry-After": "3600"}, None)

        result = c.collect(cfg, NOW, opener=request, sleeper=lambda _: None)
        self.assertEqual(result["retry_after_seconds"], 3600)
        self.assertEqual(len(calls), 1)

    def test_retry_after_is_respected(self):
        sleeps = []
        count = 0

        def request(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise HTTPError("https://example.com", 503, "Unavailable", {"Retry-After": "12"}, None)
            return opener_for([article()])()

        result = c.collect(CONFIG, NOW, opener=request, sleeper=sleeps.append)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(sleeps, [12])

    def test_429_stops_retries_and_remaining_queries(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["queries"] = ["Hyosung", "other"]
        calls = []

        def request(*args, **kwargs):
            calls.append(1)
            raise HTTPError("https://example.com", 429, "Limited", {"Retry-After": "3600"}, None)

        result = c.collect(cfg, NOW, opener=request, sleeper=lambda _: self.fail("must not retry"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["retry_after_seconds"], 3600)
        self.assertIn("remaining_queries_skipped_rate_limit", result["warnings"])

    def test_single_publisher_does_not_claim_coverage_goal_met(self):
        result = self.run_collection([article()])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage_goals"]["actual_publishers"], 1)
        self.assertFalse(result["coverage_goals"]["publisher_goal_met"])
        self.assertIn("publisher_goal_not_met", result["warnings"])

    def test_429_without_header_uses_configured_cooldown(self):
        def request(*args, **kwargs):
            raise HTTPError("https://example.com", 429, "Limited", {}, None)

        result = c.collect(CONFIG, NOW, opener=request, sleeper=lambda _: self.fail("must not retry"))
        self.assertEqual(result["retry_after_seconds"], CONFIG["collection"]["rate_limit_cooldown_seconds"])

    def test_429_after_success_preserves_partial_articles(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["queries"] = ["first", "second", "third"]
        calls = []

        def request(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise HTTPError("https://example.com", 429, "Limited", {}, None)
            return opener_for([article()])()

        result = c.collect(cfg, NOW, opener=request, sleeper=lambda _: None)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["statistics"]["selected"], 1)
        self.assertEqual(result["source_queries"], {"planned": 3, "attempted": 2, "succeeded": 1})
        self.assertEqual(len(calls), 2)

    def test_relevance_exclusions_are_visible_without_relaxing_selection(self):
        result = self.run_collection([article(), article("효성 공장 준공"),
                                      article("조현준 행사 참석"), article("다른 기업 소식")])
        self.assertEqual(result["statistics"]["selected"], 1)
        self.assertEqual(result["statistics"]["organization_only"], 1)
        self.assertEqual(result["statistics"]["person_only"], 1)
        self.assertEqual(result["statistics"]["neither_target"], 1)

    def test_long_retry_after_does_not_retry_early(self):
        calls = []
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["queries"] = ["first", "second"]

        def request(*args, **kwargs):
            calls.append(1)
            raise HTTPError("https://example.com", 503, "Unavailable", {"Retry-After": "3600"}, None)

        result = c.collect(cfg, NOW, opener=request, sleeper=lambda _: self.fail("must not sleep"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["retry_after_seconds"], 3600)
        self.assertIn("remaining_queries_skipped_retry_after", result["warnings"])

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

    def test_nonempty_collection_writes_only_explicit_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_collection([{**article(), "socialimage": "secret", "body": "not collected"}])
            c.write_result(result, Path(temp))
            data = json.loads((Path(temp) / "articles.json").read_text())
            self.assertNotIn("body", data["articles"][0])
            self.assertNotIn("socialimage", data["articles"][0])

    def test_requests_are_bounded_and_local_utc_window_is_explicit(self):
        requests = []

        def request(req, timeout):
            requests.append((req.full_url, timeout))
            return opener_for([])()

        result = c.collect(CONFIG, NOW, opener=request, sleeper=lambda _: None)
        self.assertIn(f"timespan={CONFIG['collection']['lookback_days']}d", requests[0][0])
        self.assertEqual(result["window_end"], "2026-10-17T12:00:00+00:00")
        self.assertEqual(requests[0][1], CONFIG["collection"]["timeout_seconds"])

    def test_private_and_credential_urls_are_rejected(self):
        for url in ("http://127.0.0.1/x", "https://user:pass@example.com/x", "http://localhost/x"):
            with self.subTest(url=url):
                self.assertRaises(ValueError, c.canonical_url, url, CONFIG["collection"])


class RunTests(unittest.TestCase):
    def test_429_cooldown_persists_across_runs_and_preserves_articles(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "articles.json").write_text('{"previous":true}')
            calls = []

            def request(*args, **kwargs):
                calls.append(1)
                raise HTTPError("https://example.com", 429, "Limited", {"Retry-After": "3600"}, None)

            first = c.run_collection(CONFIG, root, now=NOW, opener=request)
            second = c.run_collection(CONFIG, root, now=NOW, opener=request)
            self.assertEqual(len(calls), 1)
            self.assertEqual(first["errors"][0]["error"], "http_429")
            self.assertEqual(second["errors"][0]["error"], "request_cooldown")
            self.assertEqual(second["errors"][0]["reason"], "http_429")
            self.assertEqual(second["source_queries"]["attempted"], 0)
            self.assertEqual(second["retry_not_before"], first["retry_not_before"])
            self.assertEqual((root / "articles.json").read_text(), '{"previous":true}')

    def test_completed_run_is_paced_across_invocations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = c.run_collection(CONFIG, root, now=NOW, opener=opener_for([]))
            second = c.run_collection(CONFIG, root, now=NOW,
                                      opener=lambda *a, **k: self.fail("too soon"))
            self.assertEqual(first["status"], "empty")
            self.assertEqual(second["errors"][0]["error"], "request_cooldown")

    def test_expired_cooldown_allows_next_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            c.run_collection(CONFIG, root, now=NOW, opener=opener_for([]))
            later = NOW + timedelta(seconds=CONFIG["collection"]["minimum_run_interval_seconds"] + 1)
            result = c.run_collection(CONFIG, root, now=later, opener=opener_for([]))
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
