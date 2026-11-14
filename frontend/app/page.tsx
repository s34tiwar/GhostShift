"use client";

import { useEffect, useState } from "react";
import BackendActivity from "@/components/BackendActivity";
import ConnectionMap from "@/components/ConnectionMap";
import FactoryCards from "@/components/FactoryCards";
import { api, hasBackend } from "@/lib/api";
import { liveSteps, previewSteps, toDisplayMachines, type PreviewPhase } from "@/lib/activity";
import type { AuditEvent, FactoryMachine, LedgerEntry, Machine, RunView } from "@/lib/types";

const target = "machine_b" as const;
const runKey = "ghostshift.activeRunId";
const initialMachines: Machine[] = [
  { machine_id: "machine_a", name: "Machine A", status: "healthy", records_synced: 12408, backlog: 0, fault_epoch: 0, restart_count: 0 },
  { machine_id: "machine_b", name: "Machine B", status: "healthy", records_synced: 12384, backlog: 0, fault_epoch: 0, restart_count: 0 },
  { machine_id: "machine_c", name: "Machine C", status: "healthy", records_synced: 12396, backlog: 0, fault_epoch: 0, restart_count: 0 },
];

function GhostMark() {
  return <svg viewBox="0 0 32 32" aria-hidden="true"><path d="M7 25V14a9 9 0 0 1 18 0v11l-3-2.2L19 25l-3-2.2L13 25l-3-2.2L7 25Z"/><circle cx="13" cy="14" r="1.3"/><circle cx="20" cy="14" r="1.3"/></svg>;
}

