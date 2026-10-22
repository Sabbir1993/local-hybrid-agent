import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// Block the DevTools shortcuts (best effort in a normal browser; the desktop app disables DevTools itself)
window.addEventListener('keydown', (e) => {
  const k = e.key.toLowerCase();
  if (k === 'f12' || ((e.ctrlKey || e.metaKey) && e.shiftKey && ['i', 'j', 'c'].includes(k))) {
    e.preventDefault();
    e.stopPropagation();
  }
}, true);
