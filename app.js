"use strict";

const filters = ["publisher-filter", "sentiment-filter", "date-filter"].map(
  (id) => document.getElementById(id),
);
const rows = Array.from(document.querySelectorAll(".article"));
const counter = document.getElementById("visible-count");
const empty = document.getElementById("filter-empty");

function applyFilters() {
  const [publisher, sentiment, day] = filters.map((element) => element.value);
  let visible = 0;
  for (const row of rows) {
    const matches =
      (!publisher || row.dataset.publisher === publisher) &&
      (!sentiment || row.dataset.sentiment === sentiment) &&
      (!day || row.dataset.day === day);
    row.hidden = !matches;
    if (matches) visible += 1;
  }
  // No innerHTML: article strings are escaped server-side and never executed.
  if (counter) counter.textContent = `${visible} / ${rows.length}건`;
  if (empty) empty.hidden = visible !== 0 || rows.length === 0;
}

for (const element of filters) {
  if (element) element.addEventListener("change", applyFilters);
}
