import type { AuditEvent, FactoryMachine, LedgerEntry, MachineId, RunView } from "./types";

const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "";
export const hasBackend = baseUrl.length > 0;

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

/** Turn a FastAPI `detail` (string, 422 array, or object) into readable text. */
export function detailMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => (item && typeof item === "object" && typeof (item as { msg?: unknown }).msg === "string" ? (item as { msg: string }).msg : null))
      .filter((part): part is string => Boolean(part));
    return parts.length ? parts.join("; ") : fallback;
  }
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return fallback;
}

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
      message = detailMessage(body?.detail, message);
    } catch { /* Keep the status message for non-JSON errors. */ }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export const api = {
  machines: () => request<FactoryMachine[]>("/api/machines"),
  stopSync: (machine_id: MachineId) => request<FactoryMachine>("/api/scenarios/sync_failure", {
    method: "POST", body: JSON.stringify({ machine_id }),
  }),
  reset: (force = false) => request<{ ok: boolean; machines: FactoryMachine[]; aborted_run_ids: string[] }>("/api/scenarios/reset", {
    method: "POST", body: JSON.stringify({ confirm: true, force }),
  }),
  createRun: (machine_id: MachineId, options: { crashArmed?: boolean; scenario?: "unsafe_shutdown" } = {}) => request<{ run_id: string; status: RunView["status"] }>("/api/runs", {
    method: "POST",
    body: JSON.stringify({
      machine_id,
      ...(options.crashArmed ? { crash_armed: true } : {}),
      ...(options.scenario ? { scenario: options.scenario } : {}),
    }),
  }),
  run: (id: string) => request<RunView>(`/api/runs/${encodeURIComponent(id)}`),
  audit: (id: string) => request<{ events: AuditEvent[] }>(`/api/runs/${encodeURIComponent(id)}/audit`),
  actions: (id: string) => request<LedgerEntry[]>(`/api/runs/${encodeURIComponent(id)}/actions`),
};
