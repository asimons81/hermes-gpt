import type { ToolActivity } from '../stores/conversation';

export function ActivityCard({ activity, onToggle }: { activity: ToolActivity; onToggle: () => void }): JSX.Element {
  return <section className="activity" data-status={activity.status}>
    <button className="activity-summary" aria-expanded={activity.expanded} aria-controls={`activity-${activity.callId}`} onClick={onToggle}>
      <span className="activity-label">tool · {activity.name}</span><span className="activity-text">{activity.brief}</span>
      <span className="activity-elapsed">{activity.durationMs == null ? 'running' : `${activity.durationMs}ms`}</span>
    </button>
    {activity.expanded && <pre id={`activity-${activity.callId}`} className="activity-detail">{activity.summary ?? 'Executing…'}</pre>}
  </section>;
}
