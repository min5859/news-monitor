"""Build only explicitly named static artifacts; never copy runtime directories."""

import hashlib
import json
import os
import tempfile
from datetime import date, timedelta
from html import escape
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

from news_monitor.analysis import SENTIMENTS
from news_monitor.briefing import article_day, kst_day, select_groups
from news_monitor.collect import ROOT, atomic_json
from news_monitor.report import summarize, timestamp

LABELS = {"positive": "긍정", "negative": "부정", "neutral": "중립", "mixed": "혼합", "abstain": "판단 보류", "unrelated": "관련 없음"}
STATUS = {"ok": "자동 분석 샘플", "partial": "일부 처리 미완료", "empty": "조건에 맞는 기사 없음", "failed": "수집·검증 실패",
          "stale": "이전 수집 결과 · 최신 조회 미완료"}
OUTPUT_FILES = {"index.html", "styles.css", "app.js", "report.json"}
DATE_NOTES = {"publisher_date": ("기사의 공개 발행일 기준입니다.", "공개 발행일 기준 · AI 웹 검색 수집"),
              "reported_publication_date": ("언론사 사이트맵·RSS 또는 검색 API가 제공한 발행일입니다. 일부는 갱신일일 수 있습니다.",
                                            "언론사·검색 API 제공 발행일 기준 · 사이트맵·RSS·뉴스 검색 수집"),
              "gdelt_observation_not_publication": ("날짜는 KST 관측일이며 발행일이 아닙니다.", "GDELT 관측 시각 기준")}


def h(value):
    return escape(str(value), quote=True)


def display_time(value, short=False):
    if value is None:
        return "확인 불가"
    return timestamp(value).astimezone(ZoneInfo("Asia/Seoul")).strftime("%m.%d" if short else "%Y.%m.%d %H:%M KST")


