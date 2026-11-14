import type { Machine } from "@/lib/types";
const statusLabel = { healthy: "Synced", failed: "Not synced", recovering: "Not synced", reconciled: "Synced" };

export default function FactoryCards({ machines }: { machines: Machine[] }) {
  return <div className="machine-grid">{machines.map((machine) => <article className={`machine-card ${machine.status}`} key={machine.machine_id}>
    <div className="machine-card-top"><div><p className="eyebrow">Factory unit</p><h3>{machine.name}</h3></div><span className={`status-pill ${machine.status}`}><span className="status-dot"/>{statusLabel[machine.status]}</span></div>
    <div className="machine-rack" aria-hidden="true"><div className="rack-unit"><span className="rack-vents"/><span className="rack-light"/></div><div className="rack-unit"><span className="rack-vents"/><span className="rack-light"/></div></div>
    <div className="machine-stats"><div><span>Records synced</span><strong>{machine.records_synced.toLocaleString()}</strong></div><div><span>Backlog</span><strong>{machine.backlog}</strong></div><div><span>Restarts</span><strong>{machine.restart_count}</strong></div></div>
    <span className="machine-port" aria-hidden="true"/>
  </article>)}</div>;
}
