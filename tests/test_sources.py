"""Synthetic source documents only. No network, credentials or file writes."""

import io
import json
import unittest
from datetime import date
from urllib.error import HTTPError

from test_discovery import CONFIG

from news_monitor import sources

FIRST, LAST = date(2026, 10, 5), date(2026, 10, 18)
TITLE = "가상 테스트: 조현준 회장 소식"


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


class TavilyTests(unittest.TestCase):
    def test_request_is_bounded_and_keeps_only_headline_metadata(self):
        results = [
            {"title": TITLE, "url": "https://www.paper.example/a", "published_date": "2026-10-16T01:00:00Z",
             "content": "MUST_NOT_KEEP", "score": 0.9},
            {"title": TITLE + " 2", "url": "https://paper.example/b", "published_date": "Thu, 16 Oct 2026 10:00:00 +0900"},
            {"title": "가상 테스트: 다른 기업 소식", "url": "https://paper.example/c", "published_date": "2026-10-16"},
            {"title": TITLE + " 3", "url": "https://paper.example/d", "published_date": None},
            {"title": TITLE + " 4", "url": "https://paper.example/e", "published_date": "2025-01-01"},
        ]
        log = []
        result = sources.probe_tavily(CONFIG, "tvly-test", FIRST, LAST, ["a", "b", "c"], 2,
                                      opener=opener_for({sources.TAVILY_URL: tavily(results)}, log))
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0].get_method(), "POST")
        self.assertEqual(log[0].get_header("Authorization"), "Bearer tvly-test")
        body = json.loads(log[0].data)
        self.assertEqual((body["topic"], body["max_results"], body["start_date"]), ("news", 20, "2026-10-05"))
        self.assertFalse(body["include_raw_content"] or body["include_answer"] or body["include_images"])
        self.assertEqual(result["credits_used"], 2)
        self.assertEqual([row["published_at"] for row in result["rows"]], ["2026-10-16", "2026-10-16"])
        self.assertEqual(result["rows"][0]["publisher"], "paper.example")
        counts = result["queries"][0]
        self.assertEqual((counts["not_target"], counts["undated"], counts["out_of_window"]), (1, 1, 1))
        self.assertNotIn("MUST_NOT_KEEP", json.dumps(result))
        self.assertNotIn("tvly-test", json.dumps(result))

    def test_quota_error_is_a_fixed_code(self):
        log = []
        result = sources.probe_tavily(CONFIG, "tvly-test", FIRST, LAST, ["a"], 4,
                                      opener=opener_for({sources.TAVILY_URL: 432}, log))
        self.assertEqual(len(log), 1)
        self.assertEqual(result["queries"][0]["error"], "http_432")
        self.assertEqual(result["credits_used"], 1)


SITEMAP_INDEX = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.paper.example/archive.xml</loc></sitemap>
  <sitemap><loc>https://www.paper.example/news-recent.xml</loc></sitemap>
</sitemapindex>"""
NEWS_SITEMAP = f"""<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/0.9">
  <url><loc>https://www.paper.example/view/1</loc>
    <image:image><image:loc>https://img.paper.example/1.jpg</image:loc><image:title>사진</image:title></image:image>
    <news:news><news:publication_date>2026-10-17T09:00:00+09:00</news:publication_date>
      <news:title>{TITLE}</news:title></news:news></url>
  <url><loc>https://www.paper.example/view/2</loc>
    <news:news><news:publication_date>2026-10-17</news:publication_date><news:title>가상 테스트: 시장 소식</news:title></news:news></url>
</urlset>"""
RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>가상</title>
  <item><title>{TITLE} 속보</title><link>https://www.paper.example/view/3</link>
    <pubDate>Sat, 18 Oct 2026 08:00:00 +0900</pubDate><description>MUST_NOT_KEEP</description></item>
</channel></rss>"""


class FeedTests(unittest.TestCase):
    def test_feeds_follow_robots_and_stay_on_the_publisher(self):
        base = "https://www.paper.example/"
        pages = {
            base + "robots.txt": "User-agent: *\nDisallow: /private/\nSitemap: https://www.paper.example/index.xml\n"
                                 "Sitemap: https://www.paper.example/private/news.xml\nSitemap: https://other.example/news.xml\n",
            base: '<html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml"></head></html>',
            base + "index.xml": SITEMAP_INDEX, base + "news-recent.xml": NEWS_SITEMAP, base + "rss.xml": RSS,
        }
        log = []
        result = sources.probe_outlet("www.paper.example", CONFIG, FIRST, LAST, opener_for(pages, log), sleeper=lambda _: None)
        fetched = [request.full_url for request in log]
        self.assertNotIn(base + "private/news.xml", fetched)
        self.assertNotIn("https://other.example/news.xml", fetched)
        self.assertIn(base + "news-recent.xml", fetched)
        skipped = {doc["url"]: doc.get("skipped") for doc in result["documents"]}
        self.assertEqual(skipped[base + "private/news.xml"], "robots")
        self.assertEqual(skipped["https://other.example/news.xml"], "offsite")
        self.assertEqual(sorted(row["url"] for row in result["rows"]), [base + "view/1", base + "view/3"])
        self.assertEqual({row["published_at"] for row in result["rows"]}, {"2026-10-17", "2026-10-18"})
        self.assertNotIn("MUST_NOT_KEEP", json.dumps(result))
        self.assertNotIn("사진", json.dumps(result))

    def test_disallowed_outlet_fetches_only_robots(self):
        base = "https://www.paper.example/"
        log = []
        result = sources.probe_outlet("www.paper.example", CONFIG, FIRST, LAST, opener_for(
            {base + "robots.txt": "User-agent: *\nDisallow: /\nSitemap: https://www.paper.example/index.xml\n"}, log),
            sleeper=lambda _: None)
        self.assertEqual([request.full_url for request in log], [base + "robots.txt"])
        self.assertEqual(result["rows"], [])

    def test_missing_robots_skips_the_outlet(self):
        log = []
        result = sources.probe_outlet("www.paper.example", CONFIG, FIRST, LAST, opener_for({}, log), sleeper=lambda _: None)
        self.assertEqual(result["robots"], "unavailable")
        self.assertEqual(len(log), 1)


class CompareTests(unittest.TestCase):
    def test_url_or_same_story_on_another_subdomain_counts_as_found(self):
        baseline = [{"title": TITLE, "url": "https://www.paper.example/view/1", "published_at": "2026-10-17"},
                    {"title": TITLE + " 2", "url": "https://www.paper.example/view/2", "published_at": "2026-10-16"},
                    {"title": TITLE + " 3", "url": "https://www.paper.example/view/3", "published_at": "2026-10-01"}]
        rows = [{"title": TITLE, "url": "https://mobile.paper.example/view/1", "publisher": "paper.example",
                 "published_at": "2026-10-17"},
                {"title": TITLE + " 2", "url": "https://www.paper.example/view/2", "publisher": "paper.example",
                 "published_at": "2026-10-15"},
                {"title": "가상 테스트: 조현준 새 소식", "url": "https://new.example/1", "publisher": "new.example",
                 "published_at": "2026-10-18"}]
        self.assertEqual(sources.compare(rows, baseline, since="2026-10-10"),
                         {"baseline": 2, "found": 2, "same_date": 1, "candidate_only": 1})


if __name__ == "__main__":
    unittest.main()
