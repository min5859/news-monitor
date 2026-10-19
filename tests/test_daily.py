"""Daily collection regressions. Synthetic metadata only, no live requests."""

import copy
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from test_discovery import CONFIG, NOW, article, run_search

from news_monitor import analysis, discovery, report, site


class DailyTests(unittest.TestCase):
    def config(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"].update(search_groups=[['가상 인물 회사'], ['가상 인물 회장'], ['가상 영문명']],
                                 max_search_calls=12)
        return cfg

    def test_each_query_group_runs_with_a_shared_budget_and_union(self):
        seen = []

        def runner(prompt, model, **kwargs):
            payload = json.loads(prompt.split("INPUT_JSON:\n")[1])
            seen.append((payload["query_group"], kwargs["max_search_calls"]))
            row = article(url=f"https://publisher{len(seen)}.example/news/1")
            return json.dumps({"articles": [row]}), {"sources": {row["url"]: row["title"]}, "web_search_calls": 2}

        result = discovery.collect_web(self.config(), NOW, runner=runner)
        self.assertEqual(len(seen), 3)
        self.assertLessEqual(sum(limit for _, limit in seen), 12)
        self.assertEqual(result["statistics"]["selected"], 3)
        self.assertEqual(result["source_queries"], {"planned": 3, "attempted": 3, "succeeded": 3})
        self.assertEqual(result["web_search_calls"], 6)

    def test_one_failed_group_is_partial_and_does_not_leak_raw_errors(self):
        counter = 0

        def runner(*args, **kwargs):
            nonlocal counter
            counter += 1
            if counter == 2:
                raise analysis.AnalysisError("PRIVATE_ERROR")
            row = article()
            return json.dumps({"articles": [row]}), {"sources": {row["url"]: row["title"]}, "web_search_calls": 1}

        result = discovery.collect_web(self.config(), NOW, runner=runner)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["source_queries"]["succeeded"], 2)
        self.assertNotIn("PRIVATE_ERROR", json.dumps(result))

    def test_person_only_headline_is_a_candidate_not_automatic_positive(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["collection"]["require_organization_in_headline"] = False
        result = run_search([article(title="가상 테스트: 조현준 회장 행사 참석")], config=cfg)
        self.assertEqual(result["statistics"]["selected"], 1)
        self.assertEqual(len(report.prepare_collection(result, result, cfg, NOW)["articles"]), 1)

    def test_verified_articles_accumulate_without_changing_last_seen(self):
        previous = run_search([article(url="https://old.example/news/1")])
        fresh = run_search([article(url="https://new.example/news/2")])
        later = NOW + timedelta(days=1)
        fresh.update(checked_at=later.isoformat(), window_end=later.isoformat())
        for row in fresh["articles"]:
            row.update(indexed_at=later.isoformat(), collected_at=later.isoformat())
        merged = discovery.merge_previous(fresh, previous, CONFIG)
        self.assertEqual(len(merged["articles"]), 2)
        old = next(row for row in merged["articles"] if row["publisher"] == "old.example")
        self.assertEqual(old["collected_at"], previous["checked_at"])
        self.assertEqual(old["indexed_at"], previous["articles"][0]["indexed_at"])
        self.assertEqual(merged["statistics"]["retained"], 1)
        prepared = report.prepare_collection(merged, merged, CONFIG, later)
        self.assertEqual(len(prepared["articles"]), 2)

    def test_reconfirmed_article_preserves_first_seen_but_updates_last_seen(self):
        previous = run_search([article()])
        fresh = copy.deepcopy(previous)
        later = NOW + timedelta(hours=2)
        fresh.update(checked_at=later.isoformat(), window_end=later.isoformat())
        fresh["articles"][0].update(indexed_at=later.isoformat(), collected_at=later.isoformat())
        merged = discovery.merge_previous(fresh, previous, CONFIG)
        self.assertEqual(len(merged["articles"]), 1)
        row = merged["articles"][0]
        self.assertEqual(row["first_seen_at"], previous["articles"][0]["indexed_at"])
        self.assertEqual(row["indexed_at"], later.isoformat())
        self.assertEqual(merged["statistics"]["reconfirmed"], 1)

    def test_invalid_previous_and_failed_fresh_are_not_promoted(self):
        previous = run_search([article()])
        corrupt = copy.deepcopy(previous)
        corrupt["articles"][0]["url"] = "http://127.0.0.1/secret"
        fresh = run_search([])
        self.assertEqual(discovery.merge_previous(copy.deepcopy(fresh), corrupt, CONFIG)["articles"], [])
        failed = copy.deepcopy(fresh)
        failed["status"] = "failed"
        self.assertEqual(discovery.merge_previous(failed, previous, CONFIG)["status"], "failed")
        self.assertEqual(failed["articles"], [])

    def test_all_retained_does_not_claim_fresh_articles(self):
        previous = run_search([article()])
        merged = discovery.merge_previous(run_search([]), previous, CONFIG)
        self.assertEqual(merged["status"], "partial")
        self.assertEqual(merged["statistics"]["retained"], 1)
        self.assertIn("retained_articles_only", merged["warnings"])

    def test_previous_articles_outside_new_window_are_pruned(self):
        previous = run_search([article()])
        fresh = run_search([])
        fresh["window_start"] = (NOW + timedelta(days=1)).isoformat()
        fresh["window_end"] = fresh["checked_at"] = (NOW + timedelta(days=14)).isoformat()
        self.assertEqual(discovery.merge_previous(fresh, previous, CONFIG)["articles"], [])

    def test_future_provenance_is_rejected(self):
        value = run_search([article()])
        value["articles"][0]["collected_at"] = (NOW + timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):
            report.prepare_collection(value, value, CONFIG, NOW)

    def test_tracking_variants_of_same_publisher_title_and_day_count_once(self):
        previous = run_search([article(url="https://example.com/news/1?ref_sec=stocks")])
        fresh = run_search([article()])
        merged = discovery.merge_previous(fresh, previous, CONFIG)
        self.assertEqual(len(merged["articles"]), 1)
        self.assertEqual(merged["articles"][0]["url"], article()["url"])
        self.assertEqual(merged["statistics"]["reconfirmed"], 1)
        self.assertEqual(run_search([article(), article(url="https://example.com/news/1?ref_sec=stocks")])["statistics"]["selected"], 1)

    def test_updated_title_at_same_url_does_not_retain_two_versions(self):
        previous = run_search([article()])
        fresh = run_search([article(title="가상 테스트: 효성 조현준 회장 새 소식")])
        merged = discovery.merge_previous(fresh, previous, CONFIG)
        self.assertEqual(len(merged["articles"]), 1)
        self.assertEqual(merged["statistics"]["reconfirmed"], 1)
        report.prepare_collection(merged, merged, CONFIG, NOW)

    def test_deadline_skips_remaining_groups(self):
        cfg = self.config()
        cfg["collection"]["codex_timeout_seconds"] = 10
        with patch.object(discovery.time, "monotonic", side_effect=[0, 11]):
            result = discovery.collect_web(cfg, NOW, runner=lambda *args, **kwargs: self.fail("expired"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["source_queries"]["attempted"], 0)

    def test_microsecond_clock_and_no_today_articles(self):
        now = NOW.replace(microsecond=123456)
        row = article()
        value = discovery.collect_web(CONFIG, now, runner=lambda *args, **kwargs: (
            json.dumps({"articles": [row]}), {"sources": {row["url"]: row["title"]}, "web_search_calls": 1}))
        clean = report.prepare_collection(value, value, CONFIG, now)
        info = {"provider": "codex", "model": CONFIG["analysis"]["model"], "prompt_sha256": "0" * 64,
                "model_calls": 1, "cache_hits": 0, "failed_articles": 0}
        decision = {"relevance": "related", "sentiment": "neutral", "reason": "제목에서 사실 전달만 확인했습니다.", "evidence": "새 사업 추진"}
        public = report.make_report(clean, [{"id": clean["articles"][0]["id"], "decision": decision, "error": None}], info, CONFIG, now)
        html = site.render(public)
        self.assertIn("2026-10-18 발행 기사", html)
        self.assertIn("0건 · 분류 완료 0건", html)
        self.assertIn("비율 산출 불가", html)


if __name__ == "__main__":
    unittest.main()
