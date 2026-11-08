"""Synthetic search events only. Never contact a model or web endpoint."""

import atexit
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from news_monitor import analysis, codex, report, site, sources
from news_monitor.collect import ROOT

KEY_DIR = tempfile.TemporaryDirectory()
atexit.register(KEY_DIR.cleanup)
KEY_FILE = Path(KEY_DIR.name) / "tavily.json"
KEY_FILE.write_text(json.dumps({"api_key": "tvly-synthetic"}))

CONFIG = json.loads((ROOT / "config.json").read_text())
CONFIG["collection"].update(tavily_key_file=str(KEY_FILE), tavily_queries=["가상 검색"], feeds=[], lookback_days=14)
CONFIG["briefing"]["enabled"] = False
NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def article(title="가상 테스트: 효성 조현준 회장, 새 사업 추진", url="https://example.com/news/1", day="2026-10-16"):
    return {"title": title, "url": url, "published_date": day, "language": "ko"}


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def tavily_opener(rows):
    """An opener whose only answer is a synthetic Tavily result list."""
    results = [{"title": row["title"], "url": row["url"], "published_date": row["published_date"]} for row in rows]
    body = json.dumps({"query": "q", "results": results}).encode()
    return lambda request, timeout: Response(body)


def run_search(rows, config=None, now=NOW):
    """One tavily_feeds collection whose only source is a synthetic Tavily answer."""
    return sources.collect_sources(config or CONFIG, now, opener=tavily_opener(rows), sleeper=lambda _: None)


class DiscoveryTests(unittest.TestCase):
    def test_publication_date_and_actual_collection_time_are_distinct(self):
        result = run_search([article()])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["date_basis"], "reported_publication_date")
        self.assertEqual(result["articles"][0]["published_at"], "2026-10-16")
        self.assertEqual(result["articles"][0]["collected_at"], NOW.isoformat())
        self.assertEqual(result["articles"][0]["source"], "tavily_feeds")
        prepared = report.prepare_collection(result, result, CONFIG, NOW)
        self.assertEqual(prepared["date_basis"], "reported_publication_date")

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

    def test_final_result_at_exact_budget_is_not_discarded(self):
        text, trace = self.execute([{"type": "web_search"}] * 5, search=True, max_search_calls=5)
        self.assertEqual(text, '{"ok":true}')
        self.assertEqual(trace["web_search_calls"], 5)


if __name__ == "__main__":
    unittest.main()
