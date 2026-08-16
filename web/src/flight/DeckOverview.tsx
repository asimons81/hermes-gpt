// DeckOverview — the /ops landing: mission surface links + overview summary.
import { Link } from 'react-router-dom';

import { MISSION_SURFACES } from './types';
import { SURFACE_SCHEMAS } from './schemas';
import { useOperatorStore } from '../stores/operator';

const TITLES: Record<string, string> = {
  overview: 'Mission Control Overview',
  health: 'Health',
  profiles: 'Profiles',
  fleet: 'Fleet',
  codex: 'Codex',
  cron: 'Cron',
  delegations: 'Delegations',
  failures: 'Failures',
  approvals: 'Approvals',
  vault: 'Vault',
  usage: 'Usage',
  audit: 'Audit',
};

export function DeckOverview() {
  const surfaces = useOperatorStore((s) => s.surfaces);
  void SURFACE_SCHEMAS; // schemas are consumed by the per-surface routes

  return (
    <section className="fd-deck" data-testid="deck-overview">
      <header className="fd-surface-head">
        <h2>Flight Deck</h2>
        <p className="fd-surface-desc">Operator surfaces around chat. Read-only presentation; mutations stay gated.</p>
      </header>

      <div className="fd-deck-grid">
        {MISSION_SURFACES.map((surface) => {
          const entry = surfaces[surface];
          const state = entry?.status ?? 'idle';
          return (
            <Link key={surface} to={`/ops/${surface}`} className="fd-deck-card" data-state={state}>
              <span className="fd-deck-card-title">{TITLES[surface] ?? surface}</span>
              <span className="fd-deck-card-meta">
                {state === 'ready' ? (entry?.data?.available === false ? 'unavailable' : 'ready') : state}
              </span>
            </Link>
          );
        })}
      </div>

      <div className="fd-deck-links">
        <Link className="fd-btn fd-btn--ghost" to="/events">
          Event History
        </Link>
        <Link className="fd-btn fd-btn--ghost" to="/ops/contracts">
          Contracts
        </Link>
        <Link className="fd-btn fd-btn--ghost" to="/ops/swarm">
          Swarm Monitor
        </Link>
        <Link className="fd-btn fd-btn--ghost" to="/account">
          Account
        </Link>
      </div>
    </section>
  );
}
