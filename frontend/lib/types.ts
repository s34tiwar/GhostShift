export type MachineId = "machine_a" | "machine_b" | "machine_c";
export type MachineStatus = "healthy" | "failed" | "recovering" | "reconciled";
export interface Machine { machine_id: MachineId; name: string; status: MachineStatus; records_synced?: number; backlog: number; fault_epoch: number; restart_count: number; }
export interface FactoryMachine { machine_id: MachineId; healthy: boolean; fault_type: "NONE" | "SYNC_PROCESS_DOWN" | "NETWORK_DISCONNECTED"; sync_running: boolean; network_connected: boolean; backlog: number; fault_epoch: number; restart_count?: number; reconnect_count?: number; message?: string | null; }
export type RunStatus = "QUEUED" | "CREATED" | "DIAGNOSING" | "ACTION_PENDING" | "RUNNING" | "NEEDS_RECONCILIATION" | "COMPLETED" | "FAILED" | "DENIED";
export interface RunView { run_id: string; machine_id: MachineId; status: RunStatus; procedure_id: string | null; retrieval_mode: string | null; current_action_id: string | null; worker_status: string | null; }
export interface AuditEvent { at: string; actor: string; event_type: string; reason?: string | null; action_id?: string | null; }
export interface LedgerEntry { action_id: string; status: "PLANNED" | "AUTHORIZED" | "EXECUTING" | "EFFECT_VERIFIED" | "DENIED" | "FAILED" | "REQUIRES_REVIEW"; policy_decision?: { allowed: boolean; reason?: string | null } | null; external_receipt?: Record<string, unknown> | null; }
export interface RecoveryEvent { id: string; label: string; detail: string; state: "waiting" | "active" | "complete"; timestamp?: string; }
