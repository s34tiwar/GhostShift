const steps = [
  { title: "Fault detected", detail: "Machine health" },
  { title: "Inspect failure", detail: "Read current state" },
  { title: "Find a fix", detail: "Recovery procedure" },
  { title: "Check policy", detail: "Authorization" },
  { title: "Apply repair", detail: "Restart sync" },
  { title: "Verify result", detail: "Check machine health" },
] as const;

export default function BackendActivity({ phase, activeStep }: { phase: "idle" | "faulted" | "running" | "complete"; activeStep: number }) {
  const progress = phase === "idle" ? -1 : phase === "faulted" ? 0 : phase === "complete" ? steps.length : activeStep;

  return (
    <section className="activity-section" aria-labelledby="activity-title">
      <div className="activity-heading">
        <div><p className="section-number">02 / PROCESS</p><h2 id="activity-title">Backend activity</h2></div>
        <span className="preview-chip">Simulated preview</span>
      </div>
      <ol className="activity-steps">
        {steps.map((step, index) => {
          const state = progress > index ? "done" : progress === index ? (phase === "faulted" ? "done" : "active") : "pending";
          return <li className={`activity-step ${state}`} key={step.title}>
            <span className="activity-marker">{state === "done" ? "✓" : String(index + 1).padStart(2, "0")}</span>
            <div><strong>{step.title}</strong><small>{step.detail}</small></div>
            <span className="activity-state">{state === "done" ? "Done" : state === "active" ? "Running" : "Waiting"}</span>
          </li>;
        })}
      </ol>
    </section>
  );
}
