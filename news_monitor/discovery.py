"""Story selection and retention of earlier verified articles across runs."""

from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from zoneinfo import ZoneInfo

from news_monitor.collect import normalized, strip_site_suffix


def story_key(row):
    # Not the date: sources may report the same story a day or an update apart.
    return row["publisher"], normalized(row["title"])


def kst_midnight(day):
    return datetime.combine(day, day_time.min, ZoneInfo("Asia/Seoul")).astimezone(UTC)


def keep_whole_days(ordered, limit, start):
    """At most limit newest-first rows and the window start they are complete from. A day the limit splits is
    dropped whole and the window starts after it, so no day in the window shows fewer articles than were found."""
    if len(ordered) <= limit:
        return ordered, start
    day = date.fromisoformat(ordered[limit]["published_at"])
    kept = [row for row in ordered[:limit] if row["published_at"] > day.isoformat()]
    if not kept:  # One day alone exceeds the limit: keep its newest rows and start the window on it.
        return ordered[:limit], max(start, kst_midnight(day))
    return kept, max(start, kst_midnight(day + timedelta(days=1)))


def select_stories(rows, stats, limit, start):
    """One row per story across subdomain copies, newest publication first, at most limit, and the window start."""
    stories = {}
    for row in rows:
        key = story_key(row)
        if key in stories:
            stats["duplicates"] += 1
        # Keep the same URL for a story whatever order the sources returned it in.
        if key not in stories or row["url"] < stories[key]["url"]:
            stories[key] = row
    ordered = sorted(stories.values(), key=lambda row: (row["published_at"], row["url"]), reverse=True)
    selected, start = keep_whole_days(ordered, limit, start)
    stats.update(selected=len(selected), limit_excluded=len(ordered) - len(selected))
    return selected, start


def merge_previous(result, previous, config):
    """Retain validated in-window metadata, never relabel old observations as new."""
    from news_monitor.report import prepare_collection, timestamp

    if result["status"] == "failed" or not previous:
        return result
    try:
        checked = timestamp(result["checked_at"])
        old_checked = timestamp(previous["checked_at"])
        if old_checked > checked:
            raise ValueError("future previous collection")
        clean = prepare_collection(previous, previous, config, now=old_checked)
        if clean["status"] not in ("ok", "partial"):
            return result
        # A window the cap trimmed stays trimmed until the lookback start passes it.
        start = max(timestamp(result["window_start"]), timestamp(clean["window_start"]))
        old_first = old_checked.astimezone(ZoneInfo("Asia/Seoul")).date() - timedelta(days=config["collection"]["lookback_days"] - 1)
        if (previous["statistics"]["limit_excluded"] and clean["articles"]
                and timestamp(clean["window_start"]) == kst_midnight(old_first)):
            # Saved before the cap dropped whole days: the rows it cut may share its oldest day.
            oldest = date.fromisoformat(min(row["published_at"] for row in clean["articles"]))
            start = max(start, kst_midnight(oldest + timedelta(days=1)))
        first = start.astimezone(ZoneInfo("Asia/Seoul")).date()
        end = checked.astimezone(ZoneInfo("Asia/Seoul")).date()
        old = {}
        for row in clean["articles"]:
            row["title"] = strip_site_suffix(row["title"])  # Saved before search page titles were trimmed.
            key = story_key(row)
            # Older saves may hold one story per subdomain; the earliest first sighting wins.
            if (first <= date.fromisoformat(row["published_at"]) <= end
                    and (key not in old or row["first_seen_at"] < old[key]["first_seen_at"])):
                old[key] = row
    except (ValueError, KeyError, TypeError, OverflowError):
        result["warnings"].append("previous_collection_rejected")
        return result
    in_window = [row for row in result["articles"] if row["published_at"] >= first.isoformat()]
    result["statistics"]["out_of_window"] += len(result["articles"]) - len(in_window)
    fresh = {story_key(row): row for row in in_window}
    old_urls = {row["url"]: row for row in old.values()}
    fresh_urls = {row["url"] for row in fresh.values()}
    for key in old.keys() & fresh.keys():
        fresh[key]["first_seen_at"] = old[key]["first_seen_at"]
    for row in fresh.values():
        if row["url"] in old_urls:
            row["first_seen_at"] = old_urls[row["url"]]["first_seen_at"]
    remaining_old = {key: row for key, row in old.items() if row["url"] not in fresh_urls}
    ordered = sorted({**remaining_old, **fresh}.values(), key=lambda row: (row["published_at"], row["url"]), reverse=True)
    selected, start = keep_whole_days(ordered, config["collection"]["max_articles"], start)
    excluded = len(ordered) - len(selected)
    result["statistics"]["limit_excluded"] += excluded
    if excluded:
        result["status"] = "partial"
    retained = sum(story_key(row) not in fresh for row in selected)
    reconfirmed = sum(story_key(row) in fresh and (story_key(row) in old or row["url"] in old_urls) for row in selected)
    result["statistics"].update(selected=len(selected), retained=retained, reconfirmed=reconfirmed,
                                newly_found=len(selected) - retained - reconfirmed)
    result.update(articles=selected, window_start=start.isoformat(), publishers=sorted({row["publisher"] for row in selected}))
    result["warnings"] = [value for value in result["warnings"] if value != "publisher_goal_not_met"]
    if len(result["publishers"]) < config["collection"]["publisher_goal"]:
        result["warnings"].append("publisher_goal_not_met")
    if retained and retained == len(selected):
        result["status"] = "partial"
        result["warnings"].append("retained_articles_only")
    return result