def dashboard_content(report):
    empty = {"period_options": "", "topic_options": "", "scope_note": "자료 범위를 확인해 주세요.",
             "coverage_detail": "", "briefing_lines": "<li>검증된 핵심 브리핑을 표시할 수 없습니다.</li>",
             "briefing_meta": "이슈 요약 미완료", "issue_cards": "", "issue_count": "—",
             "trend_rows": "", "dashboard_data": "null"}
    insights = report.get("insights")
    if not insights or not insights["periods"] or report["status"] == "failed":
        return empty
    view = next(row for row in insights["periods"] if row["days"] == insights["default_period_days"])
    by_id = {row["id"]: row for row in report["articles"]}
    rows = [by_id[identity] for identity in view["article_ids"]]
    groups = select_groups(insights["groups"], rows)
    groups.sort(key=lambda group: (max(article_day(by_id[identity]) for identity in group["article_ids"]),
                                   len({by_id[identity]["publisher"] for identity in group["article_ids"]})), reverse=True)
    cards, lines = [], []
    for index, group in enumerate(groups):
        members = [by_id[identity] for identity in group["article_ids"]]
        publishers = len({row["publisher"] for row in members})
        if index < 3:
            lines.append(f'<li>{h(group["summary"])} ({publishers}개 매체·{len(members)}건)</li>')
        quotes = "".join(f'<li><a href="#article-{h(entry["article_id"])}">{h(by_id[entry["article_id"]]["publisher"])}: “{h(entry["quote"])}”</a></li>' for entry in group["evidence"])
        cards.append(f'<article class="issue-card"><span class="issue-topic">{h(insights["topics"][group["topic"]])}</span>'
                     f'<h3>{h(group["label"])}</h3><p>{h(group["summary"])}</p>'
                     f'<p class="footnote">{len(members)}건 · {publishers}개 매체</p>'
                     f'<details><summary>제목 근거·관련 기사</summary><ul>{quotes}</ul></details></article>')
    trend = []
    for day in reversed(view["daily"]):
        cells = [day["date"], "자료 범위 밖", *["—"] * 5]
        if day["covered"]:
            cells = [day["date"], day["article_count"], *[day["statistics"]["counts"][key] for key in SENTIMENTS], day["statistics"]["abstained"]]
        trend.append("<tr>" + "".join(f"<td>{h(value)}</td>" for value in cells) + "</tr>")
    # No external URLs are embedded in script data; source links remain escaped anchors.
    payload = {key: insights[key] for key in ("as_of_day", "available_start", "groups", "topics")}
    payload["articles"] = [{"id": row["id"], "title": row["title"], "publisher": row["publisher"],
                            "day": article_day(row), "relevance": row["classification"]["relevance"],
                            "sentiment": row["classification"]["sentiment"],
                            "first_seen_day": kst_day(row["observation"]["first_seen_at"]),
                            "reused": row["observation"]["reused"]} for row in report["articles"]]
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for source, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        data = data.replace(source, escaped)
    note = f'{view["start"]} — {view["end"]} · 실제 자료 범위 {insights["available_start"]} — {insights["as_of_day"]}'
    if not view["coverage_complete"]:
        note += " · 요청 기간 일부는 자료 범위 밖입니다."
    meta = "AI가 제목만으로 묶고 작성한 미검토 요약입니다. 같은 사건인지·요약의 정확성을 보장하지 않습니다."
    if insights["status"] != "ok":
        meta = "AI 이슈 묶음 미완료/비활성: 요약을 만들지 않고 기사 제목별로 표시합니다."
    return {"period_options": "".join(f'<option value="{row["days"]}"' + (' selected' if row["days"] == insights["default_period_days"] else '') + f'>{"기준일 하루" if row["days"] == 1 else str(row["days"]) + "일"}</option>' for row in insights["periods"]),
            "topic_options": "".join(f'<option value="{h(key)}">{h(label)}</option>' for key, label in insights["topics"].items()),
            "scope_note": h(note),
            "coverage_detail": h(f'핵심 이슈 {view["issue_count"]}개 · 기준일에 처음 확인 {view["newly_seen_count"]}건 · 이전 수집 유지 {view["retained_count"]}건'),
            "briefing_lines": "".join(lines) or "<li>선택 범위에서 핵심 이슈로 정리할 관련 기사를 확보하지 못했습니다.</li>",
            "briefing_meta": h(meta), "issue_cards": "".join(cards), "issue_count": f'{view["issue_count"]}개 이슈 / {len(rows)}건 기사',
            "trend_rows": "".join(trend), "dashboard_data": data}


def alert_items(rows):
    items = []
    for row in sorted(rows, key=lambda row: (article_day(row), row["id"]), reverse=True):
        decision = row["classification"]
        when = f"발행 {row['published_at']}" if row["published_at"] else f"관측 {display_time(row['indexed_at'])}"
        items.append(f'<li><span><span class="label {h(decision["sentiment"])}">{LABELS[decision["sentiment"]]}</span> '
                     f'<a href="{h(row["url"])}" target="_blank" rel="noopener noreferrer">{h(row["title"])} ↗</a></span>'
                     f'<span class="footnote">{h(row["publisher"])} · {h(when)} · {h(decision["reason"])}</span></li>')
    return '<ul class="alert-list">' + "".join(items) + "</ul>"


