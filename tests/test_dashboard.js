"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { percentages, summarize, buildView } = require("../web/app.js");

const rows = [
  {
    id: "a",
    title: "가상 생산 확대",
    publisher: "a.example",
    day: "2026-10-16",
    relevance: "related",
    sentiment: "positive",
    first_seen_day: "2026-10-19",
    reused: false,
  },
  {
    id: "b",
    title: "가상 공장 준공",
    publisher: "b.example",
    day: "2026-10-16",
    relevance: "related",
    sentiment: "neutral",
    first_seen_day: "2026-10-18",
    reused: true,
  },
  {
    id: "c",
    title: "가상 지분 매입",
    publisher: "c.example",
    day: "2026-10-06",
    relevance: "related",
    sentiment: "positive",
    first_seen_day: "2026-10-18",
    reused: false,
  },
];
const data = {
  as_of_day: "2026-10-19",
  available_start: "2026-10-06",
  articles: rows,
  groups: [
    {
      label: "생산",
      summary: "전체 묶음 요약",
      topic: "management",
      article_ids: ["a", "b"],
      evidence: [],
    },
    {
      label: "지분",
      summary: "지분 제목 요약",
      topic: "governance",
      article_ids: ["c"],
      evidence: [],
    },
  ],
};

test("largest remainder exactly matches Python rounding", () => {
  assert.deepEqual(
    percentages({ positive: 1, negative: 1, neutral: 1, mixed: 0 }),
    { positive: 33.4, negative: 33.3, neutral: 33.3, mixed: 0 },
  );
  assert.equal(summarize([]).percentages.positive, null);
});
test("seven-day scope updates articles, issue count and statistics together", () => {
  const view = buildView(data, { days: 7 });
  assert.equal(view.rows.length, 2);
  assert.equal(view.groups.length, 1);
  assert.equal(view.statistics.denominator, 2);
  assert.equal(view.statistics.percentages.positive, 50);
});
test("publisher filter uses only selected source headline in a partial issue", () => {
  const view = buildView(data, { days: 30, publisher: "a.example" });
  assert.equal(view.rows.length, 1);
  assert.equal(view.statistics.percentages.positive, 100);
  assert.equal(view.groups[0].summary, "선택된 기사 제목: 가상 생산 확대");
});
test("today with no publications does not display a zero-percent conclusion", () => {
  const view = buildView(data, { days: 1 });
  assert.equal(view.rows.length, 0);
  assert.equal(view.statistics.percentages.positive, null);
});
test("dates before actual coverage are unknown, not zero", () => {
  const view = buildView(data, { days: 30 });
  assert.equal(view.coverage_complete, false);
  assert.equal(view.daily[0].article_count, null);
  assert.equal(view.daily.at(-1).article_count, 0);
});
test("topic and date filters affect every aggregate", () => {
  assert.equal(
    buildView(data, { days: 30, topic: "governance" }).rows.length,
    1,
  );
  assert.equal(
    buildView(data, { days: 30, day: "2026-10-06" }).groups.length,
    1,
  );
  assert.equal(
    buildView(data, { days: 30, sentiment: "negative" }).statistics.denominator,
    0,
  );
});
test("unrelated and uncertain articles never enter the tone denominator", () => {
  const value = summarize([
    ...rows,
    { relevance: "unrelated", sentiment: "abstain" },
    { relevance: "uncertain", sentiment: "abstain" },
  ]);
  assert.equal(value.denominator, 3);
  assert.equal(value.unrelated, 1);
  assert.equal(value.abstained, 1);
});
