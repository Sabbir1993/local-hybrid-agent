import { StrictMode, Suspense, lazy } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from './components/common/ErrorBoundary'
import { dismissBootLoader } from './components/common/AppLoader'

// /voice is the public voice-collection page: no sign-in, none of the app's start-up calls (routes/speech_corpus.py)
const VoiceCollectPage = lazy(() => import('./components/voice-collect/VoiceCollectPage'))
const isVoicePage = window.location.pathname.replace(/\/+$/, '') === '/voice'
if (isVoicePage) dismissBootLoader()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary fallbackTitle="The app crashed">
      {isVoicePage
        ? <Suspense fallback={null}><VoiceCollectPage /></Suspense>
        : <App />}
    </ErrorBoundary>
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
