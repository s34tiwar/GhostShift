import type { Machine } from "@/lib/types";

function DatabaseIcon() {
  return (
    <svg viewBox="0 0 48 48" fill="none" aria-hidden="true">
      <ellipse cx="24" cy="10" rx="15" ry="6" />
      <path d="M9 10v26c0 3.3 6.7 6 15 6s15-2.7 15-6V10M9 23c0 3.3 6.7 6 15 6s15-2.7 15-6M9 35c0 3.3 6.7 6 15 6s15-2.7 15-6" />
    </svg>
  );
}

export default function ConnectionMap({ machines }: { machines: Machine[] }) {
  const disconnected = machines[1].status === "failed" || machines[1].status === "recovering";

  return (
    <div className="connection-map" role="img" aria-label={`Machine A synced with MongoDB Atlas. Machine B ${disconnected ? "not synced" : "synced"}. Machine C synced with MongoDB Atlas.`}>
      <svg className="connection-lines" viewBox="0 0 1200 154" preserveAspectRatio="none" aria-hidden="true">
        <path className="connection-path" d="M200 0 V48 L600 150" />
        {disconnected ? <>
          <path className="connection-path broken" d="M600 0 V48" />
          <path className="connection-path broken" d="M600 96 V150" />
        </> : <path className="connection-path" d="M600 0 V150" />}
        <path className="connection-path" d="M1000 0 V48 L600 150" />
        {disconnected && <>
          <circle className="connection-break-point" cx="600" cy="72" r="21" />
          <path className="connection-break" d="M592 64 L608 80 M608 64 L592 80" />
        </>}
      </svg>
      <div className="database-node" aria-hidden="true">
        <div className="database-icon"><DatabaseIcon /></div>
        <strong>MongoDB Atlas</strong>
      </div>
    </div>
  );
}
