import type { ActivityState } from "@/lib/activity";

const steps = [
  { title: "Fault detected", detail: "Machine health" },
  { title: "Inspect failure", detail: "Read current state" },
  { title: "Find a fix", detail: "Recovery procedure" },
  { title: "Check policy", detail: "Authorization" },
  { title: "Apply repair", detail: "Restart sync" },
  { title: "Verify result", detail: "Check machine health" },
] as const;

export default function BackendActivity({ states, live, runStatus }: { states: ActivityState[]; live: boolean; runStatus?: string }) {
  return (
    <section className="activity-section" aria-labelledby="activity-title">
      <div className="activity-heading">
        <div><p className="section-number">02 / PROCESS</p><h2 id="activity-title">Backend activity</h2></div>
        <span className="preview-chip">{live ? runStatus ?? "Live API" : "Simulated preview"}</span>
      </div>
      <ol className="activity-steps">
        {steps.map((step, index) => {
          const state = states[index] ?? "pending";
          return <li className={`activity-step ${state}`} key={step.title} aria-current={state === "active" ? "step" : undefined}>
            <span className="activity-marker">{state === "done" ? "✓" : String(index + 1).padStart(2, "0")}</span>
            <div><strong>{step.title}</strong><small>{step.detail}</small></div>
            <span className="activity-state" aria-live={state === "active" || state === "blocked" ? "polite" : undefined}>{state === "done" ? "Done" : state === "active" ? "Active" : state === "blocked" ? "Needs review" : "Waiting"}</span>
          </li>;
        })}
      </ol>
    </section>
  );
}
