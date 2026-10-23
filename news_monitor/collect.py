"""Bounded GDELT metadata collection; never fetch article bodies or images."""

import argparse
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from news_monitor.display import display_name

ROOT = Path(__file__).resolve().parents[1]


class CollectionError(Exception):
    def __init__(self, message, retry_after_seconds=None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


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
    diagnostic_limit = config["diagnostics"]["max_events"]
    if type(diagnostic_limit) is not int or not 1 <= diagnostic_limit <= 1000:
        raise ValueError("invalid diagnostics event limit")
    opts = config["collection"]
    if opts["provider"] not in ("gdelt_doc", "codex_web_search"):
        raise ValueError("unsupported provider")
    if not opts["endpoint"].startswith("https://"):
        raise ValueError("collection endpoint must use HTTPS")
    for name, lower, upper in (("lookback_days", 1, 90), ("max_results_per_query", 1, 250),
                                ("max_articles", 1, 250), ("attempts", 1, 3),
                                ("publisher_goal", 1, 250)):
        value = opts[name]
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"invalid {name}")
    for name in ("timeout_seconds", "backoff_seconds", "max_retry_wait_seconds",
                 "query_interval_seconds", "max_response_bytes", "max_title_chars",
                 "minimum_run_interval_seconds", "rate_limit_cooldown_seconds"):
        if type(opts[name]) not in (int, float) or not math.isfinite(opts[name]) or opts[name] <= 0:
            raise ValueError(f"invalid {name}")
    if opts["query_interval_seconds"] < 5:
        raise ValueError("GDELT query interval must be at least five seconds")
    if min(opts["minimum_run_interval_seconds"], opts["rate_limit_cooldown_seconds"]) < opts["query_interval_seconds"]:
        raise ValueError("run interval and cooldown must not be shorter than query interval")
    for name in ("max_response_bytes", "max_title_chars"):
        if type(opts[name]) is not int:
            raise ValueError(f"{name} must be an integer")
    for values in (opts["queries"], config["target"]["person_aliases"],
                   config["target"]["organization_aliases"]):
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("queries and aliases must be nonempty string lists")
    display_name(config)
    if opts["provider"] == "codex_web_search":
        for key, low, high in (("codex_timeout_seconds", 1, 900), ("codex_output_bytes", 1024, 2097152),
                               ("max_search_calls", 1, 30), ("search_finish_reserve_calls", 1, 29),
                               ("search_query_calls_per_group", 1, 30)):
            if type(opts[key]) is not int or not low <= opts[key] <= high:
                raise ValueError(f"invalid {key}")
        groups = opts.get("search_groups", [[config["target"]["name"]]])
        if (not isinstance(groups, list) or not 1 <= len(groups) <= min(6, opts["max_search_calls"])
                or any(not isinstance(group, list) or not 1 <= len(group) <= 4
                       or any(not isinstance(query, str) or not query.strip() or len(query) > 200 for query in group)
                       for group in groups)):
            raise ValueError("invalid search_groups")
        if type(opts.get("require_organization_in_headline", True)) is not bool:
            raise ValueError("invalid headline selection option")
        for key, directory in (("schema_file", "schemas"), ("search_prompt_file", "prompts")):
            if not (ROOT / opts[key]).resolve().is_relative_to(ROOT / directory):
                raise ValueError("unsafe discovery resource path")
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
            delay = float(value)
            if math.isfinite(delay):
                return max(0, delay)
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
            if code == 429:
                raise CollectionError("http_429", max(delay, opts["rate_limit_cooldown_seconds"])) from None
            if code not in (500, 502, 503, 504):
                raise CollectionError(f"http_{code}") from None
            if delay > opts["max_retry_wait_seconds"]:
                raise CollectionError(f"http_{code}:retry_after_exceeds_budget", delay) from None
            if attempt + 1 == opts["attempts"]:
                raise CollectionError(f"http_{code}", delay) from None
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
    if opts["provider"] == "codex_web_search":
        from news_monitor.discovery import collect_web

        return collect_web(config, now)
    start = now - timedelta(days=opts["lookback_days"])
    stats = {"fetched": 0, "invalid_metadata": 0, "out_of_window": 0, "not_target": 0,
             "duplicates": 0, "limit_excluded": 0, "selected": 0,
             "person_only": 0, "organization_only": 0, "neither_target": 0}
    rows, errors, warnings = {}, [], []
    successes = valid_metadata = 0
    retry_after_seconds = None
    attempted_queries = 0
    person = [normalized(v) for v in config["target"]["person_aliases"]]
    organization = [normalized(v) for v in config["target"]["organization_aliases"]]
    for index, query in enumerate(opts["queries"]):
        if index:
            sleeper(opts["query_interval_seconds"])
        params = {"query": query, "mode": "artlist", "format": "json",
                  "maxrecords": opts["max_results_per_query"],
                  "timespan": f"{opts['lookback_days']}d", "sort": "datedesc"}
        try:
            attempted_queries += 1
            items = fetch_json(opts["endpoint"] + "?" + urlencode(params), opts, opener, sleeper)
        except CollectionError as exc:
            errors.append({"query": query, "error": str(exc)})
            if exc.retry_after_seconds is not None:
                retry_after_seconds = exc.retry_after_seconds
                if index + 1 < len(opts["queries"]):
                    warning = "remaining_queries_skipped_rate_limit" if str(exc) == "http_429" else "remaining_queries_skipped_retry_after"
                    warnings.append(warning)
                break
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
            has_person = any(v in title for v in person)
            has_organization = any(v in title for v in organization)
            if not has_person or not has_organization:
                stats["not_target"] += 1
                reason = "person_only" if has_person else "organization_only" if has_organization else "neither_target"
                stats[reason] += 1
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
    publishers = sorted({a["publisher"] for a in selected})
    goal_met = len(publishers) >= opts["publisher_goal"]
    if successes and not goal_met:
        warnings.append("publisher_goal_not_met")
    return {
        "schema_version": 1, "status": status, "target": config["target"]["name"],
        "checked_at": now.isoformat(), "window_start": start.isoformat(), "window_end": now.isoformat(),
        "date_basis": "gdelt_observation_not_publication", "coverage": "search_sample_not_exhaustive",
        "selection": "latest_observed_target_in_headline", "statistics": stats,
        "publishers": publishers,
        "coverage_goals": {"publisher_goal": opts["publisher_goal"],
                           "actual_publishers": len(publishers), "publisher_goal_met": goal_met},
        "source_queries": {"planned": len(opts["queries"]), "attempted": attempted_queries, "succeeded": successes},
        "retry_after_seconds": retry_after_seconds,
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


@contextmanager
def collection_lock(data_dir):
    """OS releases the lock on exit/crash; never delete a lock held by another run."""
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data_dir / ".collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def failure_result(error, now):
    return {"schema_version": 1, "status": "failed", "checked_at": now.isoformat(),
            "statistics": {"selected": 0}, "publishers": [], "warnings": [],
            "errors": [{"error": error}], "articles": []}


def _run_collection(config, data_dir, now=None, opener=urlopen, sleeper=time.sleep):
    """Caller holds collection_lock. Persist throttling before any network I/O."""
    def clock():
        return now if now is not None else datetime.now(UTC)

    current = clock()
    if current.tzinfo is None:
        raise ValueError("collection time must be timezone-aware")
    state_path = data_dir / "request_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        if not isinstance(state, dict):
            raise ValueError("invalid request state")
        until = datetime.fromisoformat(state["retry_not_before"])
        if until.tzinfo is None:
            raise ValueError("request state must be timezone-aware")
        if current < until:
            result = failure_result("request_cooldown", current)
            result["errors"][0]["reason"] = state.get("reason", "previous_run_cooldown")
            result["retry_not_before"] = until.isoformat()
            result["source_queries"] = {"planned": len(config["collection"]["queries"]), "attempted": 0, "succeeded": 0}
            write_result(result, data_dir)
            return result
    interval = config["collection"]["minimum_run_interval_seconds"]
    atomic_json(state_path, {"retry_not_before": (current + timedelta(seconds=interval)).isoformat(),
                            "reason": "minimum_run_interval"})
    result = collect(config, current, opener=opener, sleeper=sleeper)
    from news_monitor.diagnostics import record_errors

    record_errors(data_dir, config, "discovery", result["checked_at"],
                  [error for error in result["errors"] if "reason" in error])
    if config["collection"]["provider"] == "codex_web_search" and (data_dir / "articles.json").exists():
        from news_monitor.discovery import merge_previous

        try:
            with (data_dir / "articles.json").open("rb") as stream:
                raw = stream.read(config["collection"]["max_response_bytes"] + 1)
            if len(raw) > config["collection"]["max_response_bytes"]:
                raise ValueError("previous collection too large")
            previous = json.loads(raw)
        except (OSError, ValueError, TypeError):
            previous = None
            result["warnings"].append("previous_collection_rejected")
        result = merge_previous(result, previous, config)
    delay = max(interval, result["retry_after_seconds"] or 0)
    until = (clock() + timedelta(seconds=delay)).isoformat()
    atomic_json(state_path, {"retry_not_before": until,
                            "reason": result["errors"][-1]["error"] if result["retry_after_seconds"] else "minimum_run_interval"})
    result["retry_not_before"] = until
    write_result(result, data_dir)
    return result


def run_collection(config, data_dir, now=None, opener=urlopen, sleeper=time.sleep):
    with collection_lock(data_dir):
        return _run_collection(config, data_dir, now, opener, sleeper)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    try:
        with collection_lock(args.data_dir):
            try:
                config = load_config(args.config)
                result = _run_collection(config, args.data_dir)
            except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
                result = failure_result(type(exc).__name__, datetime.now(UTC))
                write_result(result, args.data_dir)
    except BlockingIOError:
        print(json.dumps({"status": "failed", "error": "already_running"}))
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 1
    fields = ("status", "statistics", "publishers", "coverage_goals", "warnings", "errors", "retry_not_before")
    print(json.dumps({k: result[k] for k in fields if k in result}, ensure_ascii=False, indent=2))
    return {"ok": 0, "failed": 1}.get(result["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