def negative_alert(report):
    """Fixed to the collection day, whatever the filters: an empty list is said only after that day's collection."""
    html = '<section class="panel alert" id="negative-alert" aria-labelledby="alert-title"><p class="eyebrow">NEGATIVE WATCH</p>'
    if report["status"] in ("failed", "stale") or not report["window_end"]:
        return (html + '<h2 id="alert-title">오늘의 부정 기사</h2><p>오늘 수집이 완료되지 않아 새 부정 기사를 확인하지 못했습니다. '
                       '아래 자료는 이번 조회의 새 결과가 아닐 수 있습니다.</p></section>')
    day = kst_day(report["window_end"])
    yesterday, week = ((date.fromisoformat(day) - timedelta(days=offset)).isoformat() for offset in (1, 6))
    fresh = [row for row in report["articles"] if kst_day(row["observation"]["first_seen_at"]) == day]
    watched = [row for row in report["articles"] if row["classification"]["relevance"] == "related"
               and row["classification"]["sentiment"] in ("negative", "mixed")]
    # "Today" is news first seen this morning and published since yesterday; older first sightings are late finds.
    today = [row for row in watched if row in fresh and yesterday <= article_day(row)]
    recent = [row for row in watched if row not in today and week <= article_day(row) <= day]
    late = [row for row in watched if row in fresh and article_day(row) < week]
    unsure = sum(row["classification"]["sentiment"] == "abstain" and row["classification"]["relevance"] != "unrelated" for row in fresh)
    html += f'<h2 id="alert-title">오늘의 부정 기사</h2><p class="unit">어제·오늘 발행 · {h(day)} 아침 수집 기준</p>'
    html += (f"<p><strong>{len(today)}건</strong>을 새로 확인했습니다.</p>" + alert_items(today) if today
             else "<p>어제·오늘 발행된 새 부정·혼합 기사는 없습니다.</p>")
    if recent:
        html += f"<h3>최근 7일 부정·혼합 기사 {len(recent)}건</h3>" + alert_items(recent)
    if late:
        html += f"<h3>이번 수집에서 처음 찾은 이전 부정·혼합 기사 {len(late)}건</h3>" + alert_items(late)
    if unsure:
        html += f"<p>제목만으로 판단하지 못한 새 기사 {unsure}건이 있습니다. 기사 목록의 '판단 보류'에서 직접 확인해 주세요.</p>"
    if report["status"] == "partial":
        html += '<p class="footnote">일부 수집·분류가 완료되지 않아 빠진 기사가 있을 수 있습니다.</p>'
    return html + ('<p class="footnote">제목에 대상 인물이 나온 기사만 대상이며 AI가 제목만으로 분류한 미검토 결과입니다. '
                   '기간·분류 필터와 관계없이 수집일 기준으로 표시합니다.</p></section>')


