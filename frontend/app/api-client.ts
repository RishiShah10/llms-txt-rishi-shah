const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const WS_BASE = BASE.replace(/^http/, "ws");

export type ProgressEvent = {
  stage: string;
  phase: "start" | "done" | "info";
  url?: string | null;
  status?: number | null;
  ok?: boolean | null;
  done?: number | null;
  total?: number | null;
  message?: string | null;
  browser?: boolean | null;
};

export type GenerateResult = {
  llms_txt: string;
  pages: { url: string; title: string; description: string | null }[];
  warnings: string[];
  public_url: string | null;
};

export type ChangeResult = {
  changed: boolean;
  regenerated_llms_txt: string | null;
  public_url: string | null;
  details: string;
};

async function post<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}));
    throw new Error(errorBody.detail ?? "Request failed");
  }
  return response.json();
}

export const generate = (
  url: string,
  maxPages: number | null,
  crawl: boolean,
  enhance: boolean,
  bypass: boolean,
  honorRobots: boolean,
  autoUpdate: boolean,
  recrawlIntervalDays: number,
) =>
  post<GenerateResult>("/generate", {
    url, max_pages: maxPages, crawl, enhance, bypass, honor_robots: honorRobots,
    auto_update: autoUpdate, recrawl_interval_days: recrawlIntervalDays,
  });
export const checkChanges = (url: string) => post<ChangeResult>("/check-changes", { url });

const CLOSE_REASONS: Record<number, string> = {
  1008: "This origin is not allowed to use the generator.",
  1013: "The generator is busy — please try again in a moment.",
};
const DEFAULT_CLOSE_REASON = "Connection closed before the generation finished";

export function generateStream(
  url: string,
  maxPages: number | null,
  crawl: boolean,
  enhance: boolean,
  bypass: boolean,
  honorRobots: boolean,
  autoUpdate: boolean,
  recrawlIntervalDays: number,
  onEvent: (event: ProgressEvent) => void,
): Promise<GenerateResult> {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(`${WS_BASE}/ws/generate`);
    let settled = false;

    const settle = (action: () => void) => {
      if (settled) return;
      settled = true;
      action();
      socket.close();
    };

    socket.onopen = () =>
      socket.send(
        JSON.stringify({
          url, max_pages: maxPages, crawl, enhance, bypass, honor_robots: honorRobots,
          auto_update: autoUpdate, recrawl_interval_days: recrawlIntervalDays,
        }),
      );

    socket.onmessage = (message) => {
      let frame: Record<string, unknown>;
      try {
        frame = JSON.parse(message.data);
      } catch {
        settle(() => reject(new Error("Malformed response from the generator")));
        return;
      }
      if (frame.type === "event") {
        onEvent(frame as ProgressEvent);
      } else if (frame.type === "done") {
        settle(() => resolve(frame.result as GenerateResult));
      } else {
        settle(() => reject(new Error((frame.detail as string) ?? "Generation failed")));
      }
    };

    socket.onerror = () => settle(() => reject(new Error("Could not connect to the generator")));

    socket.onclose = (event) =>
      settle(() => reject(new Error(CLOSE_REASONS[event.code] ?? DEFAULT_CLOSE_REASON)));
  });
}
