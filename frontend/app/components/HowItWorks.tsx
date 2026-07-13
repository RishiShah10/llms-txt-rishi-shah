import { DownloadIcon, GlobeIcon, LinkIcon } from "../icons";

const STEPS = [
  {
    icon: <LinkIcon />,
    title: "Paste your URL",
    body: "Drop in your website's homepage — no setup or account needed.",
  },
  {
    icon: <GlobeIcon />,
    title: "We crawl & summarize",
    body: "We visit your pages and write a short, clear description of each one.",
  },
  {
    icon: <DownloadIcon />,
    title: "Download your llms.txt",
    body: "Get a spec-compliant file ready to drop at the root of your site.",
  },
];

export function HowItWorks() {
  return (
    <div className="how-it-works">
      <span className="section-label">How it works</span>
      <div className="steps">
        {STEPS.map((step, index) => (
          <div className="step" key={step.title}>
            <span className="step-number">{index + 1}</span>
            <span className="toggle-icon">{step.icon}</span>
            <strong>{step.title}</strong>
            <p>{step.body}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
