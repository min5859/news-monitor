"""Build only explicitly named static artifacts; never copy runtime directories."""

import os
import tempfile
from html import escape
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

from news_monitor.analysis import SENTIMENTS
from news_monitor.collect import ROOT, atomic_json
from news_monitor.report import summarize, timestamp

LABELS = {"positive": "긍정", "negative": "부정", "neutral": "중립", "mixed": "혼합", "abstain": "판단 보류", "unrelated": "관련 없음"}
STATUS = {"ok": "자동 분석 샘플", "partial": "일부 처리 미완료", "empty": "조건에 맞는 기사 없음", "failed": "수집·검증 실패",
          "stale": "이전 수집 결과 · 최신 조회 미완료"}
OUTPUT_FILES = {"index.html", "styles.css", "app.js", "report.json"}


def h(value):
    return escape(str(value), quote=True)


def display_time(value, short=False):
    if value is None:
        return "확인 불가"
    return timestamp(value).astimezone(ZoneInfo("Asia/Seoul")).strftime("%m.%d" if short else "%Y.%m.%d %H:%M KST")


def render(report):
    failed = report["status"] == "failed"
    stats = report["statistics"]
    unknown = "—"
    metrics = (("선정 기사", report["article_count"], "URL 중복 제거 후"),
               ("확보 매체", report["publisher_count"], f"목표 {report['publisher_goal']}곳 · 전수 수집 아님"),
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
    for row in report["publishers"]:
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
        article_html.append(f'<article class="article" data-publisher="{h(row["publisher"])}" data-sentiment="{h(label)}" data-day="{day}"><div class="article-meta"><span class="label {h(label)}">{LABELS[label]}</span><span>{h(row["publisher"])}</span><span>{h(article_time)}</span><span>{relevance} · {outcome}</span></div><h3><a href="{h(row["url"])}" target="_blank" rel="noopener noreferrer">{h(row["title"])} ↗</a></h3><p class="reason">{h(decision["reason"])}</p>{evidence}</article>')
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
    publication_dates = report["date_basis"] == "publisher_date"
    daily_summary = ""
    if publication_dates and report["window_end"]:
        day = timestamp(report["window_end"]).astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        rows = [row for row in report["articles"] if row["published_at"] == day]
        daily = summarize(rows)
        values = " · ".join(f"{LABELS[key]} {daily['counts'][key]}건" for key in SENTIMENTS)
        ratios = "비율 산출 불가" if not daily["denominator"] else " · ".join(
            f"{LABELS[key]} {daily['percentages'][key]:.1f}%" for key in SENTIMENTS)
        daily_summary = (f'<section class="panel" aria-label="당일 발행 기사"><h2>{day} 발행 기사</h2>'
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
              "list_count": unknown if failed else f"{len(article_html)}건",
              "attribution": f'<a href="{h(attribution["url"])}" target="_blank" rel="noopener noreferrer">{h(attribution["name"])}</a>',
              "model": h(f"{report['analysis']['provider']} / {report['analysis']['model']}"),
              "prompt_version": h(report["analysis"]["prompt_sha256"]),
              "generated": h(display_time(report["generated_at"])), "daily_summary": daily_summary}
    values.update(window_label="발행일 범위" if publication_dates else "관측 기간",
                  date_filter_label="발행일" if publication_dates else "관측일",
                  date_filter_note="기사의 공개 발행일 기준입니다." if publication_dates else "날짜는 KST 관측일이며 발행일이 아닙니다.",
                  date_basis_note="공개 발행일 기준 · AI 웹 검색 수집" if publication_dates else "GDELT 관측 시각 기준")
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
