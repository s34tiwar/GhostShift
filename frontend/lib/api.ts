import type { AuditEvent, FactoryMachine, LedgerEntry, MachineId, RunView } from "./types";

const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "";
export const hasBackend = baseUrl.length > 0;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = body.detail ?? message;
    } catch { /* Keep the status message for non-JSON errors. */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export const api = {
  machines: () => request<FactoryMachine[]>("/api/machines"),
  stopSync: (machine_id: MachineId) => request<FactoryMachine>("/api/scenarios/sync_failure", {
    method: "POST", body: JSON.stringify({ machine_id }),
  }),
  reset: () => request<{ ok: boolean }>("/api/scenarios/reset", {
    method: "POST", body: JSON.stringify({ confirm: true }),
  }),
  createRun: (machine_id: MachineId) => request<{ run_id: string; status: RunView["status"] }>("/api/runs", {
    method: "POST", body: JSON.stringify({ machine_id, crash_armed: false }),
  }),
  run: (id: string) => request<RunView>(`/api/runs/${encodeURIComponent(id)}`),
  audit: (id: string) => request<{ events: AuditEvent[] }>(`/api/runs/${encodeURIComponent(id)}/audit`),
  actions: (id: string) => request<LedgerEntry[]>(`/api/runs/${encodeURIComponent(id)}/actions`),
};
