import assert from "node:assert/strict";
import test from "node:test";

import {
  appendTimelineEntries,
  filterTimelineEntries,
  timelineClock,
  type TimelineDisplayEntry,
} from "../src/state/timelineDisplay.ts";

function entry(index: number, cls: TimelineDisplayEntry["cls"] = ""): TimelineDisplayEntry {
  return {
    eventId: index,
    createdAt: `15:03:${String(index % 60).padStart(2, "0")}`,
    tag: `T${index}`,
    text: `事件 ${index}`,
    cls,
  };
}

test("all timeline entries remain available after the former 80-row boundary", () => {
  const rows = Array.from({ length: 81 }, (_, index) => entry(index));
  assert.equal(appendTimelineEntries([], rows).length, 81);
  assert.equal(appendTimelineEntries([], rows)[0]?.text, "事件 0");
});

test("key view hides only high-frequency tool and round entries", () => {
  const rows = [
    entry(1, "tool"),
    entry(2, "round"),
    entry(3, "evidence"),
    entry(4, "gap"),
    entry(5, "planner"),
    entry(6, "phase"),
    entry(7, "done"),
    entry(8, ""),
  ];
  assert.deepEqual(
    filterTimelineEntries(rows, "key").map((row) => row.text),
    ["事件 3", "事件 4", "事件 5", "事件 6", "事件 7", "事件 8"],
  );
  assert.equal(filterTimelineEntries(rows, "all").length, 8);
});

test("every row receives a complete 24-hour timestamp", () => {
  assert.equal(timelineClock("2026-08-26T15:03:25"), "15:03:25");
});