export default function Home() {
  const [previewMachines, setPreviewMachines] = useState(initialMachines);
  const [previewPhase, setPreviewPhase] = useState<PreviewPhase>("idle");
  const [previewStep, setPreviewStep] = useState(-1);
  const [factoryMachines, setFactoryMachines] = useState<FactoryMachine[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<RunView | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [actions, setActions] = useState<LedgerEntry[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!hasBackend) return;
    const timer = window.setTimeout(() => setRunId(window.localStorage.getItem(runKey)), 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (hasBackend) return;
    const timer = window.setInterval(() => {
      setPreviewMachines((current) => current.map((machine) =>
        machine.status === "failed" || machine.status === "recovering"
          ? { ...machine, backlog: machine.backlog + 2 }
          : { ...machine, records_synced: (machine.records_synced ?? 0) + 4 },
      ));
    }, 1200);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (hasBackend || previewPhase !== "running") return;
    const timer = window.setTimeout(() => {
      if (previewStep === 3) {
        setPreviewMachines((current) => current.map((machine) => machine.machine_id === target
          ? { ...machine, status: "recovering", restart_count: machine.restart_count + 1 }
          : machine));
      }
      if (previewStep === 5) {
        setPreviewMachines((current) => current.map((machine) => machine.machine_id === target
          ? { ...machine, status: "reconciled", backlog: 0 }
          : machine));
        setPreviewPhase("complete");
      } else {
        setPreviewStep((step) => step + 1);
      }
    }, 1100);
    return () => window.clearTimeout(timer);
  }, [previewPhase, previewStep]);

  useEffect(() => {
    if (!hasBackend) return;
    let cancelled = false;
    let polling = false;
    async function poll() {
      if (polling) return;
      polling = true;
      try {
        const [machines, snapshot] = await Promise.all([
          api.machines(),
          runId ? Promise.all([api.run(runId), api.audit(runId), api.actions(runId)]) : Promise.resolve(null),
        ]);
        if (cancelled) return;
        setFactoryMachines(machines);
        if (snapshot) {
          setRun(snapshot[0]);
          setAudit(snapshot[1].events);
          setActions(snapshot[2]);
        }
        setError(null);
        setLoaded(true);
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "Backend unavailable");
          setLoaded(true);
        }
      } finally {
        polling = false;
      }
    }
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [runId]);

  async function stopSync() {
    if (!hasBackend) {
      setPreviewMachines((current) => current.map((machine) => machine.machine_id === target
        ? { ...machine, status: "failed", fault_epoch: machine.fault_epoch + 1 }
        : machine));
      setPreviewPhase("faulted");
      setPreviewStep(-1);
      return;
    }
    setBusy(true);
    try {
      await api.stopSync(target);
      window.localStorage.removeItem(runKey);
      setRunId(null);
      setRun(null);
      setAudit([]);
      setActions([]);
      setFactoryMachines(await api.machines());
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not stop sync");
    } finally { setBusy(false); }
  }

  async function runRecovery() {
    if (!hasBackend) {
      setPreviewPhase("running");
      setPreviewStep(1);
      return;
    }
    setBusy(true);
    try {
      const created = await api.createRun(target);
      window.localStorage.setItem(runKey, created.run_id);
      setRunId(created.run_id);
      setRun({ run_id: created.run_id, machine_id: target, status: created.status, procedure_id: null, retrieval_mode: null, current_action_id: null, worker_status: null });
      setAudit([]);
      setActions([]);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not start recovery");
    } finally { setBusy(false); }
  }

  async function resetDemo() {
    if (!hasBackend) {
      setPreviewMachines(initialMachines);
      setPreviewPhase("idle");
      setPreviewStep(-1);
      return;
    }
    setBusy(true);
    try {
      await api.reset();
      window.localStorage.removeItem(runKey);
      setRunId(null);
      setRun(null);
      setAudit([]);
      setActions([]);
      setFactoryMachines(await api.machines());
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not reset factory");
    } finally { setBusy(false); }
  }

  const targetMachine = factoryMachines.find((machine) => machine.machine_id === target);
  const machines = hasBackend ? toDisplayMachines(factoryMachines, run, actions) : previewMachines;
  const activity = hasBackend ? liveSteps(targetMachine, run, audit, actions) : previewSteps(previewPhase, previewStep);
  const activeRun = hasBackend && run !== null && !["COMPLETED", "FAILED", "DENIED"].includes(run.status);
  const faulted = hasBackend ? Boolean(targetMachine && targetMachine.fault_type !== "NONE") : previewPhase === "faulted";
  const recovered = hasBackend
    ? Boolean(run?.status === "COMPLETED" && targetMachine?.healthy && actions.some((action) => action.status === "EFFECT_VERIFIED"))
    : previewPhase === "complete";
  const incidentState = recovered ? "recovered" : activeRun || previewPhase === "running" ? "recovering" : faulted ? "faulted" : "ready";
  const statusText = hasBackend && !loaded ? "Connecting to backend"
    : hasBackend && machines.length === 0 ? "Backend unavailable"
    : hasBackend && run?.status === "COMPLETED" && machines[1]?.status === "reconciled" ? "Recovery verified"
    : hasBackend && run && ["FAILED", "DENIED", "COMPLETED"].includes(run.status) ? "Review required"
    : activeRun || previewPhase === "running" && !hasBackend ? "Recovery in progress"
    : faulted ? "Action required" : "All systems operational";

  return <main>
    <header className="site-header"><a className="brand" href="#top"><span><GhostMark/></span>GhostShift</a><div className="header-meta"><span className="mock-badge">{hasBackend ? "Live backend" : "Mock backend"}</span><span className="live"><i/>{hasBackend ? "Polling live" : "Simulation running"}</span></div></header>
    <div className="shell" id="top">
      <section className="hero"><div><p className="kicker"><span/> Factory recovery</p><h1>Keep the shift<br/><em>moving.</em></h1></div><aside className="hero-proof"><p>System state</p><strong>{statusText}</strong><div className="proof-row"><span><i/>{hasBackend ? machines.length : 3} machines</span><span>{hasBackend ? "Live API" : "Preview"}</span></div></aside></section>
      <section className="factory-section"><div className="section-heading"><div><p className="section-number">01 / FACTORY</p><h2>Machines</h2></div><span className="event-count"><i/>{hasBackend ? "Live state" : "Connection preview"}</span></div>
        {error && <div className="backend-error" role="alert">{error}</div>}
        {hasBackend && machines.length !== 3 ? <div className="machine-loading">{loaded ? "Waiting for backend" : "Loading machines"}</div> : <><FactoryCards machines={machines}/><ConnectionMap machines={machines}/></>}
        <div className="factory-actions" aria-label="Machine B controls"><span>Machine B</span><button className="button danger" onClick={stopSync} disabled={busy || Boolean(activeRun) || (hasBackend ? !targetMachine || targetMachine.fault_type !== "NONE" : previewPhase === "faulted" || previewPhase === "running")}>Stop sync</button><button className="button primary" onClick={runRecovery} disabled={busy || (hasBackend ? !targetMachine || targetMachine.fault_type === "NONE" || Boolean(activeRun) : previewPhase !== "faulted")}>Run recovery</button><button className="reset-button" onClick={resetDemo} disabled={busy || Boolean(activeRun) || !hasBackend && previewPhase === "running"}>Reset</button></div>
      </section>
      <BackendActivity
        states={activity}
        live={hasBackend}
        runStatus={run?.status}
        incidentState={incidentState}
        machine={targetMachine}
        busy={busy}
        onSimulateError={stopSync}
        onRecover={runRecovery}
      />
      <footer><span>GhostShift / Toronto 2026</span><span>Agents in context ↗</span></footer>
    </div>
  </main>;
}
