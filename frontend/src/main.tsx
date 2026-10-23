import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { dismissBootLoader } from './components/common/AppLoader'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

// The HTML boot loader (index.html) stays up until App.tsx calls dismissBootLoader(); this only guarantees it can never
// stay forever if the app fails to start.
window.setTimeout(() => dismissBootLoader(), 20000);

// Block the DevTools shortcuts (best effort in a normal browser; the desktop app disables DevTools itself)
window.addEventListener('keydown', (e) => {
  const k = e.key.toLowerCase();
  if (k === 'f12' || ((e.ctrlKey || e.metaKey) && e.shiftKey && ['i', 'j', 'c'].includes(k))) {
    e.preventDefault();
    e.stopPropagation();
  }
}, true);
