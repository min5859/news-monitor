"""Probe deterministic news sources once. Prints headline metadata only; writes no files."""

import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from news_monitor.collect import (
    ROOT,
    CollectionError,
    canonical_url,
    fetch_json,
    load_config,
    normalized,
    plain_title,
    publisher_domain,
    read_bounded,
)

KST = ZoneInfo("Asia/Seoul")
TAVILY_URL = "https://api.tavily.com/search"
NEWS_NS = "{http://www.google.com/schemas/sitemap-news/0.9}"
QUERIES = ("조현준 효성", "조현준 회장", "Hyosung Cho Hyun-joon")
OUTLETS = ("www.newsis.com", "www.yna.co.kr", "www.etoday.co.kr", "biz.newdaily.co.kr", "www.newspim.com",
           "www.sedaily.com", "www.hankyung.com", "www.mk.co.kr", "www.fntimes.com", "www.dailyenews.co.kr",
           "www.topdaily.kr", "www.koreatimes.co.kr")


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


def candidate(title, url, published, config, first, last):
    """Apply the collector's headline rules. Returns (row, None) or (None, reason)."""
    try:
        title, url = plain_title(title), canonical_url(url, config["collection"])
    except (TypeError, ValueError):
        return None, "invalid_metadata"
    if not title or len(title) > config["collection"]["max_title_chars"]:
        return None, "invalid_metadata"
    if not any(normalized(alias) in normalized(title) for alias in config["target"]["person_aliases"]):
        return None, "not_target"
    try:
        day = kst_date(published)
    except (TypeError, ValueError, OverflowError):
        return None, "undated"
    if not first <= day <= last:
        return None, "out_of_window"
    return {"published_at": day.isoformat(), "publisher": publisher_domain(urlsplit(url).hostname),
            "title": title, "url": url}, None


