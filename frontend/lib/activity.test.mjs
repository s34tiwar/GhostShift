import test from "node:test";
import assert from "node:assert/strict";
import { liveSteps, toDisplayMachines } from "./activity.ts";

const machine = {
  machine_id: "machine_b", healthy: false, fault_type: "SYNC_PROCESS_DOWN",
  sync_running: false, network_connected: true, backlog: 125, fault_epoch: 1,
};
const run = {
  run_id: "run_demo", machine_id: "machine_b", status: "QUEUED",
  procedure_id: null, retrieval_mode: null, current_action_id: null, worker_status: "IDLE",
};

test("queued run highlights inspection", () => {
  assert.deepEqual(liveSteps(machine, run, [], []), ["done", "active", "pending", "pending", "pending", "pending"]);
});

test("ledger execution highlights repair", () => {
  const states = liveSteps(machine, { ...run, status: "RUNNING", procedure_id: "proc_sync_restart" },
    [{ at: "now", actor: "worker", event_type: "EXECUTION_STARTED" }],
    [{ action_id: "act_1", status: "EXECUTING", policy_decision: { allowed: true } }]);
  assert.deepEqual(states, ["done", "done", "done", "done", "active", "pending"]);
});

test("verification requires both ledger evidence and healthy machine", () => {
  const completed = { ...run, status: "COMPLETED", procedure_id: "proc_sync_restart" };
  const events = [{ at: "now", actor: "worker", event_type: "EFFECT_VERIFIED" }];
  const actions = [{ action_id: "act_1", status: "EFFECT_VERIFIED", policy_decision: { allowed: true } }];
  assert.deepEqual(liveSteps({ ...machine, healthy: true }, completed, events, actions), Array(6).fill("done"));
  assert.equal(liveSteps(machine, completed, events, actions)[5], "blocked");
});

test("a healthy effect stays visually disconnected before verification", () => {
  const display = toDisplayMachines([{ ...machine, healthy: true, fault_type: "NONE", sync_running: true }],
    { ...run, status: "RUNNING" }, [{ action_id: "act_1", status: "EXECUTING" }]);
  assert.equal(display[0].status, "recovering");
});

test("denial highlights policy and never marks repair complete", () => {
  const states = liveSteps(machine, { ...run, status: "DENIED", procedure_id: "proc_sync_restart" }, [],
    [{ action_id: "act_1", status: "DENIED", policy_decision: { allowed: false } }]);
  assert.equal(states[3], "blocked");
  assert.equal(states[4], "pending");
});

test("reconciliation focuses verification after a receipt", () => {
  const states = liveSteps(machine, { ...run, status: "NEEDS_RECONCILIATION", procedure_id: "proc_sync_restart" },
    [{ at: "now", actor: "worker", event_type: "EXECUTION_STARTED" }],
    [{ action_id: "act_1", status: "REQUIRES_REVIEW", policy_decision: { allowed: true }, external_receipt: { applied: true } }]);
  assert.equal(states[4], "done");
  assert.equal(states[5], "active");
  assert.equal(states.filter((state) => state === "active").length, 1);
});

const healthy = { ...machine, healthy: true, fault_type: "NONE", sync_running: true };

test("a denied or failed run on a healthy machine shows the machine as synced", () => {
  for (const status of ["DENIED", "FAILED"]) {
    const display = toDisplayMachines([healthy], { ...run, status }, [{ action_id: "act_1", status: "DENIED" }]);
    assert.equal(display[0].status, "healthy", status);
  }
});

test("a crash after the effect shows repair applied and verification waiting", () => {
  const states = liveSteps(healthy, { ...run, status: "RUNNING", procedure_id: "proc_sync_restart" },
    [{ at: "1", actor: "w", event_type: "EXECUTION_STARTED" }, { at: "2", actor: "executor", event_type: "CRASH_INJECTED" }],
    [{ action_id: "act_1", status: "EXECUTING", policy_decision: { allowed: true }, external_receipt: { applied: true } }]);
  assert.equal(states[4], "done");
  assert.equal(states[5], "active");
});

test("an aborted or crash-looped run blocks no pipeline step", () => {
  for (const event_type of ["RUN_ABORTED_BY_RESET", "RECOVERY_ATTEMPTS_EXCEEDED"]) {
    const states = liveSteps(machine, { ...run, status: "FAILED", procedure_id: "proc_sync_restart" },
      [{ at: "1", actor: "demo_reset", event_type }], []);
    assert.ok(!states.includes("blocked"), event_type);
    assert.ok(!states.includes("active"), event_type);
  }
});
