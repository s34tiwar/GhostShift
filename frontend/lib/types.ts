export type MachineId = "machine_a" | "machine_b" | "machine_c";
export type MachineStatus = "healthy" | "failed" | "recovering" | "reconciled";
export interface Machine { machine_id: MachineId; name: string; status: MachineStatus; records_synced: number; backlog: number; fault_epoch: number; restart_count: number; }
export interface RecoveryEvent { id: string; label: string; detail: string; state: "waiting" | "active" | "complete"; timestamp?: string; }
