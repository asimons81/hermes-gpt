import { Link, Navigate, Route, Routes } from 'react-router-dom';

import { ChatPage } from './chat/ChatPage';
import { AccountPanel } from './flight/AccountPanel';
import { ApprovalsPanel } from './flight/ApprovalsPanel';
import { ContractDetail, ContractsPanel } from './flight/ContractsPanel';
import { DeckOverview } from './flight/DeckOverview';
import { EventHistoryPanel } from './flight/EventHistoryPanel';
import { FleetPanel } from './flight/FleetPanel';
import { SURFACE_SCHEMAS } from './flight/schemas';
import { SurfacePanel } from './flight/SurfacePanel';
import { SwarmDetail, SwarmMonitor } from './flight/SwarmMonitor';
import { MISSION_SURFACES } from './flight/types';

function FlightNav() {
  return (
    <nav className="fd-nav" aria-label="Flight Deck">
      <Link to="/chat" className="fd-nav-brand">HERMES</Link>
      <div className="fd-nav-links">
        <Link to="/chat">Chat</Link><Link to="/ops">Deck</Link><Link to="/events">Events</Link>
        <Link to="/ops/contracts">Contracts</Link><Link to="/ops/swarm">Swarm</Link><Link to="/account">Account</Link>
      </div>
    </nav>
  );
}

function OpsSurface() {
  return <div className="fd-shell"><FlightNav /><main className="fd-main" role="main"><Routes>
    <Route index element={<DeckOverview />} />
    {MISSION_SURFACES.map((surface) => {
      const schema = SURFACE_SCHEMAS[surface];
      const element = surface === 'approvals' ? <ApprovalsPanel /> : surface === 'fleet' ? <FleetPanel /> : schema ? <SurfacePanel schema={schema} /> : <DeckOverview />;
      return <Route key={surface} path={surface} element={element} />;
    })}
    <Route path="contracts" element={<ContractsPanel />} /><Route path="contracts/:contractSha256" element={<ContractDetail />} />
    <Route path="swarm" element={<SwarmMonitor />} /><Route path="swarm/:workflowId" element={<SwarmDetail />} />
    <Route path="*" element={<DeckOverview />} />
  </Routes></main></div>;
}

export function App(): JSX.Element {
  return <Routes>
    <Route path="/" element={<Navigate to="/chat" replace />} />
    <Route path="/chat" element={<ChatPage />} /><Route path="/chat/:sessionId" element={<ChatPage />} />
    <Route path="/ops/*" element={<OpsSurface />} />
    <Route path="/events" element={<div className="fd-shell"><FlightNav /><main className="fd-main" role="main"><EventHistoryPanel /></main></div>} />
    <Route path="/account" element={<div className="fd-shell"><FlightNav /><main className="fd-main" role="main"><AccountPanel /></main></div>} />
    <Route path="*" element={<Navigate to="/chat" replace />} />
  </Routes>;
}
