import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RoutedApp } from './App';
import './shared/tokens.css';
import './shared/global.css';
import './chat/chat.css';
import './flight.css';

createRoot(document.getElementById('root')!).render(<StrictMode><RoutedApp /></StrictMode>);
