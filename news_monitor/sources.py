"""Tavily news search plus robots-permitted publisher sitemaps/RSS. Headline metadata only."""

import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from news_monitor.collect import (
    ROOT,
    CollectionError,
    canonical_url,
    fetch_json,
    headline_names_target,
    load_config,
    normalized,
    parse_article,
    publisher_domain,
    read_bounded,
)
from news_monitor.discovery import select_stories

KST = ZoneInfo("Asia/Seoul")
TAVILY_URL = "https://api.tavily.com/search"
NEWS_NS = "{http://www.google.com/schemas/sitemap-news/0.9}"
REJECTIONS = ("invalid_metadata", "not_target", "excluded_publisher", "undated", "out_of_window")


def kst_date(value):
    """Date-only values are the publisher's calendar day; timestamps need an offset."""
    value = value.strip() if isinstance(value, str) else ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        raise ValueError("date without timezone")
    return parsed.astimezone(KST).date()


def candidate(item, config, now, first, last):
    """Apply the collector's headline rules to {title, url, published}. Returns (article, rejection)."""
    opts = config["collection"]
    try:
        url = canonical_url(item["url"], opts)
        article = parse_article({"title": item["title"], "url": url, "domain": urlsplit(url).hostname,
                                 "seendate": now.strftime("%Y%m%dT%H%M%SZ"), "language": "unknown"}, config)
    except (KeyError, TypeError, ValueError):
        return None, "invalid_metadata"
    if not headline_names_target(article["title"], config):
        return None, "not_target"
    if article["publisher"] in opts["excluded_publishers"]:
        return None, "excluded_publisher"
    try:
        day = kst_date(item["published"])
    except (TypeError, ValueError, OverflowError):
        return None, "undated"
    if not first <= day <= last:
        return None, "out_of_window"
    stamp = now.isoformat()
    article.update(language="ko" if re.search("[가-힣]", article["title"]) else "unknown", published_at=day.isoformat(),
                   indexed_at=stamp, collected_at=stamp, first_seen_at=stamp)
    return article, None


