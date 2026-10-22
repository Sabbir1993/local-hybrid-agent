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

const CSS = `
.lg-root { --ink:#0b1020; --ink2:#111833; --bone:#eceff6; --mute:#8d97b3; --line:rgba(236,239,246,.12); --signal:#7c9cff; --warm:#ffb454;
  display:grid; grid-template-columns: minmax(0,1.15fr) minmax(380px,.85fr); width:100%; height:100vh; height:100dvh; background:var(--ink); color:var(--bone);
  font-family:'Inter',system-ui,sans-serif; overflow:hidden; }
.lg-stage { position:relative; display:flex; flex-direction:column; justify-content:space-between; padding:44px clamp(32px,5vw,80px);
  background:
   linear-gradient(var(--line) 1px, transparent 1px) 0 0/100% 56px,
   radial-gradient(1000px 520px at 15% 105%, rgba(124,156,255,.22), transparent 62%),
   var(--ink2);
  border-right:1px solid var(--line); min-width:0; }
.lg-brand { display:flex; align-items:center; gap:12px; font-weight:600; font-size:14px; letter-spacing:.2px; }
.lg-brand img, .lg-brand .lg-glyph { width:32px; height:32px; border-radius:8px; object-fit:cover; }
.lg-glyph { display:grid; place-items:center; background:var(--signal); color:var(--ink); font-weight:800; }
.lg-h { font-family:'Instrument Serif', Georgia, serif; font-weight:400; font-size:clamp(40px,4.6vw,68px); line-height:1.02; letter-spacing:-.02em; margin:0; max-width:11em; }
.lg-h i { color:var(--signal); }
.lg-sub { color:var(--mute); font-size:15px; line-height:1.6; max-width:34em; margin:18px 0 0; }
.lg-trace { margin-top:34px; max-width:520px; border:1px solid var(--line); border-radius:6px; background:rgba(8,12,26,.72); backdrop-filter:blur(6px);
  font-family:'JetBrains Mono', ui-monospace, Consolas, monospace; font-size:12.5px; }
.lg-trace-bar { display:flex; align-items:center; gap:8px; padding:9px 14px; border-bottom:1px solid var(--line); color:var(--mute); font-size:11.5px; }
.lg-live { width:7px; height:7px; border-radius:50%; background:var(--warm); box-shadow:0 0 0 0 rgba(255,180,84,.6); animation:lg-pulse 2s infinite; }
.lg-row { display:grid; grid-template-columns:18px 52px 1fr; gap:6px; padding:8px 14px; opacity:0; transform:translateY(4px); animation:lg-in .45s ease forwards; }
.lg-row .m { color:var(--signal); } .lg-row .v { color:var(--mute); } .lg-row:last-child .m { color:var(--warm); }
.lg-foot { color:var(--mute); font-size:12px; max-width:34em; line-height:1.5; }
.lg-form { display:flex; align-items:flex-start; justify-content:center; padding:32px; min-width:0; overflow-y:auto; }
.lg-form > .lg-formbox { margin:auto 0; }
.lg-formbox { width:100%; max-width:360px; }
.lg-title { font-family:'Instrument Serif', Georgia, serif; font-size:36px; font-weight:400; letter-spacing:-.01em; margin:0 0 6px; }
.lg-lede { color:var(--mute); font-size:14px; margin:0 0 26px; line-height:1.5; }
.lg-label { display:block; font-size:13px; color:var(--bone); margin:0 0 7px; font-weight:500; }
.lg-field { display:flex; align-items:stretch; border:1px solid var(--line); border-radius:8px; background:rgba(255,255,255,.03); transition:border-color .15s, box-shadow .15s; }
.lg-field:focus-within { border-color:var(--signal); box-shadow:0 0 0 3px rgba(124,156,255,.2); }
.lg-field input { flex:1; min-width:0; background:transparent; border:none; outline:none; color:var(--bone); font-size:15px; padding:12px 13px; font-family:inherit; }
.lg-field input::placeholder { color:rgba(141,151,179,.55); }
.lg-suffix { display:flex; align-items:center; padding:0 12px; font-family:'JetBrains Mono', ui-monospace, monospace; font-size:12.5px; color:var(--mute); border-left:1px solid var(--line); white-space:nowrap; }
.lg-reveal { background:transparent; border:none; color:var(--mute); font-size:12.5px; cursor:pointer; padding:0 13px; }
.lg-reveal:hover { color:var(--bone); }
.lg-hint { font-size:12px; color:var(--mute); margin:7px 0 18px; min-height:16px; }
.lg-hint b { color:var(--bone); font-weight:500; }
.lg-btn { width:100%; margin-top:24px; padding:13px; border:none; border-radius:8px; background:var(--bone); color:var(--ink); font-weight:600; font-size:14.5px; cursor:pointer; transition:background .15s, transform .1s; }
.lg-btn:hover:not(:disabled) { background:#fff; } .lg-btn:active:not(:disabled) { transform:scale(.99); } .lg-btn:disabled { opacity:.4; cursor:not-allowed; }
.lg-btn:focus-visible, .lg-reveal:focus-visible, .lg-link:focus-visible { outline:2px solid var(--signal); outline-offset:2px; }
.lg-link { width:100%; margin-top:12px; background:transparent; border:none; color:var(--mute); font-size:13px; cursor:pointer; }
.lg-code { display:block; word-break:break-all; padding:8px 10px; border:1px solid var(--line); border-radius:6px; background:rgba(255,255,255,.03); font-family:'JetBrains Mono', ui-monospace, monospace; font-size:12px; user-select:text; }
.lg-err { margin-top:18px; padding:10px 12px; border-radius:8px; font-size:13px; color:#ffb3ad; background:rgba(255,99,90,.09); border:1px solid rgba(255,99,90,.3); }
@keyframes lg-in { to { opacity:1; transform:none; } }
@keyframes lg-pulse { 70% { box-shadow:0 0 0 8px rgba(255,180,84,0); } 100% { box-shadow:0 0 0 0 rgba(255,180,84,0); } }
@media (max-width: 900px) { .lg-root { grid-template-columns:1fr; } .lg-stage { display:none; } }
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
        </div>

        <div>
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

        <div className="lg-foot">Authorized users only. Every action on this system is logged.</div>
      </div>

      <main className="lg-form">
        <div className="lg-formbox" id="login-box">
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
                  style={{ letterSpacing: 6, fontSize: 20, textAlign: 'center', fontFamily: "'JetBrains Mono', monospace" }}
                />
              </div>
              <button type="submit" className="lg-btn" disabled={loading || !code.trim()}>{loading ? 'Verifying…' : enroll ? 'Turn on and sign in' : 'Verify'}</button>
              <button type="button" className="lg-link" onClick={() => { setTicket(null); setEnroll(null); setCode(''); setError(''); }}>Back to sign in</button>
            </form>
          )}

          {error && <div id="login-err" role="alert" className="lg-err">{error}</div>}
        </div>
      </main>
    </div>
  );
};
