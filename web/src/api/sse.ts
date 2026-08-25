import type { ServerEvent } from "./types";

export type ConnectionState = "connected" | "reconnecting";

function parseFrame(raw: string): ServerEvent | null {
  let eventId: string | null = null;
  let eventName: string | null = null;
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith(":")) continue;
    const idx = line.indexOf(":");
    const field = idx === -1 ? line : line.slice(0, idx);
    let value = idx === -1 ? "" : line.slice(idx + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") eventId = value;
    else if (field === "event") eventName = value;
    else if (field === "data") dataLines.push(value);
  }
  if (!dataLines.length || eventId === null) return null;
  const data = JSON.parse(dataLines.join("\n")) as {
    event_type?: string;
    payload?: Record<string, unknown>;
    task_id?: string | null;
    decision_round?: number | null;
    created_at?: string | null;
  };
  const eventType = String(data.event_type || eventName || "");
  if (!eventType) return null;
  return {
    id: Number(eventId),
    event_type: eventType,
    payload: data.payload ?? {},
    task_id: data.task_id == null ? null : String(data.task_id),
    decision_round: data.decision_round == null ? null : Number(data.decision_round),
    created_at: data.created_at == null ? null : String(data.created_at),
  };
}

export function subscribeJobEvents(
  jobId: string,
  handlers: {
    onEvent: (event: ServerEvent) => void;
    onStatus?: (state: ConnectionState, delay?: number) => void;
  },
): () => void {
  const controller = new AbortController();
  let lastEventId: number | null = null;
  let delay = 1000;
  let stopped = false;

  const run = async () => {
    while (!stopped) {
      try {
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        if (lastEventId !== null) headers["Last-Event-ID"] = String(lastEventId);
        const response = await fetch(`/api/jobs/${jobId}/events`, {
          headers,
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error(`sse ${response.status}`);
        handlers.onStatus?.("connected");
        delay = 1000;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!stopped) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            if (!frame.trim() || frame.startsWith(":")) continue;
            const event = parseFrame(frame);
            if (!event) continue;
            lastEventId = event.id;
            handlers.onEvent(event);
            if (event.event_type === "job.stopped") {
              stopped = true;
              return;
            }
          }
        }
      } catch (error) {
        if (stopped || controller.signal.aborted) return;
        handlers.onStatus?.("reconnecting", delay / 1000);
        await new Promise((resolve) => setTimeout(resolve, delay));
        delay = Math.min(delay * 2, 30_000);
        if (error instanceof Error && error.name === "AbortError") return;
      }
    }
  };

  void run();
  return () => {
    stopped = true;
    controller.abort();
  };
}
