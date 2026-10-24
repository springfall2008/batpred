import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// Scope the landing-page preference to this installation, including HA ingress.
const uiPath = window.location.pathname.match(/^(.*\/)ui(?:\/|$)/)
if (uiPath) {
  try { localStorage.setItem('predbat-interface:' + uiPath[1], 'modern') } catch {}
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>
)
