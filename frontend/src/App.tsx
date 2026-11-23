import { useEffect } from 'react';
import { useAppStore } from './stores/useAppStore';
import { useStatusWs } from './hooks/useStatusWs';
import { Sidebar } from './components/sidebar/Sidebar';
import { Header } from './components/header/Header';
import { ChatArea } from './components/chat/ChatArea';
import { Composer } from './components/chat/Composer';
import { RightDock } from './components/dock/RightDock';
import { AppLoader, dismissBootLoader } from './components/common/AppLoader';
import { ProjectModal } from './components/modals/ProjectModal';
import { AgentMemoryModal } from './components/modals/AgentMemory';
import { AgentModals } from './components/modals/AgentModals';
import { IdleWarning, ForcePasswordModal } from './components/common/SessionGuards';
import { PermissionModal } from './components/modals/PermissionModal';
import { DevModals } from './components/modals/DevModals';
import { SettingsModal } from './components/modals/SettingsModal';
import { ReportModal } from './components/modals/ReportModal';
import { AuditModal } from './components/modals/AuditModal';
import { DbModal } from './components/modals/DbModal';
import { ProfileModal } from './components/modals/ProfileModal';
import { LoginPage } from './components/auth/LoginPage';
import { ErrorBoundary } from './components/common/ErrorBoundary';
import { logger } from './utils/logger';
import { loadAppearance } from './utils/appearance';
import { env } from './config/env';
import { ThemedToast } from './components/common/ThemedToast';
import { ThemedDialog } from './components/common/ThemedDialog';

