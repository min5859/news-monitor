"""Synthetic source documents only. No network, real credentials or writes outside temp dirs."""

import copy
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError

from test_discovery import CONFIG, NOW, article, run_search

from news_monitor import discovery, report, site, sources
from news_monitor.collect import ROOT, load_config, strip_site_suffix

TITLE = "가상 테스트: 조현준 회장 소식"
BASE = "https://www.paper.example/"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def opener_for(pages, log):
    def opener(request, timeout):
        log.append(request)
        body = pages.get(request.full_url)
        if isinstance(body, int):
            raise HTTPError(request.full_url, body, "synthetic", {}, None)
        if body is None:
            raise HTTPError(request.full_url, 404, "missing", {}, None)
        return Response(body.encode())
    return opener


def tavily(results):
    return json.dumps({"query": "q", "results": results, "response_time": 0.1})


SITEMAP = f"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/0.9">
  <url><loc>{BASE}view/1</loc>
    <image:image><image:loc>https://img.paper.example/1.jpg</image:loc><image:title>사진 설명</image:title></image:image>
    <news:news><news:publication_date>2026-10-17T09:00:00+09:00</news:publication_date>
      <news:title>{TITLE}</news:title></news:news></url>
  <url><loc>{BASE}view/2</loc>
    <news:news><news:publication_date>2026-10-17</news:publication_date><news:title>가상 테스트: 시장 소식</news:title></news:news></url>
</urlset>"""
RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>가상</title>
  <item><title>{TITLE} 속보</title><link>https://feed.paper.example/view/3</link>
    <pubDate>Sat, 18 Oct 2026 08:00:00 +0900</pubDate><description>MUST_NOT_KEEP</description></item>
</channel></rss>"""


class SourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.key = Path(temp.name) / "tavily.json"
        self.key.write_text(json.dumps({"api_key": "tvly-synthetic"}))
        self.config = copy.deepcopy(CONFIG)
        self.config["collection"].update(provider="tavily_feeds", tavily_key_file=str(self.key), tavily_queries=["a", "b"],
                                         tavily_max_results=20, excluded_publishers=["press.example"], feeds=[])

    def collect(self, pages, log=None, **options):
        self.config["collection"].update(options)
        log = [] if log is None else log
        return sources.collect_sources(self.config, NOW, opener=opener_for(pages, log), sleeper=lambda _: None)

    def test_tavily_keeps_only_headline_metadata_and_round_trips(self):
        results = [
            {"title": TITLE, "url": BASE + "a", "published_date": "2026-10-16T01:00:00Z",
             "content": "MUST_NOT_KEEP", "score": 0.9},
            {"title": TITLE + " 2", "url": "https://paper.example/b", "published_date": "Thu, 16 Oct 2026 10:00:00 GMT"},
            {"title": "가상 테스트: 다른 기업 소식", "url": "https://paper.example/c", "published_date": "2026-10-16"},
            {"title": TITLE + " 3", "url": "https://paper.example/d", "published_date": None},
            {"title": TITLE + " 4", "url": "https://paper.example/e", "published_date": "2025-01-01"},
            {"title": TITLE + " 보도자료", "url": "https://www.press.example/f", "published_date": "2026-10-16"},
        ]
        log = []
        result = self.collect({sources.TAVILY_URL: tavily(results)}, log)
        self.assertEqual(len(log), 2)
        self.assertEqual((log[0].get_method(), log[0].get_header("Authorization")), ("POST", "Bearer tvly-synthetic"))
        body = json.loads(log[0].data)
        self.assertEqual((body["topic"], body["max_results"], body["start_date"]), ("news", 20, "2026-10-05"))
        self.assertFalse(body["include_raw_content"] or body["include_answer"] or body["include_images"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual([row["published_at"] for row in result["articles"]], ["2026-10-16", "2026-10-16"])
        stats = result["statistics"]
        self.assertEqual((stats["not_target"], stats["undated"], stats["out_of_window"], stats["excluded_publisher"]),
                         (2, 2, 2, 2))
        self.assertEqual(result["publishers"], ["paper.example"])
        self.assertNotIn("MUST_NOT_KEEP", json.dumps(result))
        self.assertNotIn("tvly-synthetic", json.dumps(result))
        prepared = report.prepare_collection(result, result, self.config, NOW)
        decision = {"relevance": "related", "sentiment": "neutral", "reason": "가상 제목에서 사실 전달만 확인했습니다.",
                    "evidence": "소식"}
        info = {"provider": "codex", "model": self.config["analysis"]["model"], "prompt_sha256": "0" * 64,
                "model_calls": 2, "cache_hits": 0, "failed_articles": 0}
        value = report.make_report(prepared, [{"id": row["id"], "decision": decision, "error": None}
                                              for row in prepared["articles"]], info, self.config, NOW)
        html = site.render(value)
        self.assertEqual(value["date_basis"], "reported_publication_date")
        self.assertIn("발행 2026-10-16", html)
        self.assertIn("일부는 갱신일일 수 있습니다", html)

    def test_feeds_follow_each_hosts_robots_and_never_keep_other_text(self):
        pages = {BASE + "robots.txt": "User-agent: *\nDisallow: /private/\n", BASE + "news.xml": SITEMAP,
                 "https://feed.paper.example/robots.txt": "User-agent: *\nAllow: /\n",
                 "https://feed.paper.example/rss.xml": RSS}
        log = []
        result = self.collect(pages, log, tavily_queries=["a"],
                              feeds=[BASE + "news.xml", BASE + "private/news.xml", "https://feed.paper.example/rss.xml"])
        fetched = [request.full_url for request in log]
        self.assertNotIn(BASE + "private/news.xml", fetched)
        self.assertIn("https://feed.paper.example/robots.txt", fetched)
        self.assertEqual(sorted(row["url"] for row in result["articles"]), ["https://feed.paper.example/view/3", BASE + "view/1"])
        self.assertEqual({row["published_at"] for row in result["articles"]}, {"2026-10-17", "2026-10-18"})
        self.assertEqual({error["error"] for error in result["errors"]}, {"http_404", "robots_disallowed"})
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("MUST_NOT_KEEP", json.dumps(result))
        self.assertNotIn("사진 설명", json.dumps(result))

    def test_feed_hosts_that_label_kst_as_utc_keep_their_written_day(self):
        # Seen 10/27 17:20 KST: newdaily listed 17:17 KST as 17:17:19+00:00, so afternoon news moved a day later.
        pages = {BASE + "robots.txt": "User-agent: *\nAllow: /\n",
                 BASE + "news.xml": SITEMAP.replace("2026-10-17T09:00:00+09:00", "2026-10-17T17:17:19+00:00")}
        options = {"tavily_queries": ["a"], "feeds": [BASE + "news.xml"]}
        self.assertEqual([row["published_at"] for row in self.collect(pages, **options)["articles"]], ["2026-10-18"])
        result = self.collect(pages, kst_wall_clock_hosts=["www.paper.example"], **options)
        self.assertEqual([row["published_at"] for row in result["articles"]], ["2026-10-17"])
        self.config["collection"]["kst_wall_clock_hosts"] = ["not a host"]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(self.config))
            with self.assertRaisesRegex(ValueError, "kst_wall_clock_hosts"):
                load_config(path)

    def test_publisher_feed_date_replaces_search_date_for_the_same_url(self):
        pages = {sources.TAVILY_URL: tavily([{"title": TITLE, "url": BASE + "view/1", "published_date": "2026-10-18"}]),
                 BASE + "robots.txt": "User-agent: *\nAllow: /\n", BASE + "news.xml": SITEMAP}
        result = self.collect(pages, tavily_queries=["a"], feeds=[BASE + "news.xml"])
        self.assertEqual([(row["url"], row["published_at"]) for row in result["articles"]], [(BASE + "view/1", "2026-10-17")])
        self.assertEqual(result["statistics"]["duplicates"], 1)

    def test_quota_missing_key_and_unreadable_robots_are_fixed_codes(self):
        result = self.collect({sources.TAVILY_URL: 432}, tavily_queries=["a", "b"])
        self.assertEqual([error["error"] for error in result["errors"]], ["http_432", "http_432", "no_source_succeeded"])
        self.assertEqual(result["status"], "failed")
        self.key.unlink()
        log = []
        result = self.collect({}, log, feeds=[BASE + "news.xml"])
        self.assertEqual([request.full_url for request in log], [BASE + "robots.txt"])
        self.assertEqual({error["error"] for error in result["errors"]},
                         {"tavily_key_unavailable", "robots_unavailable", "no_source_succeeded"})
        self.assertEqual((result["status"], result["articles"]), ("failed", []))

    def test_verified_rows_from_the_previous_source_are_retained_not_relabeled(self):
        previous = run_search([article(url="https://old.example/news/1")])
        fresh = self.collect({sources.TAVILY_URL: tavily([{"title": TITLE, "url": BASE + "a", "published_date": "2026-10-17"}])},
                             tavily_queries=["a"])
        merged = discovery.merge_previous(fresh, previous, self.config)
        self.assertEqual(sorted(row["source"] for row in merged["articles"]), ["codex_web_search", "tavily_feeds"])
        old = next(row for row in merged["articles"] if row["source"] == "codex_web_search")
        self.assertEqual(old["first_seen_at"], previous["articles"][0]["first_seen_at"])
        self.assertEqual(merged["statistics"]["retained"], 1)
        self.assertEqual(len(report.prepare_collection(merged, merged, self.config, NOW + timedelta(hours=1))["articles"]), 2)

    def test_same_story_with_a_different_reported_date_is_not_counted_twice(self):
        previous = run_search([article(url="https://mobile.old.example/view/1", day="2026-10-16")])
        fresh = self.collect({sources.TAVILY_URL: tavily([{"title": article()["title"], "url": "https://www.old.example/view/1",
                                                           "published_date": "2026-10-15"}])}, tavily_queries=["a"])
        merged = discovery.merge_previous(fresh, previous, self.config)
        self.assertEqual(len(merged["articles"]), 1)
        self.assertEqual(merged["articles"][0]["first_seen_at"], previous["articles"][0]["first_seen_at"])
        self.assertEqual(merged["statistics"]["reconfirmed"], 1)

    def test_search_page_title_suffixes_are_dropped_but_headline_text_is_kept(self):
        base = article()["title"]
        for raw, clean in ((base + " < 에너지·화학 < 산업 < 기사본문 - 가상비즈", base), (base + " - 가상일보", base),
                           (base + " - The Virtual Times", base), (base + " | 가상뉴스", base), (base + " - 뉴스1", base),
                           (base + " - The Virtual Business Daily", base), (base + " | Virtual Goods Intelligence", base),
                           (base + " - 3분기 1조 돌파", None), (base + " - 매출 감소에도 투자 늘린 이유는", None),
                           (base + " - 현장 경영", None), (base + " - expands in Vietnam", None), ("짧은 제목 - 가상일보", None)):
            with self.subTest(raw=raw):
                self.assertEqual(strip_site_suffix(raw), clean or raw)

    def test_saved_and_new_copies_with_site_suffixes_are_one_clean_story(self):
        title = article()["title"]
        previous = run_search([article(title=title + " - 가상일보", url="https://www.old.example/amp/view/1")])
        fresh = self.collect({sources.TAVILY_URL: tavily([{"title": title + " < 산업 < 기사본문 - 가상일보",
                                                           "url": "https://www.old.example/view/1", "published_date": "2026-10-16"}])},
                             tavily_queries=["a"])
        self.assertEqual([row["title"] for row in fresh["articles"]], [title])
        merged = discovery.merge_previous(fresh, previous, self.config)
        self.assertEqual([row["title"] for row in merged["articles"]], [title])
        self.assertEqual(merged["articles"][0]["first_seen_at"], previous["articles"][0]["first_seen_at"])
        retained = discovery.merge_previous(self.collect({sources.TAVILY_URL: tavily([])}, tavily_queries=["a"]), previous, self.config)
        self.assertEqual([row["title"] for row in retained["articles"]], [title])

    def test_source_settings_reject_unsafe_values(self):
        self.assertEqual(load_config(ROOT / "config.json")["collection"]["provider"], "tavily_feeds")
        for key, value in (("feeds", ["http://www.paper.example/news.xml"]), ("feeds", ["https://www.paper.example/a?b=1&a=2"]),
                           ("feeds", []), ("tavily_key_file", "tavily.json"), ("tavily_max_results", 21),
                           ("tavily_queries", ["q"] * 11)):
            cfg = copy.deepcopy(self.config)
            cfg["collection"].update(feeds=[BASE + "news.xml"])
            cfg["collection"][key] = value
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "config.json"
                path.write_text(json.dumps(cfg))
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    load_config(path)


class CompareTests(unittest.TestCase):
    def test_url_or_same_story_on_another_subdomain_counts_as_found(self):
        baseline = [{"title": TITLE, "url": BASE + "view/1", "published_at": "2026-10-17"},
                    {"title": TITLE + " 2", "url": BASE + "view/2", "published_at": "2026-10-16"},
                    {"title": TITLE + " 3", "url": BASE + "view/3", "published_at": "2026-10-01"}]
        rows = [{"title": TITLE, "url": "https://mobile.paper.example/view/1", "publisher": "paper.example",
                 "published_at": "2026-10-17"},
                {"title": TITLE + " 2", "url": BASE + "view/2", "publisher": "paper.example", "published_at": "2026-10-15"},
                {"title": "가상 테스트: 조현준 새 소식", "url": "https://new.example/1", "publisher": "new.example",
                 "published_at": "2026-10-18"}]
        self.assertEqual(sources.compare(rows, baseline, since="2026-10-10"),
                         {"baseline": 2, "found": 2, "same_date": 1, "candidate_only": 1})


if __name__ == "__main__":
    unittest.main()
