import type { RecoveryEvent } from "@/lib/types";
export default function ActionTimeline({ events }: { events: RecoveryEvent[] }) {
  return <ol className="timeline">{events.map((event) => <li className={event.state} key={event.id}>
    <div className="timeline-marker">{event.state === "complete" ? "✓" : event.state === "active" ? "•" : ""}</div>
    <div className="timeline-copy"><div><strong>{event.label}</strong>{event.timestamp && <time>{event.timestamp}</time>}</div><p>{event.detail}</p></div>
  </li>)}</ol>;
}
