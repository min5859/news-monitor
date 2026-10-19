"use strict";

const TONES = ["positive", "negative", "neutral", "mixed"];
const LABELS = {
  positive: "긍정",
  negative: "부정",
  neutral: "중립",
  mixed: "혼합",
  abstain: "판단 보류",
  unrelated: "관련 없음",
};

function percentages(counts) {
  const keys = Object.keys(counts);
  const total = keys.reduce((sum, key) => sum + counts[key], 0);
  if (!total) return Object.fromEntries(keys.map((key) => [key, null]));
  const units = Object.fromEntries(
    keys.map((key) => [key, Math.floor((counts[key] * 1000) / total)]),
  );
  const order = [...keys].sort(
    (a, b) => ((counts[b] * 1000) % total) - ((counts[a] * 1000) % total),
  );
  const remainder = 1000 - keys.reduce((sum, key) => sum + units[key], 0);
  for (const key of order.slice(0, remainder)) units[key] += 1;
  return Object.fromEntries(keys.map((key) => [key, units[key] / 10]));
}

function summarize(rows) {
  const counts = Object.fromEntries(TONES.map((key) => [key, 0]));
  let abstained = 0,
    unrelated = 0;
  for (const row of rows) {
    if (row.relevance === "unrelated") unrelated += 1;
    else if (row.sentiment === "abstain") abstained += 1;
    else counts[row.sentiment] += 1;
  }
  const pn = { positive: counts.positive, negative: counts.negative };
  return {
    counts,
    denominator: Object.values(counts).reduce((a, b) => a + b, 0),
    percentages: percentages(counts),
    abstained,
    unrelated,
    positive_negative_only: {
      denominator: pn.positive + pn.negative,
      percentages: percentages(pn),
    },
  };
}

function shiftDay(day, amount) {
  const value = new Date(day + "T00:00:00Z");
  value.setUTCDate(value.getUTCDate() + amount);
  return value.toISOString().slice(0, 10);
}

function buildView(data, filters) {
  const days = Number(filters.days);
  const start = shiftDay(data.as_of_day, 1 - days);
  const topicIds = new Set(
    data.groups
      .filter((group) => !filters.topic || group.topic === filters.topic)
      .flatMap((group) => group.article_ids),
  );
  const rows = data.articles.filter(
    (row) =>
      row.day >= start &&
      row.day <= data.as_of_day &&
      (!filters.publisher || row.publisher === filters.publisher) &&
      (!filters.sentiment ||
        (row.relevance === "unrelated" ? "unrelated" : row.sentiment) ===
          filters.sentiment) &&
      (!filters.day || row.day === filters.day) &&
      (!filters.topic || topicIds.has(row.id)),
  );
  const byId = new Map(rows.map((row) => [row.id, row]));
  const groups = data.groups
    .flatMap((group) => {
      const ids = group.article_ids.filter((id) => byId.has(id));
      if (!ids.length) return [];
      const members = ids.map((id) => byId.get(id));
      return [
        {
          ...group,
          article_ids: ids,
          members,
          summary:
            ids.length === group.article_ids.length
              ? group.summary
              : "선택된 기사 제목: " + members[0].title,
          evidence: group.evidence.filter((entry) =>
            byId.has(entry.article_id),
          ),
          publisher_count: new Set(members.map((row) => row.publisher)).size,
          latest_day: members
            .map((row) => row.day)
            .sort()
            .at(-1),
          statistics: summarize(members),
        },
      ];
    })
    .sort(
      (a, b) =>
        b.latest_day.localeCompare(a.latest_day) ||
        b.publisher_count - a.publisher_count ||
        a.label.localeCompare(b.label),
    );
  const daily = Array.from({ length: days }, (_, index) => {
    const day = shiftDay(start, index);
    const covered = Boolean(
      data.available_start && day >= data.available_start,
    );
    const dayRows = rows.filter((row) => row.day === day);
    return {
      date: day,
      covered,
      article_count: covered ? dayRows.length : null,
      statistics: covered ? summarize(dayRows) : null,
    };
  });
  return {
    rows,
    groups,
    daily,
    start,
    end: data.as_of_day,
    coverage_complete: Boolean(
      data.available_start && start >= data.available_start,
    ),
    publisher_count: new Set(rows.map((row) => row.publisher)).size,
    statistics: summarize(rows),
    newly_seen_count: rows.filter(
      (row) => row.first_seen_day === data.as_of_day,
    ).length,
    retained_count: rows.filter((row) => row.reused).length,
  };
}