def probe_tavily(config, key, first, last, queries, max_credits, opener=urlopen, sleeper=time.sleep):
    # One attempt per query keeps billed requests within max_credits.
    opts = {**config["collection"], "attempts": 1}
    result, rows = {"credits_used": 0, "queries": []}, {}
    for query in queries[:max_credits]:
        body = {"query": query, "topic": "news", "search_depth": "basic", "max_results": 20,
                "start_date": first.isoformat(), "end_date": last.isoformat(),
                "include_answer": False, "include_raw_content": False, "include_images": False}
        stats = {"query": query, "returned": 0, "kept": 0, "invalid_metadata": 0, "not_target": 0,
                 "undated": 0, "out_of_window": 0, "date_shapes": []}
        result["credits_used"] += 1
        try:
            items = fetch_json(TAVILY_URL, opts, opener, sleeper, data=json.dumps(body).encode(), key="results",
                               headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        except CollectionError as exc:
            result["queries"].append({**stats, "error": str(exc)})
            continue
        for item in items:
            item = item if isinstance(item, dict) else {}
            stats["returned"] += 1
            shape = re.sub(r"\d", "9", str(item.get("published_date")))[:40]
            if shape not in stats["date_shapes"] and len(stats["date_shapes"]) < 5:
                stats["date_shapes"].append(shape)
            # Only headline, link and date leave this loop; snippets and scores are dropped.
            row, reason = candidate(item.get("title"), item.get("url"), item.get("published_date"), config, first, last)
            stats["kept" if row else reason] += 1
            if row:
                rows.setdefault(row["url"], row)
        result["queries"].append(stats)
    result["rows"] = list(rows.values())
    return result


class FeedLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("type") == "application/rss+xml" and attrs.get("href"):
            self.links.append(attrs["href"])


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(node, name, namespace="", deep=False):
    for child in node.iter() if deep else node:
        if child is not node and local(child.tag) == name and child.tag.startswith(namespace):
            return (child.text or "").strip() or None
    return None


def parse_feed(raw):
    """Read a sitemap index, news sitemap or RSS 2.0 feed as (kind, entries, child sitemaps)."""
    root = ET.fromstring(raw)
    kind = local(root.tag)
    entries, children = [], []
    for node in root.iter():
        name = local(node.tag)
        if name == "sitemap" and kind == "sitemapindex":
            children.append(child_text(node, "loc"))
        elif name == "url" and kind == "urlset":
            entries.append((child_text(node, "title", NEWS_NS, deep=True), child_text(node, "loc"),
                            child_text(node, "publication_date", NEWS_NS, deep=True)))
        elif name == "item" and kind == "rss":
            entries.append((child_text(node, "title"), child_text(node, "link"), child_text(node, "pubDate")))
    return kind, entries, [child for child in children if child]


def probe_outlet(host, config, first, last, opener=urlopen, sleeper=time.sleep, max_documents=4):
    opts, agent = config["collection"], config["collection"]["user_agent"]
    base = "https://" + host + "/"
    result = {"host": host, "robots": "unavailable", "documents": [], "rows": []}

    def get(url):
        return read_bounded(Request(url, headers={"User-Agent": agent}), opts, opener)

    try:
        robots = RobotFileParser()
        robots.parse(get(base + "robots.txt").decode("utf-8", "replace").splitlines())
    except (CollectionError, OSError, HTTPException, ValueError):
        return result  # No readable robots.txt: do not guess what the publisher allows.
    result["robots"] = "allowed" if robots.can_fetch(agent, base) else "homepage_disallowed"
    queue = list(robots.site_maps() or [])
    if robots.can_fetch(agent, base):
        try:
            links = FeedLinks()
            links.feed(get(base).decode("utf-8", "replace"))
            queue += [urljoin(base, link) for link in links.links]
        except (CollectionError, OSError, HTTPException, ValueError):
            pass
    seen, fetched, rows = set(), 0, {}
    while queue and fetched < max_documents:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        document = {"url": url}
        result["documents"].append(document)
        try:
            same_publisher = publisher_domain(urlsplit(canonical_url(url, opts)).hostname) == publisher_domain(host)
        except ValueError:
            same_publisher = False
        if not same_publisher:
            document["skipped"] = "offsite"
            continue
        if not robots.can_fetch(agent, url):
            document["skipped"] = "robots"
            continue
        fetched += 1
        sleeper(1)
        try:
            kind, entries, children = parse_feed(get(url))
        except HTTPError as exc:
            document["error"] = f"http_{exc.code}"
            continue
        except (CollectionError, OSError, HTTPException, ET.ParseError) as exc:
            document["error"] = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
            continue
        # Recent/news child sitemaps first: indexes may list years of archives.
        queue += sorted(children, key=lambda child: not re.search(r"news|recent|today", child, re.IGNORECASE))
        days = []
        counts = {"kept": 0, "invalid_metadata": 0, "not_target": 0, "undated": 0, "out_of_window": 0}
        for title, link, published in entries:
            try:
                days.append(kst_date(published))
            except (TypeError, ValueError, OverflowError):
                pass
            row, reason = candidate(title, link, published, config, first, last)
            counts["kept" if row else reason] += 1
            if row:
                rows.setdefault(row["url"], row)
        document.update(kind=kind, entries=len(entries), titled=sum(entry[0] is not None for entry in entries),
                        dated=len(days), oldest=min(days).isoformat() if days else None,
                        newest=max(days).isoformat() if days else None, **counts)
    result["rows"] = list(rows.values())
    return result


def compare(rows, baseline, since=None):
    """Baseline rows (the current collector) found again by URL or by story on any subdomain."""
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--tavily-key-file", type=Path, help="probe Tavily with this private key file")
    parser.add_argument("--max-credits", type=int, default=len(QUERIES), choices=range(1, 11))
    parser.add_argument("--feeds", action="store_true", help="probe publisher sitemaps and RSS within robots.txt")
    parser.add_argument("--baseline", type=Path, help="read-only articles.json to compare against")
    args = parser.parse_args()
    config = load_config(args.config)
    now = datetime.now(UTC)
    last = now.astimezone(KST).date()
    first = last - timedelta(days=config["collection"]["lookback_days"] - 1)
    baseline = json.loads(args.baseline.read_text())["articles"] if args.baseline else None
    result = {"checked_at": now.isoformat(), "window": [first.isoformat(), last.isoformat()]}
    if args.tavily_key_file:
        key = json.loads(args.tavily_key_file.read_text()).get("api_key")
        if not isinstance(key, str) or not re.fullmatch(r"tvly-[A-Za-z0-9_-]+", key):
            parser.error("invalid Tavily key file")  # Never echo the value.
        result["tavily"] = probe_tavily(config, key, first, last, list(QUERIES), args.max_credits)
        if baseline is not None:
            result["tavily"]["compare"] = compare(result["tavily"]["rows"], baseline)
    if args.feeds:
        outlets = [probe_outlet(host, config, first, last) for host in OUTLETS]
        rows = [row for outlet in outlets for row in outlet["rows"]]
        oldest = min((doc["oldest"] for outlet in outlets for doc in outlet["documents"] if doc.get("oldest")), default=None)
        result["feeds"] = {"outlets": outlets, "rows": rows}
        if baseline is not None:
            result["feeds"]["compare"] = compare(rows, baseline, since=max(oldest or last.isoformat(), first.isoformat()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
