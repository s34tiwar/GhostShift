import type { AuditEvent, FactoryMachine, LedgerEntry, Machine, RunView } from "./types";

export type ActivityState = "pending" | "active" | "done" | "blocked";
export type PreviewPhase = "idle" | "faulted" | "running" | "complete";

export const TERMINAL_RUN_STATUSES = ["COMPLETED", "FAILED", "DENIED"] as const;

export function isTerminal(status: string | undefined | null): boolean {
  return (TERMINAL_RUN_STATUSES as readonly string[]).includes(status ?? "");
}

/** A run stopped from outside the pipeline (demo reset, crash-loop guard), not by a failed step. */
export function stoppedReason(audit: AuditEvent[]): "aborted" | "attempts" | null {
  if (audit.some((event) => event.event_type === "RUN_ABORTED_BY_RESET")) return "aborted";
  if (audit.some((event) => event.event_type === "RECOVERY_ATTEMPTS_EXCEEDED")) return "attempts";
  return null;
}

export function previewSteps(phase: PreviewPhase, activeStep: number): ActivityState[] {
  return Array.from({ length: 6 }, (_, index) => {
    if (phase === "complete") return "done";
    if (phase === "idle") return "pending";
    if (phase === "faulted") return index === 0 ? "done" : "pending";
    return index < activeStep ? "done" : index === activeStep ? "active" : "pending";
  });
}

export function liveSteps(machine: FactoryMachine | undefined, run: RunView | null, audit: AuditEvent[], actions: LedgerEntry[]): ActivityState[] {
  const states: ActivityState[] = Array(6).fill("pending");
  if (!machine) return states;
  if (machine.fault_type !== "NONE" || run) states[0] = "done";
  if (!run) return states;

  const events = new Set(audit.map((event) => event.event_type));
  const action = actions.at(-1);
  const planned = Boolean(run.procedure_id || events.has("PLANNER_RESULT") || events.has("PLANNER_FALLBACK"));
  const authorized = action?.policy_decision?.allowed === true || ["AUTHORIZED", "EXECUTING", "EFFECT_VERIFIED"].includes(action?.status ?? "");
  const executing = events.has("EXECUTION_STARTED") || ["EXECUTING", "EFFECT_VERIFIED", "REQUIRES_REVIEW"].includes(action?.status ?? "");
  const verified = action?.status === "EFFECT_VERIFIED" && events.has("EFFECT_VERIFIED");

  if (run.status === "QUEUED" || run.status === "CREATED") {
    states[1] = "active";
  } else if (run.status === "DIAGNOSING") {
    states[1] = planned ? "done" : "active";
    if (planned) states[2] = "active";
  } else {
    states[1] = "done";
    states[2] = planned ? "done" : "active";
    if (planned) states[3] = authorized ? "done" : "active";
    if (authorized) states[4] = executing ? "active" : "pending";
    if (verified) {
      states[4] = "done";
      states[5] = "active";
    }
  }

  // Crash drill: the worker died after the repair was applied and before it was
  // verified. Show the repair as applied and verification waiting for a takeover.
  if (events.has("CRASH_INJECTED") && !verified && !isTerminal(run.status)) {
    states[4] = "done";
    states[5] = "active";
  }

  if (run.status === "COMPLETED") {
    if (verified && machine.healthy) return states.map(() => "done");
    for (let index = 1; index < states.length; index++) if (states[index] === "active") states[index] = "pending";
    states[5] = "blocked";
  } else if (run.status === "DENIED" || action?.status === "DENIED") {
    for (let index = 1; index < states.length; index++) if (states[index] === "active") states[index] = "pending";
    states[3] = "blocked";
    states[4] = "pending";
    states[5] = "pending";
  } else if (run.status === "FAILED") {
    for (let index = 1; index < states.length; index++) if (states[index] === "active") states[index] = "pending";
    // Aborted by a reset or stopped by the crash-loop guard: no pipeline step failed.
    if (stoppedReason(audit) === null) {
      const failedIndex = events.has("PLANNER_ABSTAINED") ? 2 : executing ? 5 : planned ? 3 : 1;
      states[failedIndex] = "blocked";
      for (let index = failedIndex + 1; index < states.length; index++) states[index] = "pending";
    }
  } else if (run.status === "NEEDS_RECONCILIATION" || action?.status === "REQUIRES_REVIEW") {
    const verifying = Boolean(action?.external_receipt) || action?.status === "REQUIRES_REVIEW" || verified;
    for (let index = 1; index < states.length; index++) if (states[index] === "active") states[index] = "pending";
    states[4] = verifying ? "done" : "active";
    states[5] = verifying ? "active" : "pending";
  }
  return states;
}

export function toDisplayMachines(machines: FactoryMachine[], run: RunView | null, actions: LedgerEntry[]): Machine[] {
  const verified = actions.some((action) => action.status === "EFFECT_VERIFIED");
  return [...machines].sort((a, b) => a.machine_id.localeCompare(b.machine_id)).map((machine) => {
    const isTarget = run?.machine_id === machine.machine_id;
    const completed = isTarget && run?.status === "COMPLETED" && verified && machine.healthy;
    // Healthy but not yet verified only while the run is still going; once the run is
    // COMPLETED, FAILED or DENIED the card follows the machine's real health.
    const unverifiedEffect = isTarget && Boolean(run) && !isTerminal(run?.status) && !completed && machine.healthy;
    const status = completed ? "reconciled" : unverifiedEffect ? "recovering" : machine.healthy && machine.sync_running && machine.network_connected ? "healthy" : "failed";
    return {
      machine_id: machine.machine_id,
      name: `Machine ${machine.machine_id.slice(-1).toUpperCase()}`,
      status,
      backlog: machine.backlog,
      fault_epoch: machine.fault_epoch,
      restart_count: machine.restart_count ?? 0,
    };
  });
}
