import type { GenerateResult } from "../api-client";
import {
  AlertTriangleIcon,
  CheckCircleIcon,
  CheckIcon,
  ChevronDownIcon,
  ClockIcon,
  CopyIcon,
  DownloadIcon,
  RefreshIcon,
  ShareIcon,
} from "../icons";

type ResultPanelProps = {
  result: GenerateResult;
  status: string | null;
  copied: boolean;
  checking: boolean;
  autoUpdateDays: number | null;
  onDownload: () => void;
  onCopy: () => void;
  onCheck: () => void;
};

function intervalPhrase(days: number): string {
  return days === 1 ? "every day" : `every ${days} days`;
}

export function ResultPanel({
  result, status, copied, checking, autoUpdateDays, onDownload, onCopy, onCheck,
}: ResultPanelProps) {
  return (
    <section className="result-card" aria-label="Generated llms.txt">
      <div className="result-header">
        <span className="result-title">
          <CheckCircleIcon />
          Your llms.txt is ready
        </span>
        <div className="toolbar">
          <button type="button" className="btn-icon btn" onClick={onDownload}>
            <DownloadIcon width={15} height={15} />
            Download
          </button>
          <button type="button" className={`btn-icon btn${copied ? " is-active" : ""}`} onClick={onCopy}>
            {copied ? <CheckIcon width={15} height={15} /> : <CopyIcon width={15} height={15} />}
            {copied ? "Copied!" : "Copy"}
          </button>
          <button type="button" className="btn-icon btn" onClick={onCheck} disabled={checking}>
            <RefreshIcon width={15} height={15} className={checking ? "spinner" : undefined} />
            Re-check
          </button>
          {result.public_url && (
            <a
              className="btn-icon btn"
              href={result.public_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              <ShareIcon width={15} height={15} />
              Share link
            </a>
          )}
        </div>
      </div>

      {autoUpdateDays !== null && (
        <p className="auto-update-note">
          <ClockIcon width={15} height={15} />
          Auto-update enabled — this site will be re-checked {intervalPhrase(autoUpdateDays)} (nightly,
          midnight UTC), and the hosted link always serves the latest version.
        </p>
      )}

      {status && <p className="status-line">{status}</p>}

      {result.warnings.length > 0 && (
        <details className="warnings">
          <summary>
            <AlertTriangleIcon width={16} height={16} className="warn-icon" />
            {result.warnings.length} warning{result.warnings.length === 1 ? "" : "s"}
            <ChevronDownIcon width={16} height={16} className="chevron" />
          </summary>
          <ul>
            {result.warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </details>
      )}

      <div className="code-block">
        <pre>{result.llms_txt}</pre>
      </div>
    </section>
  );
}
