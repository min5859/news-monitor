"""Synthetic metadata fixtures only; no live API requests."""

import copy
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
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
                raise HTTPError("https://example.com", 503, "Unavailable", {}, None)
            return opener_for([article()])()

        result = c.collect(cfg, NOW, opener=request, sleeper=lambda _: None)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["articles"]), 1)

    def test_retry_after_is_respected(self):
        sleeps = []
        count = 0

        def request(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise HTTPError("https://example.com", 429, "Limited", {"Retry-After": "12"}, None)
            return opener_for([article()])()

        result = c.collect(CONFIG, NOW, opener=request, sleeper=sleeps.append)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(sleeps, [12])

    def test_long_retry_after_does_not_retry_early(self):
        calls = []

        def request(*args, **kwargs):
            calls.append(1)
            raise HTTPError("https://example.com", 429, "Limited", {"Retry-After": "3600"}, None)

        result = c.collect(CONFIG, NOW, opener=request, sleeper=lambda _: self.fail("must not sleep"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), 1)

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
        self.assertIn("timespan=7d", requests[0][0])
        self.assertEqual(result["window_start"], "2026-10-10T12:00:00+00:00")
        self.assertEqual(result["window_end"], "2026-10-17T12:00:00+00:00")
        self.assertEqual(requests[0][1], CONFIG["collection"]["timeout_seconds"])

    def test_private_and_credential_urls_are_rejected(self):
        for url in ("http://127.0.0.1/x", "https://user:pass@example.com/x", "http://localhost/x"):
            with self.subTest(url=url):
                self.assertRaises(ValueError, c.canonical_url, url, CONFIG["collection"])


if __name__ == "__main__":
    unittest.main()
