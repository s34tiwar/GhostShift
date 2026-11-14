import type { ActivityState } from "@/lib/activity";
import type { FactoryMachine } from "@/lib/types";

const steps = [
  { title: "Fault detected", detail: "Machine health" },
  { title: "Inspect failure", detail: "Read current state" },
  { title: "Find a fix", detail: "Recovery procedure" },
  { title: "Check policy", detail: "Authorization" },
  { title: "Apply repair", detail: "Restart sync" },
  { title: "Verify result", detail: "Check machine health" },
] as const;

type IncidentState = "ready" | "faulted" | "recovering" | "recovered";

interface BackendActivityProps {
  states: ActivityState[];
  live: boolean;
  runStatus?: string;
  incidentState: IncidentState;
  machine?: FactoryMachine;
  busy: boolean;
  onSimulateError: () => void;
  onRecover: () => void;
}

const incidentCopy = {
  ready: {
    eyebrow: "Demo ready",
    title: "Machine B is syncing normally",
    detail: "Inject a real sync-process failure to begin the recovery story.",
  },
  faulted: {
    eyebrow: "Sync error detected",
    title: "Machine B stopped syncing",
    detail: "The fault is live. Backlog will keep rising until recovery runs.",
  },
  recovering: {
    eyebrow: "Recovery in progress",
    title: "GhostShift is repairing Machine B",
    detail: "Follow the live steps below while policy and verification complete.",
  },
  recovered: {
    eyebrow: "Effect verified",
    title: "Machine B is syncing again",
    detail: "The repair was verified and the backlog returned to normal.",
  },
} as const;

export default function BackendActivity({
  states,
  live,
  runStatus,
  incidentState,
  machine,
  busy,
  onSimulateError,
  onRecover,
}: BackendActivityProps) {
  const copy = incidentCopy[incidentState];
  const action = incidentState === "ready"
    ? <button className="activity-demo-button" onClick={onSimulateError} disabled={busy}>{busy ? "Injecting fault…" : "Simulate sync error"}</button>
    : incidentState === "faulted"
      ? <button className="activity-recover-button" onClick={onRecover} disabled={busy}>{busy ? "Starting recovery…" : "Recover Machine B"}</button>
      : <span className="incident-status">{incidentState === "recovering" ? runStatus ?? "Running" : "Verified"}</span>;

  return (
    <section className="activity-section" aria-labelledby="activity-title">
      <div className="activity-heading">
        <div><p className="section-number">02 / PROCESS</p><h2 id="activity-title">Backend activity</h2></div>
        <span className="preview-chip">{live ? runStatus ?? "Live API" : "Simulated preview"}</span>
      </div>
      <ol className="activity-steps">
        {steps.map((step, index) => {
          const state = states[index] ?? "pending";
          return <li className="activity-step-group" key={step.title}>
            <div className={`activity-step ${state}`} aria-current={state === "active" ? "step" : undefined}>
              <span className="activity-marker">{state === "done" ? "✓" : String(index + 1).padStart(2, "0")}</span>
              <div><strong>{step.title}</strong><small>{step.detail}</small></div>
              <span className="activity-state" aria-live={state === "active" || state === "blocked" ? "polite" : undefined}>{state === "done" ? "Done" : state === "active" ? "Active" : state === "blocked" ? "Needs review" : "Waiting"}</span>
            </div>
            {index === 0 && <div className={`activity-incident ${incidentState}`} aria-live="polite">
              <span className="incident-icon" aria-hidden="true">{incidentState === "faulted" ? "!" : incidentState === "recovered" ? "✓" : "↓"}</span>
              <div className="incident-copy">
                <small>{copy.eyebrow}</small>
                <strong>{copy.title}</strong>
                <p>{machine?.message ?? copy.detail}</p>
                {machine && incidentState !== "ready" && <span className="incident-metric">Backlog <b>{machine.backlog}</b> · Restarts <b>{machine.restart_count ?? 0}</b></span>}
              </div>
              {action}
            </div>}
          </li>;
        })}
      </ol>
    </section>
  );
}
