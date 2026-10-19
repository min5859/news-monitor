"""Synthetic search events only. Never contact a model or web endpoint."""

import copy
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from news_monitor import analysis, codex, discovery, report, site
from news_monitor.collect import ROOT

CONFIG = json.loads((ROOT / "config.json").read_text())
CONFIG["collection"]["search_groups"] = [["가상 검색"]]
NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def article(title="가상 테스트: 효성 조현준 회장, 새 사업 추진", url="https://example.com/news/1", day="2026-10-16"):
    return {"title": title, "url": url, "published_date": day, "language": "ko"}


def run_search(rows, sources=None, calls=1, config=None):
    sources = sources if sources is not None else {row["url"]: row["title"] for row in rows}
    if not rows and not sources:
        sources = {"https://example.com/search": "검색 페이지"}
    return discovery.collect_web(config or CONFIG, NOW,
                                 runner=lambda *args, **kwargs: (json.dumps({"articles": rows}),
                                                                {"sources": sources, "web_search_calls": calls}))


class DiscoveryTests(unittest.TestCase):
    def test_publisher_date_and_actual_collection_time_are_distinct(self):
        result = run_search([article()])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["date_basis"], "publisher_date")
        self.assertEqual(result["articles"][0]["published_at"], "2026-10-16")
        self.assertEqual(result["articles"][0]["source"], "codex_web_search")
        prepared = report.prepare_collection(result, result, CONFIG, NOW)
        self.assertEqual(prepared["date_basis"], "publisher_date")

    def test_old_future_and_untargeted_results_are_not_selected(self):
        rows = [article(), article(day="2025-10-16"), article(day="2026-10-19"), article(title="다른 기업 소식")]
        result = run_search(rows)
        self.assertEqual(result["statistics"]["selected"], 1)
        self.assertEqual(result["statistics"]["out_of_window"], 2)
        self.assertEqual(result["statistics"]["not_target"], 1)

    def test_unseen_url_and_absent_search_events_are_rejected(self):
        result = run_search([article()], sources={"https://example.com/other": "다른 기사"})
        self.assertEqual(result["statistics"]["selected"], 0)
        self.assertEqual(result["statistics"]["unverified_url"], 1)
        self.assertNotEqual(result["status"], "ok")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(run_search([article()], calls=0)["status"], "failed")

    def test_invalid_dates_private_urls_and_extra_fields_fail(self):
        for row in ({**article(), "published_date": "unknown"}, {**article(), "url": "http://127.0.0.1/a"},
                    {**article(), "body": "MUST_NOT_STORE"}):
            with self.subTest(row=row):
                result = run_search([row])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["articles"], [])

    def test_dedup_and_latest_publication_selection(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["max_articles"] = 1
        result = run_search([article(day="2026-10-10"), article(),
                             article(url="https://other.example/news/2", day="2026-10-17")], config=cfg)
        self.assertEqual(result["statistics"]["duplicates"], 1)
        self.assertEqual(result["articles"][0]["published_at"], "2026-10-17")

    def test_valid_no_articles_is_empty_and_failure_is_distinct(self):
        self.assertEqual(run_search([])["status"], "empty")
        result = discovery.collect_web(CONFIG, NOW, runner=lambda *args, **kwargs: ('{"articles":[]}',
                                                                                 {"sources": {}, "web_search_calls": 1}))
        self.assertEqual(result["status"], "empty")
        result = discovery.collect_web(CONFIG, NOW, runner=lambda *args, **kwargs: ("invalid", {}))
        self.assertEqual(result["status"], "failed")

    def test_site_uses_publication_not_collection_date_for_filter(self):
        result = run_search([article()])
        prepared = report.prepare_collection(result, result, CONFIG, NOW)
        decision = {"relevance": "related", "sentiment": "neutral", "reason": "가상 제목에서 사실 전달만 확인했습니다.",
                    "evidence": "새 사업 추진"}
        info = {"provider": "codex", "model": CONFIG["analysis"]["model"], "prompt_sha256": "0" * 64,
                "model_calls": 1, "cache_hits": 0, "failed_articles": 0}
        value = report.make_report(prepared, [{"id": prepared["articles"][0]["id"], "decision": decision, "error": None}], info, CONFIG, NOW)
        html = site.render(value)
        self.assertIn('data-day="2026-10-16"', html)
        self.assertIn("발행 2026-10-16", html)
        self.assertNotIn("GDELT 관측", html)


class CodexTests(unittest.TestCase):
    def execute(self, items, **kwargs):
        events = [{"type": "item.started", "item": item} for item in items]
        events += [{"type": "item.completed", "item": {"type": "agent_message", "text": '{"ok":true}'}},
                   {"type": "turn.completed"}]
        raw = "\n".join(json.dumps(event) for event in events) + "\n"

        def capture(command, prompt, workspace, opts, observer):
            self.assertIn("--ignore-user-config", command)
            self.assertIn("read-only", command)
            self.assertIn("shell_tool", command)
            self.assertNotIn("danger-full-access", command)
            self.assertEqual(list(workspace.iterdir()), [])
            observer(raw.encode())
            return raw

        with patch.object(analysis, "capture_model", side_effect=capture):
            return codex.execute("data only", "test-model", **kwargs)

    def test_no_tools_for_classification(self):
        text, trace = self.execute([])
        self.assertEqual(text, '{"ok":true}')
        self.assertEqual(trace["web_search_calls"], 0)

    def test_unexpected_tools_and_search_fail_closed(self):
        for kind in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
            with self.subTest(kind=kind), self.assertRaises(analysis.AnalysisError):
                self.execute([{"type": kind}])

    def test_web_search_has_a_hard_event_budget(self):
        with self.assertRaisesRegex(analysis.AnalysisError, "budget"):
            self.execute([{"type": "web_search"}, {"type": "web_search"}], search=True, max_search_calls=1)


if __name__ == "__main__":
    unittest.main()