def render(report):
    failed = report["status"] == "failed"
    stats = report["statistics"]
    scoped_rows = report["articles"]
    insights = report.get("insights")
    if not failed and insights and insights["periods"]:
        view = next(row for row in insights["periods"] if row["days"] == insights["default_period_days"])
        stats = view["statistics"]
        scoped_rows = [row for row in report["articles"] if row["id"] in view["article_ids"]]
    scoped_ids = {row["id"] for row in scoped_rows}
    unknown = "—"
    metrics = (("선정 기사", None if failed else len(scoped_rows), "선택 범위 · 중복 제거 후"),
               ("확보 매체", None if failed else len({row["publisher"] for row in scoped_rows}), f"목표 {report['publisher_goal']}곳 · 전수 수집 아님"),
               ("분류 완료", None if stats is None else stats["denominator"], "대상 관련 기사만 집계"),
               ("판단 보류", None if stats is None else stats["abstained"], "분모에서 제외"))
    metric_html = "".join(f'<div class="metric"><span class="metric-label">{h(label)}</span><strong class="metric-value">{unknown if value is None else value}</strong><span class="metric-note">{h(note)}</span></div>' for label, value, note in metrics)
    tones = []
    for label in SENTIMENTS:
        count = None if stats is None else stats["counts"][label]
        percentage = None if stats is None else stats["percentages"][label]
        text = "산출 불가" if percentage is None else f"{percentage:.1f}%"
        meter = "" if percentage is None else f'<meter min="0" max="100" value="{percentage:.1f}" aria-label="{LABELS[label]} 비율">{text}</meter>'
        tones.append(f'<div class="tone {label}"><span>{LABELS[label]}</span><span class="tone-count">{unknown if count is None else count}건</span><strong>{text}</strong>{meter}</div>')
    publisher_html = []
    scoped_publishers = [{"publisher": name, "article_count": sum(row["publisher"] == name for row in scoped_rows),
                          **summarize([row for row in scoped_rows if row["publisher"] == name])}
                         for name in sorted({row["publisher"] for row in scoped_rows})]
    for row in scoped_publishers:
        cells = [row["publisher"], row["article_count"], row["denominator"], *[row["counts"][key] for key in SENTIMENTS], row["abstained"], row["unrelated"]]
        publisher_html.append("<tr>" + "".join(f"<td>{h(value)}</td>" for value in cells) + "</tr>")
    article_html = []
    for row in report["articles"]:
        decision = row["classification"]
        label = "unrelated" if decision["relevance"] == "unrelated" else decision["sentiment"]
        day = row["published_at"] or timestamp(row["indexed_at"]).astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
        article_time = f"발행 {day}" if row["published_at"] else f"관측 {display_time(row['indexed_at'])}"
        evidence = f'<p class="evidence">제목 근거: “{h(decision["evidence"])}”</p>' if decision["evidence"] else ""
        if row.get("evidence_omitted_for_display"):
            evidence = '<p class="evidence">인명 표기 변경으로 제목 근거의 일부 표시를 생략했습니다.</p>'
        relevance = {"related": "대상 관련", "uncertain": "관련성 불확실", "unrelated": "관련 없음"}[decision["relevance"]]
        outcome = "AI 호출·응답 검증 실패" if row["analysis_failed"] else "AI 자동분류 · 미검토"
        observation = row.get("observation", {})
        if observation.get("reused"):
            outcome += f" · 이전 수집 유지(마지막 확인 {display_time(observation['last_seen_at'])})"
        hidden = "" if row["id"] in scoped_ids else " hidden"
        article_html.append(f'<article class="article" id="article-{h(row["id"])}" data-id="{h(row["id"])}" data-publisher="{h(row["publisher"])}" data-sentiment="{h(label)}" data-day="{day}"{hidden}><div class="article-meta"><span class="label {h(label)}">{LABELS[label]}</span><span>{h(row["publisher"])}</span><span>{h(article_time)}</span><span>{relevance} · {outcome}</span></div><h3><a href="{h(row["url"])}" target="_blank" rel="noopener noreferrer">{h(row["title"])} ↗</a></h3><p class="reason">{h(decision["reason"])}</p>{evidence}</article>')
    notice = "제목만으로 자동 분류한 미검토 샘플입니다. 실제 확보 범위 안에서만 해석해 주세요."
    if failed:
        notice = "수집 또는 입력 검증에 실패했습니다. 기사 수와 비율을 산출할 수 없습니다. 이전 자료를 이번 실행의 새 기사로 표시하지 않습니다."
    elif report["status"] == "empty":
        notice = "조회는 완료했으나 선정 조건에 맞는 기사가 없습니다. 이 결과가 전체 언론 보도 0건을 뜻하지는 않습니다."
    elif report["status"] == "partial":
        notice = "일부 수집·분류가 완료되지 않았습니다. 확보된 기사와 검증된 분류만 표시하며, 분석 실패는 판단 보류로 남깁니다."
    elif report["status"] == "stale":
        attempt = report["latest_attempt"]
        notice = f"최근 조회({display_time(attempt['checked_at'])}, {STATUS[attempt['status']]})에서 새 기사를 확보하지 못해 이전 수집 결과를 표시합니다. 아래 기사와 비율은 이전 수집 시각의 자료이며 이번 조회의 새 결과가 아닙니다."
    if "publisher_goal_not_met" in report["warnings"]:
        notice += f" 매체 확보 목표 {report['publisher_goal']}곳에는 미달했습니다."
    if report.get("test_fixture"):
        notice = "UI 테스트용 가상 데이터입니다. 실제 뉴스·실제 수집 결과가 아닙니다. " + notice
    notice += " 기사 제목·판단 이유·근거의 인명 표기를 치환했습니다. 원문 링크에서 대상을 확인할 수 있어 완전 익명화는 아닙니다."
    pn = None if stats is None else stats["positive_negative_only"]
    ratio_note = "긍정 대 부정 비율: 산출 불가."
    if pn and pn["denominator"]:
        ratio_note = f"긍정 대 부정만 비교: {pn['percentages']['positive']:.1f}% / {pn['percentages']['negative']:.1f}% (분모 {pn['denominator']}건 · 중립·혼합·보류 제외)."
    attribution = report["source_attribution"]
    publication_dates = report["date_basis"] != "gdelt_observation_not_publication"
    daily_summary = ""
    if publication_dates and report["window_end"]:
        day = timestamp(report["window_end"]).astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        rows = [row for row in scoped_rows if row["published_at"] == day]
        daily = summarize(rows)
        values = " · ".join(f"{LABELS[key]} {daily['counts'][key]}건" for key in SENTIMENTS)
        ratios = "비율 산출 불가" if not daily["denominator"] else " · ".join(
            f"{LABELS[key]} {daily['percentages'][key]:.1f}%" for key in SENTIMENTS)
        daily_summary = (f'<section class="panel" id="daily-summary" aria-label="당일 발행 기사"><h2>{day} 발행 기사</h2>'
                         f'<p>{len(rows)}건 · 분류 완료 {daily["denominator"]}건 · 판단 보류 {daily["abstained"]}건</p>'
                         f'<p>{values}</p><p class="footnote">{ratios}. 수집일이 아닌 발행일 기준이며 아래 분포는 전체 조회 기간 기준입니다.</p></section>')
    values = {"title": h(f"{report['target']['organization']} {report['target']['name']}"),
              "status_label": STATUS[report["status"]], "checked": h(display_time(report["collection_checked_at"])),
              "window": h(f"{display_time(report['window_start'], True)} — {display_time(report['window_end'], True)}"),
              "notice": h(notice), "metrics": metric_html, "denominator": unknown if stats is None else stats["denominator"],
              "tones": "".join(tones), "ratio_note": h(ratio_note),
              "publishers": "".join(publisher_html) or '<tr><td colspan="9">표시할 매체 데이터가 없습니다.</td></tr>',
              "publisher_options": "".join(f'<option value="{h(row["publisher"])}">{h(row["publisher"])}</option>' for row in report["publishers"]),
              "articles": "".join(article_html) or '<p class="empty">' + ("수집 상태를 확인해야 합니다." if failed else "선정된 기사가 없습니다.") + "</p>",
              "list_count": unknown if failed else f"{len(scoped_rows)}건",
              "attribution": f'<a href="{h(attribution["url"])}" target="_blank" rel="noopener noreferrer">{h(attribution["name"])}</a>',
              "model": h(f"{report['analysis']['provider']} / {report['analysis']['model']}"),
              "prompt_version": h(report["analysis"]["prompt_sha256"]),
              "generated": h(display_time(report["generated_at"])), "daily_summary": daily_summary,
              "negative_alert": negative_alert(report)}
    date_filter_note, date_basis_note = DATE_NOTES[report["date_basis"]]
    values.update(window_label="발행일 범위" if publication_dates else "관측 기간",
                  date_filter_label="발행일" if publication_dates else "관측일",
                  date_filter_note=date_filter_note, date_basis_note=date_basis_note)
    values.update(dashboard_content(report))
    values["asset_version"] = hashlib.sha256(b"".join((ROOT / "web" / name).read_bytes()
                                                     for name in ("app.js", "styles.css"))).hexdigest()[:12]
    return Template((ROOT / "web" / "index.html").read_text()).substitute(values)


def atomic_text(path, content):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".site-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def build_site(report, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(path.name not in OUTPUT_FILES or path.is_dir() for path in output_dir.iterdir()):
        raise ValueError("output directory must contain only owned static files")
    html = render(report)
    # These four names are the whole artifact; no data/, logs/, env or raw output.
    for name in ("styles.css", "app.js"):
        atomic_text(output_dir / name, (ROOT / "web" / name).read_text())
    atomic_json(output_dir / "report.json", report)
    atomic_text(output_dir / "index.html", html)
