"use client";

import { useEffect, useRef } from "react";
import type { ProgressEvent } from "../api-client";

const STAGE_LABEL: Record<string, string> = {
  robots: "robots.txt",
  home: "homepage",
  discover: "discover",
  extract: "fetch",
  page: "page",
  twins: "md twins",
  rank: "rank",
  curate: "curate",
  format: "format",
  validate: "validate",
  persist: "store",
};

function shorten(url: string): string {
  try {
    const parsed = new URL(url);
    return parsed.pathname === "/" ? parsed.host : parsed.pathname;
  } catch {
    return url;
  }
}

function line(event: ProgressEvent): string {
  if (event.message) return event.message;
  if (!event.url) return "";
  return shorten(event.url);
}

function statusText(event: ProgressEvent): string {
  if (event.stage === "twins" && event.phase === "done" && event.ok === false) {
    return event.status === 200 ? "no twin" : String(event.status ?? "");
  }
  return event.status != null ? String(event.status) : "";
}

export function LogConsole({ events }: { events: ProgressEvent[] }) {
  const linesRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = linesRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [events.length]);

  const progress = [...events].reverse().find((event) => event.stage === "page");
  const inFlight =
    events.filter((event) => event.phase === "start").length -
    events.filter((event) => event.phase === "done" && event.stage !== "page").length;

  const done = Math.min(progress?.done ?? 0, progress?.total ?? 0);

  return (
    <div className="log-console">
      <div className="log-lines" ref={linesRef}>
        {events
          .filter((event) => event.stage !== "page")
          .map((event, index) => (
            <div key={index} className="log-line">
              <span className="log-stage">{STAGE_LABEL[event.stage] ?? event.stage}</span>
              <span className={`log-mark log-mark-${event.phase}`}>
                {event.phase === "start"
                  ? "→"
                  : event.phase === "info"
                    ? "·"
                    : event.ok === false
                      ? "✗"
                      : "✓"}
              </span>
              <span className="log-body">{line(event)}</span>
              <span className="log-status">
                {event.browser ? "browser " : ""}
                {statusText(event)}
              </span>
            </div>
          ))}
      </div>
      <div className="log-summary">
        <span>In flight: {Math.max(0, inFlight)}</span>
        <span>
          Done: {done}
          {progress?.total ? ` / ${progress.total}` : ""}
        </span>
      </div>
    </div>
  );
}
