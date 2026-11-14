"use client";

import { useEffect, useMemo, useState } from "react";
import type { ActivityState } from "@/lib/activity";
import type { FactoryMachine, RunView } from "@/lib/types";

const DEMO_STEP_MS = 2800;

const steps = [
  { title: "Fault detected", detail: "Machine health", narration: "The factory health check reports that Machine B stopped syncing and its backlog is beginning to rise." },
  { title: "Inspect failure", detail: "Read current state", narration: "The agent reads the live fault type, sync state, network status, and backlog before deciding what to do." },
  { title: "Find a fix", detail: "Recovery procedure", narration: "The planner searches the recovery knowledge base for a procedure that matches this exact failure." },
  { title: "Check policy", detail: "Authorization", narration: "A safety policy checks the proposed tool, target machine, and procedure before any action can run." },
  { title: "Apply repair", detail: "Restart sync", narration: "The executor applies the approved repair once, using a stable operation ID to prevent duplicate work." },
  { title: "Verify result", detail: "Check machine health", narration: "GhostShift asks the factory for fresh health data and completes only after the repair effect is verified." },
] as const;

type IncidentState = "ready" | "faulted" | "recovering" | "recovered";

interface BackendActivityProps {
  states: ActivityState[];
  live: boolean;
  run?: RunView | null;
  runStatus?: string;
  incidentState: IncidentState;
  machine?: FactoryMachine;
}

function targetStep(states: ActivityState[]) {
  if (states.every((state) => state === "done")) return steps.length;
  const blocked = states.findIndex((state) => state === "blocked");
  if (blocked >= 0) return blocked;
  const active = states.findIndex((state) => state === "active");
  if (active >= 0) return active;
  const lastDone = states.findLastIndex((state) => state === "done");
  return lastDone >= 0 ? Math.min(lastDone + 1, steps.length - 1) : -1;
}

function narratedStates(visibleStep: number, targetStates: ActivityState[]): ActivityState[] {
  if (visibleStep < 0) return Array(steps.length).fill("pending") as ActivityState[];
  if (visibleStep >= steps.length) return Array(steps.length).fill("done") as ActivityState[];
  const targetState = targetStates[visibleStep];
  return steps.map((_, index) => {
    if (index < visibleStep) return "done";
    if (index > visibleStep) return "pending";
    return targetState === "blocked" ? "blocked" : "active";
  });
}

function evidenceFor(step: number, machine: FactoryMachine | undefined, run: RunView | null | undefined) {
  if (step === 0) return [`Fault: ${machine?.fault_type ?? "SYNC_PROCESS_DOWN"}`, `Backlog: ${machine?.backlog ?? "rising"}`];
  if (step === 1) return [`Sync: ${machine?.sync_running === false ? "stopped" : "checking"}`, `Network: ${machine?.network_connected === false ? "offline" : "connected"}`];
  if (step === 2) return [run?.procedure_id ?? "Procedure search", run?.retrieval_mode ?? "Retrieval pending"];
  if (step === 3) return ["Allow-listed tools", "Target validated"];
  if (step === 4) return [run?.current_action_id ?? "Action pending", "Idempotent execution"];
  return [run?.worker_status ?? "Worker verifying", `Restarts: ${machine?.restart_count ?? 0}`];
}

function ActivityPresentation({ states, live, run, runStatus, incidentState, machine }: BackendActivityProps) {
  const actualTarget = targetStep(states);
  const [visibleStep, setVisibleStep] = useState(incidentState === "ready" ? -1 : incidentState === "faulted" ? 0 : 1);

  useEffect(() => {
    if (incidentState !== "recovering" && incidentState !== "recovered") return;
    if (visibleStep >= actualTarget) return;
    const timer = window.setTimeout(() => setVisibleStep((current) => Math.min(current + 1, actualTarget)), DEMO_STEP_MS);
    return () => window.clearTimeout(timer);
  }, [actualTarget, incidentState, visibleStep]);

  const displayStates = useMemo(() => narratedStates(visibleStep, states), [states, visibleStep]);
  const presentationComplete = incidentState === "recovered" && visibleStep >= steps.length;
  const waitingForRun = incidentState === "ready" && !run;
  const faultObserved = incidentState === "faulted" && !run;
  const spotlightIndex = Math.min(Math.max(visibleStep, 0), steps.length - 1);
  const spotlight = steps[spotlightIndex];
  const spotlightState = displayStates[spotlightIndex];
  const evidence = evidenceFor(spotlightIndex, machine, run);

  return <section className="activity-section" aria-labelledby="activity-title">
    <div className="activity-heading">
      <div><p className="section-number">02 / PROCESS</p><h2 id="activity-title">Backend activity</h2></div>
      <div className="activity-heading-meta"><span className="demo-pace"><i/>Demo pace · 2.8s per stage</span><span className="preview-chip">{live ? runStatus ?? "Live API" : "Simulated preview"}</span></div>
    </div>

    <div className="activity-console">
      <div className="activity-rail-wrap" aria-label="Recovery progress">
        <ol className="activity-rail">
          {steps.map((step, index) => {
            const state = displayStates[index] ?? "pending";
            return <li className={`rail-step ${state}`} key={step.title} aria-current={state === "active" ? "step" : undefined}>
              <span className="rail-node">{state === "done" ? "✓" : state === "blocked" ? "!" : String(index + 1).padStart(2, "0")}</span>
              <strong>{step.title}</strong><small>{step.detail}</small>
            </li>;
          })}
        </ol>
      </div>

      <div className={`activity-spotlight ${waitingForRun ? "pending" : spotlightState}`} key={`${spotlightIndex}-${spotlightState}`} role="status" aria-live="polite">
        <div className="spotlight-kicker"><span><i/>{waitingForRun ? "Demo controls ready" : faultObserved ? "Live fault observed" : "Now happening"}</span><b>{waitingForRun ? "Choose a scenario above" : `Step ${String(spotlightIndex + 1).padStart(2, "0")} / 06`}</b></div>
        <div className="spotlight-body">
          <span className="spotlight-icon" aria-hidden="true">{waitingForRun ? "◇" : spotlightState === "blocked" ? "!" : presentationComplete ? "✓" : spotlightIndex + 1}</span>
          <div><small>{waitingForRun ? "Machine B demo" : presentationComplete ? "Recovery complete" : spotlight.detail}</small><h3>{waitingForRun ? "Choose a demo scenario" : presentationComplete ? "Machine B is syncing again" : spotlight.title}</h3><p>{waitingForRun ? "Use the Machine B controls above to break sync, run a normal recovery, arm a deliberate crash, or prove an unsafe action is denied." : presentationComplete ? "The repair effect is verified, the run is complete, and Machine B is healthy." : spotlight.narration}</p></div>
        </div>
        {!waitingForRun && <div className="spotlight-evidence">{evidence.map((item) => <span key={item}>{item}</span>)}</div>}
      </div>
    </div>
  </section>;
}

export default function BackendActivity(props: BackendActivityProps) {
  const presentationKey = props.run?.run_id ?? (props.incidentState === "ready" || props.incidentState === "faulted" ? props.incidentState : "preview-run");
  return <ActivityPresentation key={presentationKey} {...props}/>;
}
