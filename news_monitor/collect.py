"""Bounded GDELT metadata collection; never fetch article bodies or images."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


class CollectionError(Exception):
    pass


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_title(value):
    if not isinstance(value, str):
        raise TypeError("invalid title")
    parser = PlainText()
    parser.feed(value)
    return " ".join(unescape("".join(parser.parts)).split())


def normalized(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def load_config(path):
    config = json.loads(Path(path).read_text())
    opts = config["collection"]
    if opts["provider"] != "gdelt_doc":
        raise ValueError("unsupported provider")
    if not opts["endpoint"].startswith("https://"):
        raise ValueError("collection endpoint must use HTTPS")
    for name, lower, upper in (("lookback_days", 1, 90), ("max_results_per_query", 1, 250),
                                ("max_articles", 1, 250), ("attempts", 1, 3)):
        value = opts[name]
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"invalid {name}")
    for name in ("timeout_seconds", "backoff_seconds", "max_retry_wait_seconds",
                 "query_interval_seconds", "max_response_bytes", "max_title_chars"):
        if type(opts[name]) not in (int, float) or opts[name] <= 0:
            raise ValueError(f"invalid {name}")
    if opts["query_interval_seconds"] < 5:
        raise ValueError("GDELT query interval must be at least five seconds")
    for name in ("max_response_bytes", "max_title_chars"):
        if type(opts[name]) is not int:
            raise ValueError(f"{name} must be an integer")
    for values in (opts["queries"], config["target"]["person_aliases"],
                   config["target"]["organization_aliases"]):
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("queries and aliases must be nonempty string lists")
    return config


def canonical_url(value, opts):
    if not isinstance(value, str) or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("invalid article URL")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("unsafe article URL")
    host = parts.hostname.lower()
    if "." not in host or host.endswith((".local", ".internal")):
        raise ValueError("private article URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("private article URL")
    port = parts.port
    netloc = host if port is None or (parts.scheme, port) in (("https", 443), ("http", 80)) else f"{host}:{port}"
    tracking = set(opts["tracking_query_parameters"])
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in tracking]
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", urlencode(sorted(query)), ""))


def retry_delay(headers, attempt, opts):
    value = headers.get("Retry-After") if headers else None
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return opts["backoff_seconds"] * 2**attempt


def fetch_json(url, opts, opener, sleeper):
    for attempt in range(opts["attempts"]):
        try:
            request = Request(url, headers={"User-Agent": opts["user_agent"]})
            with opener(request, timeout=opts["timeout_seconds"]) as response:
                raw = response.read(opts["max_response_bytes"] + 1)
            if len(raw) > opts["max_response_bytes"]:
                raise CollectionError("response_too_large")
            document = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(document, dict) or not isinstance(document.get("articles"), list):
                raise CollectionError("invalid_articles_envelope")
            return document["articles"]
        except HTTPError as exc:
            delay = retry_delay(exc.headers, attempt, opts)
            code = exc.code
            exc.close()
            if code not in (429, 500, 502, 503, 504) or attempt + 1 == opts["attempts"]:
                raise CollectionError(f"http_{code}") from None
            if delay > opts["max_retry_wait_seconds"]:
                raise CollectionError(f"http_{code}:retry_after_exceeds_budget") from None
            sleeper(max(delay, opts["query_interval_seconds"]))
        except (OSError, HTTPException) as exc:
            if attempt + 1 == opts["attempts"]:
                raise CollectionError(f"network_{type(exc).__name__}") from None
            sleeper(max(opts["backoff_seconds"] * 2**attempt, opts["query_interval_seconds"]))
        except (ValueError, UnicodeError) as exc:
            raise CollectionError(f"invalid_response_{type(exc).__name__}") from None
    raise CollectionError("request_failed")


def parse_article(item, config):
    if not isinstance(item, dict):
        raise TypeError("article is not an object")
    opts = config["collection"]
    title = plain_title(item.get("title"))
    if not title or len(title) > opts["max_title_chars"]:
        raise ValueError("invalid title length")
    url = canonical_url(item.get("url"), opts)
    domain = item.get("domain")
    if not isinstance(domain, str) or domain.lower().removeprefix("www.") != urlsplit(url).hostname.removeprefix("www."):
        raise ValueError("publisher domain mismatch")
    observed = datetime.strptime(item["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    language = item.get("language", "unknown")
    if not isinstance(language, str) or len(language) > 80:
        raise ValueError("invalid language")
    return {
        "id": hashlib.sha256(url.encode()).hexdigest()[:20],
        "title": title, "url": url, "publisher": domain.lower().removeprefix("www."),
        "language": language, "indexed_at": observed.isoformat(), "published_at": None,
        "analysis_scope": "headline_only", "source": opts["provider"],
    }


def collect(config, now=None, opener=urlopen, sleeper=time.sleep):
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("collection time must be timezone-aware")
    now = now.astimezone(UTC)
    opts = config["collection"]
    start = now - timedelta(days=opts["lookback_days"])
    stats = {"fetched": 0, "invalid_metadata": 0, "out_of_window": 0, "not_target": 0,
             "duplicates": 0, "limit_excluded": 0, "selected": 0}
    rows, errors, warnings = {}, [], []
    successes = valid_metadata = 0
    person = [normalized(v) for v in config["target"]["person_aliases"]]
    organization = [normalized(v) for v in config["target"]["organization_aliases"]]
    for index, query in enumerate(opts["queries"]):
        if index:
            sleeper(opts["query_interval_seconds"])
        params = {"query": query, "mode": "artlist", "format": "json",
                  "maxrecords": opts["max_results_per_query"],
                  "timespan": f"{opts['lookback_days']}d", "sort": "datedesc"}
        try:
            items = fetch_json(opts["endpoint"] + "?" + urlencode(params), opts, opener, sleeper)
        except CollectionError as exc:
            errors.append({"query": query, "error": str(exc)})
            continue
        successes += 1
        stats["fetched"] += len(items)
        if len(items) >= opts["max_results_per_query"]:
            warnings.append("upstream_limit_reached")
        for item in items:
            try:
                article = parse_article(item, config)
            except (ValueError, KeyError, TypeError):
                stats["invalid_metadata"] += 1
                continue
            valid_metadata += 1
            indexed = datetime.fromisoformat(article["indexed_at"])
            if not start <= indexed <= now:
                stats["out_of_window"] += 1
                continue
            title = normalized(article["title"])
            if not any(v in title for v in person) or not any(v in title for v in organization):
                stats["not_target"] += 1
                continue
            article["collected_at"] = now.isoformat()
            old = rows.get(article["url"])
            if old:
                stats["duplicates"] += 1
            if old is None or article["indexed_at"] > old["indexed_at"]:
                rows[article["url"]] = article
    ordered = sorted(rows.values(), key=lambda a: (a["indexed_at"], a["url"]), reverse=True)
    selected = ordered[:opts["max_articles"]]
    stats["selected"] = len(selected)
    stats["limit_excluded"] = len(ordered) - len(selected)
    if successes == 0 or (stats["fetched"] and valid_metadata == 0):
        status = "failed"
    elif errors or warnings or stats["invalid_metadata"]:
        status = "partial"
    else:
        status = "ok" if selected else "empty"
    return {
        "schema_version": 1, "status": status, "target": config["target"]["name"],
        "checked_at": now.isoformat(), "window_start": start.isoformat(), "window_end": now.isoformat(),
        "date_basis": "gdelt_observation_not_publication", "coverage": "search_sample_not_exhaustive",
        "selection": "latest_observed_target_in_headline", "statistics": stats,
        "publishers": sorted({a["publisher"] for a in selected}),
        "source_attribution": {"name": opts["attribution_name"], "url": opts["attribution_url"]},
        "warnings": sorted(set(warnings)), "errors": errors, "articles": selected,
    }


def atomic_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".collection-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_result(result, data_dir):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if result["status"] != "failed" and result["articles"]:
        atomic_json(data_dir / "articles.json", result)
    status = {k: v for k, v in result.items() if k != "articles"}
    atomic_json(data_dir / "collection_status.json", status)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        result = collect(config)
        write_result(result, args.data_dir)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 1
    print(json.dumps({k: result[k] for k in ("status", "statistics", "publishers", "warnings", "errors")}, ensure_ascii=False, indent=2))
    return {"ok": 0, "failed": 1}.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
