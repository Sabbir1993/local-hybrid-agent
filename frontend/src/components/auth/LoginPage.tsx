import React, { useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { api, apiFetch } from '../../services/api';
import { env, withOrgDomain } from '../../config/env';

// A run, as the agent would report it. Plays once on load: the one moving thing on the page.
const TRACE = [
  { mark: '◇', verb: 'plan', text: 'Reconcile March settlements, 3 steps' },
  { mark: '✓', verb: 'read', text: 'settlements_mar.xlsx  (4,212 rows)' },
  { mark: '✓', verb: 'run', text: 'python reconcile.py --month 03' },
  { mark: '✓', verb: 'check', text: 'Totals match the gateway ledger' },
  { mark: '●', verb: 'done', text: 'Report saved to /reports/march.docx' },
];

// Corporate ledger: ink-dark brand panel, paper-toned sign-in side, one teal accent. Newsreader for headings, IBM Plex for text.
const CSS = `
.lg-root { --ink:#0e1d1c; --ink2:#132826; --bone:#e9f0ee; --dmute:#8fa19e; --dline:rgba(233,240,238,.11);
  --paper:#f3f1ea; --card:#ffffff; --text:#17201f; --mute:#5d6967; --line:#d9d4c6; --field:#ffffff; --soft:#f6f5f0;
  --accent:#0f766e; --accent-hi:#14968b; --accent-dk:#0b5c56; --amber:#d4942c; --danger:#a4271c; --dangerbg:#fdf1ef; --dangerline:#f0c3bc;
  display:grid; grid-template-columns:minmax(0,1.1fr) minmax(400px,.9fr); width:100%; height:100vh; height:100dvh; background:var(--paper); color:var(--text);
  font-family:'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', sans-serif; overflow:hidden; }
.lg-stage { position:relative; display:flex; flex-direction:column; justify-content:space-between; padding:48px clamp(36px,5vw,84px); min-width:0; overflow:hidden; color:var(--bone);
  background:
    radial-gradient(900px 480px at 0% 110%, rgba(20,150,139,.2), transparent 62%),
    linear-gradient(var(--dline) 1px, transparent 1px) 0 0/100% 64px,
    linear-gradient(90deg, var(--dline) 1px, transparent 1px) 0 0/64px 100%,
    var(--ink); }
.lg-brand { display:flex; align-items:center; gap:12px; font-weight:600; font-size:15px; letter-spacing:.01em; }
.lg-brand img, .lg-brand .lg-glyph { width:34px; height:34px; border-radius:8px; object-fit:cover; }
.lg-glyph { display:grid; place-items:center; background:var(--accent-hi); color:var(--ink); font-weight:700; }
.lg-tag { margin-left:auto; font-family:'IBM Plex Mono', ui-monospace, monospace; font-size:10.5px; letter-spacing:.14em; text-transform:uppercase; color:var(--dmute);
  border:1px solid var(--dline); padding:5px 10px; border-radius:999px; white-space:nowrap; }
.lg-eyebrow { margin:0 0 18px; font-size:12px; letter-spacing:.16em; text-transform:uppercase; font-weight:600; color:#7fd6cc; }
.lg-h { font-family:'Newsreader', Georgia, 'Times New Roman', serif; font-weight:400; font-size:clamp(40px,4.4vw,64px); line-height:1.04; letter-spacing:-.015em; margin:0; max-width:11.5em; }
.lg-h i { color:#7fd6cc; }
.lg-sub { color:var(--dmute); font-size:15px; line-height:1.65; max-width:34em; margin:20px 0 0; }
.lg-trace { margin-top:38px; max-width:540px; border:1px solid var(--dline); border-radius:10px; background:rgba(6,16,15,.6); backdrop-filter:blur(6px);
  font-family:'IBM Plex Mono', ui-monospace, Consolas, monospace; font-size:12.5px; }
.lg-trace-bar { display:flex; align-items:center; gap:9px; padding:10px 15px; border-bottom:1px solid var(--dline); color:var(--dmute); font-size:11.5px; letter-spacing:.02em; }
.lg-live { width:7px; height:7px; border-radius:50%; background:var(--amber); box-shadow:0 0 0 0 rgba(212,148,44,.6); animation:lg-pulse 2s infinite; }
.lg-row { display:grid; grid-template-columns:18px 56px 1fr; gap:6px; padding:9px 15px; opacity:0; transform:translateY(4px); animation:lg-in .45s ease forwards; }
.lg-row .m { color:#7fd6cc; } .lg-row .v { color:var(--dmute); } .lg-row:last-child .m { color:var(--amber); }
.lg-foot { display:flex; flex-wrap:wrap; gap:10px 22px; color:var(--dmute); font-size:12px; line-height:1.5; }
.lg-foot span { display:inline-flex; align-items:center; gap:7px; }
.lg-foot span::before { content:''; width:5px; height:5px; border-radius:50%; background:#7fd6cc; }
.lg-form { position:relative; display:flex; align-items:center; justify-content:center; padding:40px 32px; min-width:0; overflow-y:auto; color:var(--text); }
.lg-root .lg-form { background:var(--paper) !important; }   /* style.css paints every main with var(--bg) !important */
.lg-formbox { width:100%; max-width:396px; margin:auto 0; background:var(--card); border:1px solid var(--line); border-radius:14px; padding:40px 36px 34px;
  box-shadow:0 1px 2px rgba(14,29,28,.05), 0 24px 48px -28px rgba(14,29,28,.35); }
.lg-kick { display:flex; align-items:center; gap:8px; margin:0 0 16px; font-size:11.5px; letter-spacing:.14em; text-transform:uppercase; font-weight:600; color:var(--accent); }
.lg-kick svg { width:14px; height:14px; }
.lg-title { font-family:'Newsreader', Georgia, serif; font-size:34px; font-weight:400; letter-spacing:-.01em; line-height:1.1; margin:0 0 8px; color:var(--text); }
.lg-lede { color:var(--mute); font-size:14px; margin:0 0 26px; line-height:1.55; }
.lg-label { display:block; font-size:13px; color:var(--text); margin:0 0 7px; font-weight:600; }
.lg-field { display:flex; align-items:stretch; border:1px solid #cdc7b8; border-radius:8px; background:var(--field); transition:border-color .15s, box-shadow .15s; }
.lg-field:hover { border-color:#b5ae9d; }
.lg-field:focus-within { border-color:var(--accent); box-shadow:0 0 0 3px rgba(15,118,110,.18); }
.lg-field input { flex:1; min-width:0; background:transparent; border:none; outline:none; color:var(--text); font-size:15px; padding:12px 13px; font-family:inherit; }
.lg-field input::placeholder { color:#9aa5a2; }
.lg-suffix { display:flex; align-items:center; padding:0 12px; font-family:'IBM Plex Mono', ui-monospace, monospace; font-size:12.5px; color:var(--mute); background:var(--soft); border-left:1px solid #cdc7b8; border-radius:0 7px 7px 0; white-space:nowrap; }
.lg-reveal { background:transparent; border:none; color:var(--accent); font-size:12.5px; font-weight:600; cursor:pointer; padding:0 13px; }
.lg-reveal:hover { color:var(--accent-dk); }
.lg-hint { font-size:12px; color:var(--mute); margin:7px 0 18px; min-height:16px; }
.lg-hint b { color:var(--text); font-weight:600; }
.lg-btn { width:100%; margin-top:24px; padding:13px; border:none; border-radius:8px; background:var(--accent); color:#fff; font-weight:600; font-size:14.5px; letter-spacing:.01em; cursor:pointer; transition:background .15s, transform .1s; }
.lg-btn:hover:not(:disabled) { background:var(--accent-dk); } .lg-btn:active:not(:disabled) { transform:scale(.99); } .lg-btn:disabled { opacity:.45; cursor:not-allowed; }
.lg-btn:focus-visible, .lg-reveal:focus-visible, .lg-link:focus-visible { outline:2px solid var(--accent-hi); outline-offset:2px; }
.lg-link { width:100%; margin-top:12px; background:transparent; border:none; color:var(--accent); font-size:13px; font-weight:600; cursor:pointer; }
.lg-code { display:block; word-break:break-all; padding:9px 11px; border:1px solid var(--line); border-radius:6px; background:var(--soft); color:var(--text); font-family:'IBM Plex Mono', ui-monospace, monospace; font-size:12px; user-select:text; }
.lg-err { margin-top:18px; padding:11px 13px; border-radius:8px; font-size:13px; color:var(--danger); background:var(--dangerbg); border:1px solid var(--dangerline); }
.lg-secure { margin:22px 0 0; padding-top:18px; border-top:1px solid var(--line); display:flex; align-items:center; gap:8px; font-size:11.5px; color:var(--mute); }
.lg-secure svg { width:13px; height:13px; color:var(--accent); flex-shrink:0; }
@keyframes lg-in { to { opacity:1; transform:none; } }
@keyframes lg-pulse { 70% { box-shadow:0 0 0 8px rgba(212,148,44,0); } 100% { box-shadow:0 0 0 0 rgba(212,148,44,0); } }
@media (max-width: 900px) { .lg-root { grid-template-columns:1fr; } .lg-stage { display:none; } .lg-form { padding:24px 16px; } .lg-formbox { padding:30px 24px 26px; } }
@media (prefers-reduced-motion: reduce) { .lg-row { animation:none; opacity:1; transform:none; } .lg-live { animation:none; } }
`;

export const LoginPage: React.FC = () => {
  const { setAuthenticatedUser } = useAppStore();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPw, setShowPw] = useState(false);
  const [code, setCode] = useState('');
  const [ticket, setTicket] = useState<string | null>(null);
  const [enroll, setEnroll] = useState<{ secret: string; uri: string; codes: string[] } | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [logoOk, setLogoOk] = useState(true);

  const loginName = withOrgDomain(username);

  const finish = async (user?: any) => {
    if (user) {
      setAuthenticatedUser(user);
      return;
    }
    const me = await api.auth.getMe();
    if (me) setAuthenticatedUser(me);
    else setError('Signed in, but the profile could not be loaded. Reload the page.');
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setError('');
    setLoading(true);
    const result = await api.auth.login(loginName, password);
    setLoading(false);
    if (!result.ok) {
      setError(result.error || 'Sign in failed');
      return;
    }
    if (result.mfaRequired) {
      setTicket(result.ticket || null);
      if (result.enrollRequired) {
        // MFA is enforced and nothing is enrolled yet: start enrolment over the ticket
        try {
          const res = await apiFetch('/auth/mfa/ticket/enroll', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ticket: result.ticket }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            setError(data.detail || 'Could not start two-step setup');
            setTicket(null);
          } else {
            setEnroll({ secret: data.secret || '', uri: data.otpauth_uri || '', codes: data.backup_codes || [] });
          }
        } catch (_) {
          setError('Network error. Is the server running?');
          setTicket(null);
        }
      }
      return;
    }
    await finish(result.user);
  };

  const handleCode = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!code.trim() || !ticket) return;
    setError('');
    setLoading(true);
    try {
      const res = await apiFetch(enroll ? '/auth/mfa/ticket/confirm' : '/auth/mfa/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticket, code: code.trim() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(data.detail || 'Code rejected');
        if (res.status === 401 && /sign in again/.test(data.detail || '')) {
          setTicket(null);
          setCode('');
        }
      } else {
        await finish();
      }
    } catch (_) {
      setError('Network error. Is the server running?');
    }
    setLoading(false);
  };

  return (
    <div className="lg-root select-none">
      <style>{CSS}</style>

      <div className="lg-stage">
        <div className="lg-brand">
          {logoOk ? (
            <img src="/static/logo/logo.jpg" alt="" onError={() => setLogoOk(false)} />
          ) : (
            <span className="lg-glyph">{(env.projectName || 'L').charAt(0)}</span>
          )}
          {env.projectName}
          <span className="lg-tag">Internal platform</span>
        </div>

        <div>
          <p className="lg-eyebrow">Agents you can audit</p>
          <h1 className="lg-h">Give it the task. <i>Come back to the result.</i></h1>
          <p className="lg-sub">Agents that read your files, run your tools and check their own work, on hardware your team controls.</p>
          <div className="lg-trace" aria-hidden="true">
            <div className="lg-trace-bar"><span className="lg-live" />agent run, no one at the keyboard</div>
            {TRACE.map((r, i) => (
              <div key={r.verb} className="lg-row" style={{ animationDelay: `${0.5 + i * 0.7}s` }}>
                <span className="m">{r.mark}</span><span className="v">{r.verb}</span><span>{r.text}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="lg-foot">
          <span>Authorized users only</span>
          <span>Every action is logged</span>
          <span>Data stays on your infrastructure</span>
        </div>
      </div>

      <main className="lg-form">
        <div className="lg-formbox" id="login-box">
          <p className="lg-kick">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="4" y="11" width="16" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" />
            </svg>
            Secure sign-in
          </p>
          <h2 className="lg-title">{ticket ? 'Verify it is you' : 'Sign in'}</h2>
          <p className="lg-lede">
            {ticket ? 'Enter the 6-digit code from your authenticator app. A backup code works too.' : 'Use your work account to pick up where you left off.'}
          </p>

          {!ticket ? (
            <form onSubmit={handleSubmit}>
              <label htmlFor="username" className="lg-label">{env.orgDomain ? 'Work email' : 'Username'}</label>
              <div className="lg-field">
                <input
                  id="username" name="username" type="text" required autoFocus autoComplete="username" spellCheck={false}
                  placeholder={env.orgDomain ? 'first.last' : 'username'} value={username} onChange={(e) => setUsername(e.target.value)}
                />
                {env.orgDomain && !username.includes('@') && <span className="lg-suffix">@{env.orgDomain}</span>}
              </div>
              <div className="lg-hint">
                {env.orgDomain && (username.trim() ? <>Signing in as <b>{loginName}</b></> : <>Type only the part before @{env.orgDomain}</>)}
              </div>

              <label htmlFor="password" className="lg-label">Password</label>
              <div className="lg-field">
                <input
                  id="password" name="password" type={showPw ? 'text' : 'password'} required autoComplete="current-password"
                  value={password} onChange={(e) => setPassword(e.target.value)}
                />
                <button type="button" className="lg-reveal" onClick={() => setShowPw((v) => !v)} aria-pressed={showPw}>
                  {showPw ? 'Hide' : 'Show'}
                </button>
              </div>

              <button id="login-submit" type="submit" className="lg-btn" disabled={loading || !username.trim() || !password}>
                {loading ? 'Signing in…' : 'Sign in'}
              </button>
            </form>
          ) : (
            <form onSubmit={handleCode}>
              {enroll && (
                <div style={{ marginBottom: 18, fontSize: 12.5, lineHeight: 1.5 }}>
                  <p style={{ margin: '0 0 10px', color: 'var(--mute)' }}>
                    Your organization requires two-step verification. Add this key to your authenticator app, save the backup codes, then enter a code below.
                  </p>
                  <div className="lg-label">Setup key</div>
                  <code className="lg-code">{enroll.secret}</code>
                  <div className="lg-label" style={{ marginTop: 10 }}>Setup link (for apps that accept it)</div>
                  <code className="lg-code" style={{ fontSize: 10.5 }}>{enroll.uri}</code>
                  <div className="lg-label" style={{ marginTop: 10 }}>Backup codes. Each works once.</div>
                  <code className="lg-code">{enroll.codes.join('   ')}</code>
                </div>
              )}
              <label htmlFor="mfa-code" className="lg-label">Authentication code</label>
              <div className="lg-field">
                <input
                  id="mfa-code" type="text" inputMode="numeric" autoComplete="one-time-code" autoFocus placeholder="123456"
                  value={code} onChange={(e) => setCode(e.target.value)}
                  style={{ letterSpacing: 6, fontSize: 20, textAlign: 'center', fontFamily: "'IBM Plex Mono', monospace" }}
                />
              </div>
              <button type="submit" className="lg-btn" disabled={loading || !code.trim()}>{loading ? 'Verifying…' : enroll ? 'Turn on and sign in' : 'Verify'}</button>
              <button type="button" className="lg-link" onClick={() => { setTicket(null); setEnroll(null); setCode(''); setError(''); }}>Back to sign in</button>
            </form>
          )}

          {error && <div id="login-err" role="alert" className="lg-err">{error}</div>}

          <div className="lg-secure">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 2l8 4v6c0 5-3.5 9-8 10-4.5-1-8-5-8-10V6l8-4z" /><path d="M9 12l2 2 4-4" />
            </svg>
            Authorized users only. Activity on this system is recorded.
          </div>
        </div>
      </main>
    </div>
  );
};