function initializeDashboard() {
  const payload = document.getElementById("dashboard-data");
  if (!payload) return;
  const data = JSON.parse(payload.textContent);
  if (!data) return;
  const get = (id) => document.getElementById(id);
  const controls = [
    "period-filter",
    "publisher-filter",
    "sentiment-filter",
    "topic-filter",
    "date-filter",
  ].map(get);
  const articleNodes = Array.from(document.querySelectorAll(".article"));
  // Model/news strings are text, never HTML. No external URLs in the embedded data.
  function node(tag, text, className) {
    const value = document.createElement(tag);
    if (text !== undefined) value.textContent = text;
    if (className) value.className = className;
    return value;
  }
  function set(id, value) {
    const target = get(id);
    if (target) target.textContent = value;
  }
  function tableRow(values) {
    const tr = node("tr");
    for (const value of values) tr.append(node("td", value));
    return tr;
  }

  function applyFilters() {
    const [days, publisher, sentiment, topic, day] = controls.map(
      (control) => control.value,
    );
    const view = buildView(data, { days, publisher, sentiment, topic, day });
    const ids = new Set(view.rows.map((row) => row.id));
    for (const row of articleNodes) row.hidden = !ids.has(row.dataset.id);
    set("visible-count", `${view.rows.length} / ${data.articles.length}건`);
    get("filter-empty").hidden = view.rows.length !== 0;
    const metrics = document.querySelectorAll(".metric-value");
    [
      view.rows.length,
      view.publisher_count,
      view.statistics.denominator,
      view.statistics.abstained,
    ].forEach((value, index) => {
      metrics[index].textContent = value;
    });
    set(
      "scope-note",
      `${view.start} — ${view.end} · 실제 자료 범위 ${data.available_start} — ${data.as_of_day}${view.coverage_complete ? "" : " · 요청 기간 일부는 자료 범위 밖입니다."}`,
    );
    set(
      "coverage-detail",
      `핵심 이슈 ${view.groups.length}개 · 기준일에 처음 확인 ${view.newly_seen_count}건 · 이전 수집 유지 ${view.retained_count}건 · 관련 없음 ${view.statistics.unrelated}건. 처음 확인한 과거 기사도 발행일은 바꾸지 않습니다.`,
    );
    set("denominator", view.statistics.denominator);
    const tones = get("tones");
    tones.replaceChildren();
    for (const key of TONES) {
      const box = node("div", undefined, `tone ${key}`);
      const percent = view.statistics.percentages[key];
      const text = percent === null ? "산출 불가" : `${percent.toFixed(1)}%`;
      box.append(
        node("span", LABELS[key]),
        node("span", `${view.statistics.counts[key]}건`, "tone-count"),
        node("strong", text),
      );
      if (percent !== null) {
        const meter = node("meter");
        meter.min = 0;
        meter.max = 100;
        meter.value = percent;
        meter.setAttribute("aria-label", `${LABELS[key]} 비율`);
        meter.textContent = text;
        box.append(meter);
      }
      tones.append(box);
    }
    const pn = view.statistics.positive_negative_only;
    set(
      "ratio-note",
      pn.denominator
        ? `긍정 대 부정만 비교: ${pn.percentages.positive.toFixed(1)}% / ${pn.percentages.negative.toFixed(1)}% (분모 ${pn.denominator}건 · 중립·혼합·보류 제외).`
        : "긍정 대 부정 비율: 산출 불가.",
    );
    const dailySummary = get("daily-summary");
    if (dailySummary) {
      const today = view.rows.filter((row) => row.day === data.as_of_day),
        stats = summarize(today);
      const ratios = stats.denominator
        ? TONES.map(
            (key) => `${LABELS[key]} ${stats.percentages[key].toFixed(1)}%`,
          ).join(" · ")
        : "비율 산출 불가";
      dailySummary.replaceChildren(
        node("h2", `${data.as_of_day} 발행 기사`),
        node(
          "p",
          `${today.length}건 · 분류 완료 ${stats.denominator}건 · 판단 보류 ${stats.abstained}건`,
        ),
        node(
          "p",
          TONES.map((key) => `${LABELS[key]} ${stats.counts[key]}건`).join(
            " · ",
          ),
        ),
        node(
          "p",
          `${ratios}. 선택 조건의 당일 발행 기준이며 아래 분포는 선택한 전체 기간 기준입니다.`,
          "footnote",
        ),
      );
    }
    const publisherBody = get("publisher-rows");
    publisherBody.replaceChildren();
    for (const name of [
      ...new Set(view.rows.map((row) => row.publisher)),
    ].sort()) {
      const members = view.rows.filter((row) => row.publisher === name),
        stats = summarize(members);
      publisherBody.append(
        tableRow([
          name,
          members.length,
          stats.denominator,
          ...TONES.map((key) => stats.counts[key]),
          stats.abstained,
          stats.unrelated,
        ]),
      );
    }
    if (!view.rows.length)
      publisherBody.append(tableRow(["선택 범위에 확보된 기사 없음"]));
    const brief = get("briefing-lines");
    brief.replaceChildren();
    const issueContainer = get("issue-cards");
    issueContainer.replaceChildren();
    for (const [index, group] of view.groups.entries()) {
      if (index < 3)
        brief.append(
          node(
            "li",
            `${group.summary} (${group.publisher_count}개 매체·${group.members.length}건)`,
          ),
        );
      const card = node("article", undefined, "issue-card");
      card.append(
        node(
          "span",
          `${data.topics[group.topic]} · ${group.latest_day}`,
          "issue-topic",
        ),
        node("h3", group.label),
        node("p", group.summary),
      );
      const fresh = group.members.every(
        (row) => row.first_seen_day === data.as_of_day,
      );
      card.append(
        node(
          "p",
          `${group.members.length}건 · ${group.publisher_count}개 매체 · ${fresh ? "기준일에 처음 확인한 묶음" : "이전부터 확인한 보도 포함"}`,
          "footnote",
        ),
      );
      const details = node("details");
      details.append(node("summary", "제목 근거·관련 기사"));
      const list = node("ul");
      for (const evidence of group.evidence) {
        const row = group.members.find(
          (member) => member.id === evidence.article_id,
        );
        const item = node("li"),
          link = node(
            "a",
            `${row.publisher} · ${row.day}: “${evidence.quote}”`,
          );
        link.href = `#article-${row.id}`;
        item.append(link);
        list.append(item);
      }
      details.append(list);
      card.append(details);
      issueContainer.append(card);
    }
    if (!view.groups.length) {
      brief.append(
        node(
          "li",
          "선택 범위에서 핵심 이슈로 정리할 관련 기사를 확보하지 못했습니다.",
        ),
      );
      issueContainer.append(
        node("p", "보류·관련 없음은 이슈 묶음에서 제외합니다.", "empty"),
      );
    }
    set(
      "issue-count",
      `${view.groups.length}개 이슈 / ${view.rows.length}건 기사`,
    );
    const trend = get("trend-rows");
    trend.replaceChildren();
    for (const entry of [...view.daily].reverse()) {
      const values = entry.covered
        ? [
            entry.date,
            entry.article_count,
            ...TONES.map((key) => entry.statistics.counts[key]),
            entry.statistics.abstained,
          ]
        : [entry.date, "자료 범위 밖", "—", "—", "—", "—", "—"];
      trend.append(tableRow(values));
    }
  }
  for (const control of controls)
    control.addEventListener("change", applyFilters);
  applyFilters();
}

if (typeof module !== "undefined" && module.exports)
  module.exports = { percentages, summarize, buildView };
if (typeof document !== "undefined") initializeDashboard();