def search_tavily(config, first, last, opener, sleeper):
    """Only title, URL and date are read; snippets, scores and answers are never kept."""
    opts = config["collection"]
    found = {"items": [], "errors": [], "attempted": 0, "succeeded": 0, "limit_reached": False}
    try:
        key = json.loads(Path(opts["tavily_key_file"]).read_text())["api_key"]
        if not isinstance(key, str) or not re.fullmatch(r"tvly-[A-Za-z0-9_-]+", key):
            raise ValueError("invalid key")
    except (OSError, ValueError, KeyError, TypeError):
        found["errors"].append({"error": "tavily_key_unavailable", "source": "tavily"})
        return found
    for index, query in enumerate(opts["tavily_queries"]):
        if index:
            sleeper(1)
        body = {"query": query, "topic": "news", "search_depth": "basic", "max_results": opts["tavily_max_results"],
                "start_date": first.isoformat(), "end_date": last.isoformat(),
                "include_answer": False, "include_raw_content": False, "include_images": False}
        found["attempted"] += 1
        try:
            results = fetch_json(TAVILY_URL, opts, opener, sleeper, data=json.dumps(body).encode(), key="results",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        except CollectionError as exc:
            found["errors"].append({"error": str(exc), "source": "tavily", "query": index + 1})
            if exc.retry_after_seconds is not None:
                break  # Rate-limited or unavailable: later queries would fail the same way.
            continue
        found["succeeded"] += 1
        found["limit_reached"] |= len(results) >= opts["tavily_max_results"]
        found["items"] += [{"title": row.get("title"), "url": row.get("url"), "published": row.get("published_date")}
                           for row in results if isinstance(row, dict)]
    return found


def child_text(node, name, namespace="", deep=False):
    for child in node.iter() if deep else node:
        if child is not node and child.tag.rsplit("}", 1)[-1] == name and child.tag.startswith(namespace):
            return (child.text or "").strip() or None
    return None


def parse_feed(raw):
    """Entries of a Google News sitemap or an RSS 2.0 feed as {title, url, published}."""
    root = ET.fromstring(raw)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind == "urlset":
        return [{"title": child_text(node, "title", NEWS_NS, deep=True), "url": child_text(node, "loc"),
                 "published": child_text(node, "publication_date", NEWS_NS, deep=True)}
                for node in root if node.tag.endswith("url")]
    if kind == "rss":
        return [{"title": child_text(node, "title"), "url": child_text(node, "link"), "published": child_text(node, "pubDate")}
                for node in root.iter("item")]
    raise ValueError("unsupported_feed")


def read_feeds(config, opener, sleeper):
    """Each document is fetched only if its own host's robots.txt allows it; articles are never opened."""
    opts, agent = config["collection"], config["collection"]["user_agent"]
    found = {"items": [], "errors": [], "attempted": 0, "succeeded": 0}
    robots = {}

    def get(url):
        return read_bounded(Request(url, headers={"User-Agent": agent}), opts, opener)

    for index, url in enumerate(opts["feeds"]):
        host = urlsplit(url).hostname
        found["attempted"] += 1
        if host not in robots:
            try:
                robots[host] = RobotFileParser()
                robots[host].parse(get(f"https://{host}/robots.txt").decode("utf-8", "replace").splitlines())
            except (CollectionError, OSError, HTTPException, ValueError):
                robots[host] = None  # Unreadable robots.txt: do not guess what the publisher allows.
        try:
            if robots[host] is None:
                raise CollectionError("robots_unavailable")
            if not robots[host].can_fetch(agent, url):
                raise CollectionError("robots_disallowed")
            sleeper(1)
            found["items"] += parse_feed(get(url))
            found["succeeded"] += 1
        except HTTPError as exc:
            found["errors"].append({"error": f"http_{exc.code}", "source": "feed", "feed": index + 1})
        except CollectionError as exc:
            found["errors"].append({"error": str(exc), "source": "feed", "feed": index + 1})
        except (ET.ParseError, ValueError):
            found["errors"].append({"error": "invalid_feed", "source": "feed", "feed": index + 1})
        except (OSError, HTTPException) as exc:
            found["errors"].append({"error": "network_" + type(exc).__name__, "source": "feed", "feed": index + 1})
    return found


def collect_sources(config, now, opener=urlopen, sleeper=time.sleep):
    opts = config["collection"]
    now = now.astimezone(UTC)
    last = now.astimezone(KST).date()
    first = last - timedelta(days=opts["lookback_days"] - 1)
    start = datetime.combine(first, day_time.min, KST).astimezone(UTC)
    stats = dict.fromkeys(("fetched", *REJECTIONS, "duplicates", "limit_excluded", "selected"), 0)
    searched, feeds = search_tavily(config, first, last, opener, sleeper), read_feeds(config, opener, sleeper)
    rows = {}
    # Feeds go last so a publisher-provided date replaces a search index date for the same URL.
    for item in searched["items"] + feeds["items"]:
        stats["fetched"] += 1
        article, rejection = candidate(item, config, now, first, last)
        if rejection:
            stats[rejection] += 1
            continue
        if article["url"] in rows:
            stats["duplicates"] += 1
        rows[article["url"]] = article
    selected = select_stories(rows.values(), stats, opts["max_articles"])
    succeeded = searched["succeeded"] + feeds["succeeded"]
    warnings = ["upstream_limit_reached"] if searched["limit_reached"] else []
    publishers = sorted({row["publisher"] for row in selected})
    if len(publishers) < opts["publisher_goal"]:
        warnings.append("publisher_goal_not_met")
    errors = searched["errors"] + feeds["errors"]
    if not succeeded:
        status = "failed"
        errors.append({"error": "no_source_succeeded"})
    elif errors or stats["limit_excluded"] or "upstream_limit_reached" in warnings:
        status = "partial"
    else:
        status = "ok" if selected else "empty"
    stats.update(newly_found=len(selected), reconfirmed=0, retained=0)
    return {
        "schema_version": 1, "status": status, "target": config["target"]["name"],
        "checked_at": now.isoformat(), "window_start": start.isoformat(), "window_end": now.isoformat(),
        "date_basis": "reported_publication_date", "coverage": "search_sample_not_exhaustive",
        "selection": "latest_published_target_in_headline", "statistics": stats, "publishers": publishers,
        "source_queries": {"planned": len(opts["tavily_queries"]) + len(opts["feeds"]),
                           "attempted": searched["attempted"] + feeds["attempted"], "succeeded": succeeded},
        "source_attribution": {"name": opts["attribution_name"], "url": opts["attribution_url"]},
        "retry_after_seconds": None, "warnings": sorted(set(warnings)), "errors": errors,
        "articles": selected,
    }


def compare(rows, baseline, since=None):
    """Baseline rows (an earlier collection) found again by URL or by story on any subdomain."""
    def story(row):
        return publisher_domain(urlsplit(row["url"]).hostname), normalized(row["title"])

    baseline = [row for row in baseline if since is None or row["published_at"] >= since]
    by_url, by_story = {row["url"]: row for row in rows}, {story(row): row for row in rows}
    found = same_date = 0
    matched = set()
    for row in baseline:
        hit = by_url.get(row["url"]) or by_story.get(story(row))
        if hit:
            found += 1
            same_date += hit["published_at"] == row["published_at"]
            matched.add(hit["url"])
    return {"baseline": len(baseline), "found": found, "same_date": same_date,
            "candidate_only": sum(row["url"] not in matched for row in rows)}


def main():
    """Probe: collect once and print headline metadata. Writes no data, status or output files."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--baseline", type=Path, help="read-only articles.json to compare against")
    args = parser.parse_args()
    config = load_config(args.config)
    if config["collection"]["provider"] != "tavily_feeds":
        parser.error("config provider must be tavily_feeds")
    result = collect_sources(config, datetime.now(UTC))
    summary = {key: result[key] for key in ("checked_at", "status", "statistics", "source_queries", "warnings",
                                            "errors", "publishers")}
    summary["articles"] = [{key: row[key] for key in ("published_at", "publisher", "title", "url")}
                           for row in result["articles"]]
    if args.baseline:
        summary["compare"] = compare(result["articles"], json.loads(args.baseline.read_text())["articles"])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
