import assert from "node:assert/strict";
import test from "node:test";
import { subscribeJobEvents } from "../src/api/sse.ts";
import type { ServerEvent } from "../src/api/types.ts";

const frame = (id: number, type: string, payload = {}) =>
  `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify({ event_type: type, payload: { text: "中文进度", ...payload } })}\n\n`;

function response(text: string) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({
    start(controller) {
      // Deliberately split framing and multi-byte UTF-8 across transport chunks.
      for (let i = 0; i < bytes.length; i += 7) controller.enqueue(bytes.slice(i, i + 7));
      controller.close();
    },
  }));
}

test("报告核验失败不终止事件消费，只有 job.stopped 结束订阅", { timeout: 5000 }, async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => response(
    ": heartbeat\n\n" + frame(1, "job.phase_changed", { phase: "report_failed" }) +
    frame(2, "report.draft_rendered") + frame(3, "job.stopped"),
  ));
  const events: ServerEvent[] = [];
  let done!: () => void;
  const finished = new Promise<void>(resolve => { done = resolve; });
  const stop = subscribeJobEvents("a", { onEvent(event) {
    events.push(event);
    if (event.event_type === "job.stopped") done();
  } });
  t.after(stop);
  await finished;
  assert.deepEqual(events.map(event => event.id), [1, 2, 3]);
  assert.equal(events[0].payload.text, "中文进度");
  assert.equal(fetch.mock.callCount(), 1);
});

test("断线后仅续传完整收到的事件，未完成的帧从服务端重新读取", { timeout: 5000 }, async t => {
  const headers: Headers[] = [];
  t.mock.method(globalThis, "fetch", async (_url: unknown, init: RequestInit) => {
    headers.push(new Headers(init.headers));
    assert.ok(headers.length <= 2, "Terminal event must stop reconnects");
    return headers.length === 1
      ? response(frame(7, "task.finished") + "id: 8\nevent: job.stopped\ndata: {")
      : response(frame(8, "job.stopped"));
  });
  const ids: number[] = [];
  let done!: () => void;
  const finished = new Promise<void>(resolve => { done = resolve; });
  const stop = subscribeJobEvents("a", { onEvent(event) {
    ids.push(event.id);
    if (event.event_type === "job.stopped") done();
  } });
  t.after(stop);
  await finished;
  assert.deepEqual(ids, [7, 8]);
  assert.equal(headers[0].get("Last-Event-ID"), null);
  assert.equal(headers[1].get("Last-Event-ID"), "7");
});

test("离开页面会取消正在等待的订阅请求", async t => {
  let signal: AbortSignal | undefined;
  const fetch = t.mock.method(globalThis, "fetch", (_url: unknown, init: RequestInit) => {
    signal = init.signal!;
    return new Promise((_resolve, reject) => signal!.addEventListener("abort", () => {
      reject(new DOMException("aborted", "AbortError"));
    }));
  });
  const stop = subscribeJobEvents("a", { onEvent() { assert.fail("No event was sent"); } });
  stop();
  assert.equal(signal?.aborted, true);
  assert.equal(fetch.mock.callCount(), 1);
});
