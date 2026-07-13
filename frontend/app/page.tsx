"use client";

import { useState, type CSSProperties } from "react";
import { generateStream, checkChanges, GenerateResult, ProgressEvent } from "./api-client";
import { OptionToggle } from "./components/Switch";
import { ResultPanel } from "./components/ResultPanel";
import { HowItWorks } from "./components/HowItWorks";
import { Footer } from "./components/Footer";
import { LogConsole } from "./components/LogConsole";
import {
  AlertTriangleIcon,
  ClockIcon,
  GlobeIcon,
  InfinityIcon,
  LinkIcon,
  LoaderIcon,
  RobotIcon,
  ShieldIcon,
  SparklesIcon,
} from "./icons";

const MIN_PAGES = 10;
const MAX_PAGES = 200;

const RECRAWL_CHOICES = [
  { days: 1, label: "Daily" },
  { days: 3, label: "Every 3 days" },
  { days: 7, label: "Weekly" },
  { days: 14, label: "Every 2 weeks" },
  { days: 30, label: "Monthly" },
];

export default function Home() {
  const [url, setUrl] = useState("");
  const [result, setResult] = useState<GenerateResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [maxPages, setMaxPages] = useState(50);
  const [noLimit, setNoLimit] = useState(false);
  const [crawl, setCrawl] = useState(true);
  const [enhance, setEnhance] = useState(false);
  const [bypass, setBypass] = useState(false);
  const [honorRobots, setHonorRobots] = useState(true);
  const [autoUpdate, setAutoUpdate] = useState(false);
  const [recrawlDays, setRecrawlDays] = useState(7);
  const [enrolledDays, setEnrolledDays] = useState<number | null>(null);
  const [copied, setCopied] = useState(false);
  const [events, setEvents] = useState<ProgressEvent[]>([]);

  const checking = status === "Checking…";

  async function onGenerate() {
    setLoading(true);
    setError(null);
    setStatus(null);
    setEvents([]);
    try {
      setResult(
        await generateStream(
          url, noLimit ? null : maxPages, crawl, enhance, bypass, honorRobots,
          autoUpdate, recrawlDays,
          (event) => setEvents((current) => [...current, event]),
        ),
      );
      setEnrolledDays(autoUpdate ? recrawlDays : null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }

  async function onCheck() {
    setStatus("Checking…");
    try {
      const change = await checkChanges(url);
      setStatus(change.details);
      if (change.changed && change.regenerated_llms_txt && result) {
        setResult({ ...result, llms_txt: change.regenerated_llms_txt });
      }
    } catch (err) {
      setStatus((err as Error).message);
    }
  }

  function onDownload() {
    if (!result) return;
    const blob = new Blob([result.llms_txt], { type: "text/plain" });
    const anchor = document.createElement("a");
    anchor.href = URL.createObjectURL(blob);
    anchor.download = "llms.txt";
    anchor.click();
  }

  async function onCopy() {
    if (!result) return;
    await navigator.clipboard.writeText(result.llms_txt);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  }

  const sliderPercent = ((maxPages - MIN_PAGES) / (MAX_PAGES - MIN_PAGES)) * 100;
  const sliderStyle = { "--fill": `${sliderPercent}%` } as CSSProperties;

  return (
    <main className="page">
      <div className="shell">
        <div className="hero">
          <span className="brand">llms.txt Generator</span>
          <h1>Give AI a map of your website</h1>
          <p>
            Paste a URL and get a spec-compliant <code>llms.txt</code> — a curated, LLM-friendly guide to
            your site&apos;s pages — in under a minute.
          </p>
        </div>

        <div className="generate-card">
          <div className="url-row">
            <div className="url-field">
              <LinkIcon width={17} height={17} />
              <input
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                placeholder="https://example.com"
                aria-label="Website URL"
                type="url"
                inputMode="url"
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !loading && url) onGenerate();
                }}
              />
            </div>
            <button className="btn btn-primary" onClick={onGenerate} disabled={loading || !url}>
              {loading && <LoaderIcon width={16} height={16} />}
              {loading ? "Generating…" : "Generate"}
            </button>
          </div>
          <div className="generate-hint">
            {loading
              ? "Crawling pages and writing your llms.txt — this usually takes 10–60 seconds."
              : "Works on most public websites. Generation typically takes 10–60 seconds."}
          </div>
          {events.length > 0 && <LogConsole events={events} />}
        </div>

        <div className="options-card">
          <span className="section-label">Options</span>
          <div className="slider-block">
            <span className="section-label section-label-sub">Crawl depth</span>
            <div className="slider-row">
              <span className="slider-row-label">
                <strong>Pages to crawl</strong>
                <span>Stop after visiting this many pages.</span>
              </span>
              <input
                id="max-pages"
                type="range"
                min={MIN_PAGES}
                max={MAX_PAGES}
                step={10}
                value={maxPages}
                disabled={noLimit}
                style={sliderStyle}
                onChange={(event) => setMaxPages(Number(event.target.value))}
                aria-label="Maximum pages to crawl"
              />
              <span
                className={`slider-value${noLimit ? " is-muted" : ""}`}
                aria-label={noLimit ? "No page limit" : undefined}
              >
                {noLimit ? <InfinityIcon width={20} height={20} strokeWidth={1.75} /> : maxPages}
              </span>
            </div>
            <OptionToggle
              id="no-limit"
              checked={noLimit}
              onChange={setNoLimit}
              icon={<GlobeIcon width={16} height={16} />}
              title="No limit"
              description="Crawl the entire site, however large — ignores the slider above."
            />
          </div>

          <div className="toggle-list">
            <OptionToggle
              id="crawl"
              checked={crawl}
              onChange={setCrawl}
              icon={<LinkIcon width={16} height={16} />}
              title="Crawl linked pages"
              description="Follow links from your homepage to discover more pages (BFS)."
            />
            <OptionToggle
              id="enhance"
              checked={enhance}
              onChange={setEnhance}
              icon={<SparklesIcon width={16} height={16} />}
              title="AI-enhanced descriptions"
              description="Use an LLM via OpenRouter to write richer page summaries."
            />
            <OptionToggle
              id="bypass"
              checked={bypass}
              onChange={setBypass}
              icon={<ShieldIcon width={16} height={16} />}
              title="Unblock protected sites"
              description="Escalate to a real browser when a page blocks simple requests."
            />
            <OptionToggle
              id="honor-robots"
              checked={honorRobots}
              onChange={setHonorRobots}
              icon={<RobotIcon width={16} height={16} />}
              title="Honor robots.txt"
              description="Respect the site's crawler rules (recommended)."
            />
            <OptionToggle
              id="auto-update"
              checked={autoUpdate}
              onChange={setAutoUpdate}
              icon={<ClockIcon width={16} height={16} />}
              title="Auto-update"
              description="Re-check this site on a schedule and refresh the hosted file when its content changes. Checks run nightly at midnight UTC. Leave off to generate once."
            />
            {autoUpdate && (
              <div className="toggle-sub-row">
                <label htmlFor="recrawl-interval">Re-check frequency</label>
                <select
                  id="recrawl-interval"
                  value={recrawlDays}
                  onChange={(event) => setRecrawlDays(Number(event.target.value))}
                >
                  {RECRAWL_CHOICES.map((choice) => (
                    <option key={choice.days} value={choice.days}>
                      {choice.label}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>
        </div>

        {error && (
          <div className="callout callout-danger" role="alert">
            <AlertTriangleIcon />
            <div className="callout-body">
              <div className="callout-title">Something went wrong</div>
              {error}
            </div>
          </div>
        )}

        {result ? (
          <div className="output-block">
            <span className="section-label">Output</span>
            <ResultPanel
              result={result}
              status={status}
              copied={copied}
              checking={checking}
              autoUpdateDays={enrolledDays}
              onDownload={onDownload}
              onCopy={onCopy}
              onCheck={onCheck}
            />
          </div>
        ) : (
          !loading && !error && <HowItWorks />
        )}

        <Footer />
      </div>
    </main>
  );
}