export function App() {
  const { 
    theme, 
    sidebarCollapsed, 
    isAuthenticated, 
    currentUser,
    hasPerm,
    checkAuth, 
    fetchModels, 
    fetchProjects,
    fetchSessions,
    activeModal,
    setActiveModal,
    createSession,
    sessionExpired,
  } = useAppStore();

  useEffect(() => {
    (window as any).__user = currentUser;
    (window as any).hasPerm = (perm: string) => hasPerm(perm);
  }, [currentUser, hasPerm]);

  // Per-user appearance must survive a refresh (theme-boot.js only knows localStorage).
  useEffect(() => { void loadAppearance(); }, []);

  // apiFetch calls this on any 401 outside the sign-in endpoints
  useEffect(() => {
    (window as any).__onAuthRequired = sessionExpired;
    return () => { delete (window as any).__onAuthRequired; };
  }, [sessionExpired]);

  // Check login authentication on boot
  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  // Connect real-time WebSocket telemetry only when logged in
  useStatusWs();

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  // browser tab name: from VITE_PROJECT_NAME (index.html's %VITE_PROJECT_NAME% covers the first paint)
  useEffect(() => {
    document.title = env.projectName;
    try {
      // Security hygiene: purge legacy raw SQL query history from persistent localStorage
      localStorage.removeItem('a770_db_query_history');
      // Clean internal server filesystem paths if previously stored in app_model
      const savedModel = localStorage.getItem('app_model');
      if (savedModel && (savedModel.includes('/') || savedModel.includes('\\'))) {
        const cleanName = savedModel.split(/[/\\]/).pop() || '';
        if (cleanName) {
          localStorage.setItem('app_model', cleanName);
        } else {
          localStorage.removeItem('app_model');
        }
      }
    } catch (_) {}
  }, []);

  useEffect(() => {
    document.body.classList.toggle('sidebar-collapsed', sidebarCollapsed);
  }, [sidebarCollapsed]);

  useEffect(() => {
    if (isAuthenticated) {
      const isNative = typeof window !== 'undefined' && Boolean(
        (window as any).electronAPI?.isNativeApp ||
        (window as any).isCompanionApp ||
        navigator.userAgent.includes('Electron') ||
        window.location.search.includes('companion=true')
      );

      if (isNative) {
        document.documentElement.classList.add('is-native-app');
        document.body.classList.add('is-native-app');
        // Companion app: only Agent and Code appear. If in chat mode, switch to code:
        const curMode = useAppStore.getState().appMode;
        if (curMode === 'chat') {
          useAppStore.getState().setAppMode('code');
        }
      } else {
        document.documentElement.classList.remove('is-native-app');
        document.body.classList.remove('is-native-app');
        // Web browser: only Chat mode exists
        if (useAppStore.getState().appMode !== 'chat') {
          useAppStore.getState().setAppMode('chat');
        }
      }

      logger.info('System', `${env.projectName} session active`, {
        version: env.version,
        companionConnected: isNative,
        deviceId: isNative ? (window as any).electronAPI?.deviceId : null,
      });

      fetchModels();
      fetchProjects();
      fetchSessions().then(() => {
        try {
          const params = new URLSearchParams(window.location.search);
          const sharedSession = params.get('session');
          if (sharedSession) {
            void useAppStore.getState().openSession(sharedSession);
          }
        } catch (_) {}
      });
    }
  }, [isAuthenticated, fetchModels, fetchProjects, fetchSessions]);

  // The HTML boot loader stays up until we know whether the user is signed in; then the real screen fades in under it
  useEffect(() => {
    if (isAuthenticated !== null) dismissBootLoader();
  }, [isAuthenticated]);

  // URL routing synchronization for /settings, /report, /audit, and /db
  useEffect(() => {
    const handleUrlChange = () => {
      const p = window.location.pathname.toLowerCase();
      const h = window.location.hash.toLowerCase();
      if (p === '/settings' || h === '#settings' || h.startsWith('#/settings') || h.startsWith('#sec-')) {
        setActiveModal('settings');
      } else if (p === '/report' || h === '#report' || h.startsWith('#/report')) {
        setActiveModal('report');
      } else if (p === '/audit' || h === '#audit' || h.startsWith('#/audit')) {
        setActiveModal('audit');
      } else if (p === '/db' || h === '#db' || h.startsWith('#/db')) {
        setActiveModal('db');
      }
    };

    handleUrlChange();
    window.addEventListener('popstate', handleUrlChange);
    window.addEventListener('hashchange', handleUrlChange);
    return () => {
      window.removeEventListener('popstate', handleUrlChange);
      window.removeEventListener('hashchange', handleUrlChange);
    };
  }, [setActiveModal]);

  // Layout shortcuts: Ctrl+B sidebar, Ctrl+Shift+B right panel, Ctrl+Shift+F files, Ctrl+` terminal
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      if (!mod || e.altKey) return;
      const k = e.key.toLowerCase();
      const st = useAppStore.getState();
      const tag = (document.activeElement as HTMLElement | null)?.tagName;
      if (k === 'b' && !e.shiftKey) {
        if (tag === 'INPUT' || tag === 'TEXTAREA') return;   // Ctrl+B in a text box is the user's
        e.preventDefault();
        st.toggleSidebar();
      } else if (k === 'b' && e.shiftKey) {
        e.preventDefault();
        st.toggleDock();
      } else if (k === 'f' && e.shiftKey) {
        e.preventDefault();
        if (st.dockCollapsed || st.dockTab !== 'files') st.setDockTab('files');
        if (st.dockCollapsed) st.setDockCollapsed(false);
      } else if (e.key === '`') {
        e.preventDefault();
        st.toggleDock('terminal');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // Global keydown listeners (Escape to close open modal, Ctrl+K for new session)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        createSession();
      } else if (e.key === 'Escape') {
        if (['/settings', '/report', '/audit', '/db'].includes(window.location.pathname)) {
          try {
            window.history.pushState({}, '', '/');
          } catch (_) {}
        }
        setActiveModal('none');
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [createSession, setActiveModal]);

  // Show login screen if not authenticated
  if (isAuthenticated === false) {
    return <LoginPage />;
  }

  // Start-up screen while the sign-in check runs
  if (isAuthenticated === null) return <AppLoader />;

  if (activeModal === 'settings') {
    return (
      <ErrorBoundary fallbackTitle="Settings">
        <SettingsModal />
      </ErrorBoundary>
    );
  }

  if (activeModal === 'report') {
    return (
      <ErrorBoundary fallbackTitle="Token Usage Report">
        <ReportModal />
      </ErrorBoundary>
    );
  }

  if (activeModal === 'audit') {
    return (
      <ErrorBoundary fallbackTitle="Security & Audit Logs">
        <AuditModal />
      </ErrorBoundary>
    );
  }

  if (activeModal === 'db') {
    return (
      <ErrorBoundary fallbackTitle="Database Explorer">
        <DbModal />
      </ErrorBoundary>
    );
  }

  if (activeModal === 'profile') {
    return (
      <ErrorBoundary fallbackTitle="User Profile">
        <ProfileModal onClose={() => setActiveModal('none')} />
      </ErrorBoundary>
    );
  }

  return (
    <div id="app" className={sidebarCollapsed ? 'sidebar-collapsed' : ''}>
      <ErrorBoundary fallbackTitle="Sidebar">
        <Sidebar />
      </ErrorBoundary>
      <main>
        <ErrorBoundary fallbackTitle="Header">
          <Header />
        </ErrorBoundary>
        <ErrorBoundary fallbackTitle="Chat Feed">
          <ChatArea />
        </ErrorBoundary>
        <ErrorBoundary fallbackTitle="Composer">
          <Composer />
        </ErrorBoundary>
      </main>
      <ErrorBoundary fallbackTitle="Right Dock">
        <RightDock />
      </ErrorBoundary>
      {/* each in its own boundary: a crash in one modal must show its error, not blank the whole app */}
      <ErrorBoundary fallbackTitle="Dialogs"><DevModals /></ErrorBoundary>
      <ErrorBoundary fallbackTitle="Permission dialog"><PermissionModal /></ErrorBoundary>
      <ErrorBoundary fallbackTitle="Idle warning"><IdleWarning /></ErrorBoundary>
      <ErrorBoundary fallbackTitle="Password dialog"><ForcePasswordModal /></ErrorBoundary>
      <ErrorBoundary fallbackTitle="Agent dialog"><AgentModals /></ErrorBoundary>
      <ErrorBoundary fallbackTitle="Agent memory"><AgentMemoryModal /></ErrorBoundary>
      <ErrorBoundary fallbackTitle="Project dialog"><ProjectModal /></ErrorBoundary>
      <ThemedToast />
      <ErrorBoundary fallbackTitle="Dialog"><ThemedDialog /></ErrorBoundary>
    </div>
  );
}

export default App;
