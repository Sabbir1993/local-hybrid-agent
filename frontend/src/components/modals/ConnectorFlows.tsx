import React, { useState, useEffect, useRef, useCallback } from 'react';
import { apiFetch } from '../../services/api';

/* ---------- shared helpers ---------- */

async function errMsg(r: Response, fallback: string): Promise<string> {
  const j = await r.json().catch(() => ({} as any));
  const d = j.error || j.detail;
  return typeof d === 'string' && d ? d : `${fallback} (HTTP ${r.status})`;
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

const sleep = (ms: number) => new Promise<void>((res) => setTimeout(res, ms));

/* ---------- Connector OAuth ---------- */

export interface OAuthItem {
  id: string;
  title: string;
  installed?: boolean;
  installed_scope?: string;
  oauth?: boolean;
  signed_in?: boolean;
  client?: string; // 'none' | 'auto' | 'preregistered'
  needs_client?: boolean;
  has_client?: boolean;
}

const POLL_MS = 2000;
const POLL_TIMEOUT_MS = 330000;

/** Start -> open sign-in window -> poll status until done. Returns connect/disconnect actions and state. */
export function useConnectorOAuth(onChanged: () => void | Promise<void>) {
  const [connecting, setConnecting] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [links, setLinks] = useState<Record<string, string>>({});
  const alive = useRef(true);
  // in-flight guard (a second click or the auto sign-in must not start a second flow) and a fresh onChanged
  const connectingRef = useRef<Record<string, boolean>>({});
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const setErr = (id: string, msg: string) => setErrors((p) => ({ ...p, [id]: msg }));
  const setNote = (id: string, msg: string) => setNotes((p) => ({ ...p, [id]: msg }));

  const connect = useCallback(
    async (id: string, scope: string) => {
      if (connectingRef.current[id]) return;
      connectingRef.current[id] = true;
      setConnecting((p) => ({ ...p, [id]: true }));
      setErr(id, '');
      setNote(id, '');
      setLinks((p) => ({ ...p, [id]: '' }));
      try {
        const sr = await apiFetch(`/mcp/servers/${encodeURIComponent(id)}/oauth/start`, jsonInit('POST', { scope }));
        if (!sr.ok) throw new Error(await errMsg(sr, 'Could not start sign-in'));
        const start = await sr.json();
        if (start.mode === 'redirect' && /^https:\/\//.test(start.auth_url || '')) {
          // this runs after awaits, so a popup blocker may refuse it: always offer the link as well
          const w = window.open(start.auth_url, '_blank', 'noopener');
          setLinks((p) => ({ ...p, [id]: start.auth_url }));
          setNote(
            id,
            w
              ? 'Finish signing in in the window that just opened.'
              : 'Your browser blocked the sign-in window. Use the "Open sign-in page" link to continue.',
          );
        } else {
          setNote(id, start.message || 'Finish signing in in the browser window the companion opened.');
        }
        const t0 = Date.now();
        for (;;) {
          await sleep(POLL_MS);
          if (!alive.current) return;
          const pr = await apiFetch(
            `/mcp/servers/${encodeURIComponent(id)}/oauth/status?scope=${encodeURIComponent(scope)}`,
            { background: true },
          );
          if (!pr.ok) throw new Error(await errMsg(pr, 'Could not check sign-in status'));
          const st = await pr.json();
          if (st.state === 'done') break;
          if (st.state === 'error') throw new Error(st.error || 'Sign-in failed');
          if (Date.now() - t0 > POLL_TIMEOUT_MS) throw new Error('Timed out waiting for the sign-in');
        }
        if (alive.current) { setNote(id, 'Connected.'); setLinks((p) => ({ ...p, [id]: '' })); }
      } catch (err: any) {
        if (alive.current) {
          setNote(id, '');
          setLinks((p) => ({ ...p, [id]: '' }));
          setErr(id, 'Sign-in failed: ' + (err?.message || 'unknown error'));
        }
      } finally {
        delete connectingRef.current[id];
        if (alive.current) {
          setConnecting((p) => {
            const n = { ...p };
            delete n[id];
            return n;
          });
          try {
            await onChangedRef.current();
          } catch {
            /* ignore refresh errors */
          }
        }
      }
    },
    [],
  );

  const disconnect = useCallback(
    async (id: string, scope: string) => {
      setErr(id, '');
      setNote(id, '');
      try {
        const r = await apiFetch(`/mcp/servers/${encodeURIComponent(id)}/oauth/disconnect`, jsonInit('POST', { scope }));
        if (!r.ok) throw new Error(await errMsg(r, 'Could not sign out'));
        setNote(id, 'Signed out.');
        await onChangedRef.current();
      } catch (err: any) {
        setErr(id, 'Could not sign out: ' + (err?.message || 'unknown error'));
      }
    },
    [],
  );

  return { connecting, errors, notes, links, connect, disconnect };
}

interface PanelProps {
  item: OAuthItem;
  isAdmin: boolean;
  oauth: ReturnType<typeof useConnectorOAuth>;
  onChanged: () => void | Promise<void>;
}

/** Connect / reconnect / sign out, plus the admin-only preregistered client form. */
export const ConnectorOAuthPanel: React.FC<PanelProps> = ({ item, isAdmin, oauth, onChanged }) => {
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [saving, setSaving] = useState(false);
  const [clientErr, setClientErr] = useState('');
  const [clientNote, setClientNote] = useState('');
  const [confirmClear, setConfirmClear] = useState(false);

  if (!item.oauth || !item.installed) return null;

  const scope = item.installed_scope || 'user';
  const canSignIn = scope === 'user' || isAdmin;
  const busy = !!oauth.connecting[item.id];
  const showClient = isAdmin && (item.client === 'preregistered' || item.client === 'auto');

  const saveClient = async () => {
    const cid = clientId.trim();
    const secret = clientSecret.trim();
    setClientErr('');
    setClientNote('');
    if (!cid && !secret) {
      setClientErr('Paste the client ID and secret first.');
      return;
    }
    if (!cid && !item.has_client) {
      setClientErr('The client ID is required.');
      return;
    }
    setSaving(true);
    try {
      const r = await apiFetch(
        `/customize/connectors/${encodeURIComponent(item.id)}/oauth-client`,
        jsonInit('PUT', { client_id: cid, client_secret: secret }),
      );
      if (!r.ok) throw new Error(await errMsg(r, 'Could not save client'));
      setClientId('');
      setClientSecret('');
      setClientNote('Client saved. People can now press Connect account.');
      await onChanged();
    } catch (err: any) {
      setClientErr(err?.message || 'Could not save client');
    } finally {
      setSaving(false);
    }
  };

  const clearClient = async () => {
    setClientErr('');
    setClientNote('');
    setConfirmClear(false);
    try {
      const r = await apiFetch(`/customize/connectors/${encodeURIComponent(item.id)}/oauth-client`, { method: 'DELETE' });
      if (!r.ok) throw new Error(await errMsg(r, 'Could not remove client'));
      await onChanged();
    } catch (err: any) {
      setClientErr(err?.message || 'Could not remove client');
    }
  };

  return (
    <div style={{ marginTop: '16px' }}>
      <div className="cz-sec">
        <span>Account</span>
        {item.signed_in && <span className="cz-chip ok">Connected</span>}
      </div>
      {canSignIn ? (
        <div className="cz-actions">
          <button className="btn accent cz-act" disabled={busy} onClick={() => oauth.connect(item.id, scope)}>
            {busy ? 'Connecting…' : item.signed_in ? 'Reconnect account' : 'Connect account'}
          </button>
          {item.signed_in && (
            <button className="btn ghost cz-act" disabled={busy} onClick={() => oauth.disconnect(item.id, scope)}>
              Sign out
            </button>
          )}
        </div>
      ) : (
        <div className="cz-dim">Only an administrator can sign in to this shared connector.</div>
      )}
      {item.needs_client && !isAdmin && (
        <div className="cz-dim">This vendor needs a one-time app setup by an administrator before anyone can sign in.</div>
      )}
      {oauth.notes[item.id] && (
        <div className="cz-dim">
          {oauth.notes[item.id]}
          {oauth.links[item.id] && (
            <>
              {' '}
              <a href={oauth.links[item.id]} target="_blank" rel="noopener noreferrer">
                Open sign-in page ↗
              </a>
            </>
          )}
        </div>
      )}
      {oauth.errors[item.id] && (
        <div className="cz-banner warn" role="alert">
          {oauth.errors[item.id]}
        </div>
      )}

      {showClient && (
        <div className="cz-client" style={{ marginTop: '12px' }}>
          <div className="cz-note">
            {item.client === 'preregistered'
              ? 'This vendor does not register apps automatically.'
              : 'Sign-in tries automatic registration first; use this only if the vendor refuses it.'}{' '}
            Create an OAuth app with them, add the redirect address <code>http://127.0.0.1:[PORT]/callback</code> (or your{' '}
            <code>mcp_oauth_redirect_base</code> address when the vendor needs https), then paste its client ID and secret once. The
            secret stays in the OS keychain and users never see it.
          </div>
          <div className="cz-fork-row">
            <input
              type="text"
              className="cz-search"
              value={clientId}
              onChange={(e) => setClientId(e.target.value)}
              placeholder={item.has_client ? 'Client ID saved - paste a new one to replace' : 'Client ID'}
              autoComplete="off"
              spellCheck={false}
            />
            <input
              type="password"
              className="cz-search"
              value={clientSecret}
              onChange={(e) => setClientSecret(e.target.value)}
              placeholder={item.has_client ? 'Secret saved (leave blank to keep)' : 'Client secret'}
              autoComplete="new-password"
            />
          </div>
          <div className="cz-actions">
            <button className="btn accent cz-act" disabled={saving} onClick={saveClient}>
              {saving ? 'Saving…' : 'Save client'}
            </button>
            {item.has_client &&
              (confirmClear ? (
                <>
                  <span className="cz-dim">Signed-in people stay signed in, but no one can sign in again until a client is added.</span>
                  <button className="btn ghost cz-act" style={{ color: 'var(--red)' }} onClick={clearClient}>
                    Confirm remove
                  </button>
                  <button className="btn ghost cz-act" onClick={() => setConfirmClear(false)}>
                    Cancel
                  </button>
                </>
              ) : (
                <button className="btn ghost cz-act" onClick={() => setConfirmClear(true)}>
                  Remove client
                </button>
              ))}
          </div>
          {clientNote && <div className="cz-dim">{clientNote}</div>}
          {clientErr && (
            <div className="cz-banner warn" role="alert">
              {clientErr}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

/* ---------- Plugin marketplace + remote install ---------- */

interface MarketItem {
  name: string;
  title?: string;
  description?: string;
  author?: string;
  category?: string;
  version?: string;
  installed?: boolean;
  manifest_url?: string;
  code_url?: string;
  [k: string]: any;
}

interface RemoteReview {
  manifest: { name: string; title?: string; description?: string; version?: string; author?: string; category?: string; tools?: string[] };
  manifest_url: string;
  code_url?: string;
  code_sha256?: string;
  code_syntax?: string;
  code_preview?: string;
  name_taken?: boolean;
}

export const PluginMarketplace: React.FC<{ canInstall: boolean; onInstalled: () => void | Promise<void> }> = ({
  canInstall,
  onInstalled,
}) => {
  const [q, setQ] = useState('');
  const [items, setItems] = useState<MarketItem[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchErr, setSearchErr] = useState('');

  const [url, setUrl] = useState('');
  const [reviewing, setReviewing] = useState(false);
  const [review, setReview] = useState<RemoteReview | null>(null);
  const [reviewErr, setReviewErr] = useState('');
  const [ack, setAck] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [installErr, setInstallErr] = useState('');
  const [installedNote, setInstalledNote] = useState('');

  const search = async (e?: React.FormEvent) => {
    e?.preventDefault();
    setSearching(true);
    setSearchErr('');
    try {
      const r = await apiFetch(`/customize/plugins/registry?q=${encodeURIComponent(q.trim())}`);
      if (!r.ok) throw new Error(await errMsg(r, 'Registry search failed'));
      const j = await r.json();
      setItems(j.items || []);
    } catch (err: any) {
      setItems(null);
      setSearchErr(err?.message || 'Registry search failed');
    } finally {
      setSearching(false);
    }
  };

  const inspect = async (manifestUrl: string, codeUrl = '') => {
    const mu = manifestUrl.trim();
    if (!mu) return;
    setUrl(mu);
    setReviewing(true);
    setReview(null);
    setReviewErr('');
    setInstallErr('');
    setInstalledNote('');
    setAck(false);
    try {
      const r = await apiFetch('/customize/plugins/inspect-remote', jsonInit('POST', { manifest_url: mu, code_url: codeUrl }));
      if (!r.ok) throw new Error(await errMsg(r, 'Could not review the manifest'));
      setReview(await r.json());
    } catch (err: any) {
      setReviewErr(err?.message || 'Could not review the manifest');
    } finally {
      setReviewing(false);
    }
  };

  const install = async () => {
    if (!review || !ack) return;
    setInstalling(true);
    setInstallErr('');
    try {
      const r = await apiFetch(
        '/customize/plugins/install-remote',
        jsonInit('POST', {
          manifest_url: review.manifest_url,
          code_url: review.code_url || '',
          code_sha256: review.code_sha256 || '',
        }),
      );
      if (!r.ok) throw new Error(await errMsg(r, 'Install failed'));
      const j = await r.json().catch(() => ({} as any));
      setInstalledNote(
        `Installed '${review.manifest.name}'.` + (j.error ? ` It did not load: ${j.error}` : j.loaded === false ? ' It is installed but not loaded.' : ''),
      );
      setReview(null);
      setUrl('');
      setAck(false);
      setItems(null);
      await onInstalled();
    } catch (err: any) {
      setInstallErr(err?.message || 'Install failed');
    } finally {
      setInstalling(false);
    }
  };

  const m = review?.manifest;

  return (
    <div style={{ marginTop: '24px' }}>
      <div className="cz-banner warn">
        Remote plugins run as <b>in-process Python</b> on the server. Review the manifest and code preview before installing.
      </div>
      <div className="cz-sec">
        <span>Plugin Marketplace</span>
        <span className="cz-dim">external registry</span>
      </div>
      <form className="cz-reg-form" onSubmit={search}>
        <input
          type="search"
          placeholder="Search marketplace"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          maxLength={100}
        />
        <button className="btn ghost" type="submit" disabled={searching}>
          {searching ? 'Searching…' : 'Search'}
        </button>
      </form>
      {searchErr && (
        <div className="cz-empty" role="alert">
          {searchErr}
        </div>
      )}
      {items && (
        <>
          <div className="cz-sec">
            <span>Results</span>
            <span className="cz-count">{items.length}</span>
          </div>
          {items.length === 0 ? (
            <div className="cz-empty">No marketplace results.</div>
          ) : (
            <div className="cz-grid">
              {items.map((it, idx) => (
                <div key={`${it.name}-${idx}`} className="cz-card static">
                  <div className="cz-icon">🧩</div>
                  <div className="cz-main">
                    <div className="cz-name">
                      {it.title || it.name} {it.version && <span className="cz-dim">{it.version}</span>}
                      {it.installed && <span className="cz-chip mid">installed</span>}
                    </div>
                    <div className="cz-desc">{it.description}</div>
                    <div className="cz-by">{it.author ? `by ${it.author}` : it.name}</div>
                  </div>
                  {canInstall && !it.installed && it.manifest_url && (
                    <button
                      className="btn ghost cz-act"
                      disabled={reviewing}
                      onClick={() => inspect(it.manifest_url as string, it.code_url || '')}
                    >
                      Review
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}

      <div className="cz-sec">
        <span>Install from URL</span>
      </div>
      <form
        className="cz-reg-form"
        onSubmit={(e) => {
          e.preventDefault();
          inspect(url);
        }}
      >
        <input
          type="url"
          placeholder="Paste a plugin.json manifest URL to review"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          maxLength={500}
        />
        <button className="btn ghost" type="submit" disabled={reviewing}>
          {reviewing ? 'Fetching…' : 'Review URL'}
        </button>
      </form>
      {!canInstall && <div className="cz-dim">You can review a plugin here; installing needs the administrator install permission.</div>}
      {reviewErr && (
        <div className="cz-empty" role="alert">
          {reviewErr}
        </div>
      )}
      {installedNote && <div className="cz-dim">{installedNote}</div>}

      {review && m && (
        <div className="cz-detail" style={{ marginTop: '12px' }}>
          <div className="cz-dtitle">
            {m.title || m.name} {review.name_taken && <span className="cz-chip mid">installed</span>}
          </div>
          <div className="cz-desc full">{m.description}</div>
          <table className="cz-meta">
            <tbody>
              <tr>
                <th>Name</th>
                <td>
                  <code>{m.name}</code>
                </td>
              </tr>
              {m.version && (
                <tr>
                  <th>Version</th>
                  <td>{m.version}</td>
                </tr>
              )}
              {m.author && (
                <tr>
                  <th>Author</th>
                  <td>{m.author}</td>
                </tr>
              )}
              {m.tools && m.tools.length > 0 && (
                <tr>
                  <th>Tools</th>
                  <td>
                    {m.tools.map((t) => (
                      <code key={t} style={{ marginRight: '4px' }}>
                        {t}
                      </code>
                    ))}
                  </td>
                </tr>
              )}
              <tr>
                <th>Manifest</th>
                <td>
                  <code>{review.manifest_url}</code>
                </td>
              </tr>
              <tr>
                <th>Code</th>
                <td>
                  <code>{review.code_url || '(none)'}</code>
                </td>
              </tr>
              <tr>
                <th>Syntax</th>
                <td>{review.code_syntax || ''}</td>
              </tr>
              {review.code_sha256 && (
                <tr>
                  <th>SHA-256</th>
                  <td>
                    <code style={{ wordBreak: 'break-all' }}>{review.code_sha256}</code>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <div className="cz-sec">
            <span>Code preview</span>
          </div>
          <pre
            style={{
              background: 'var(--panel2)',
              padding: '12px',
              borderRadius: '8px',
              fontSize: '11.5px',
              overflow: 'auto',
              maxHeight: '320px',
              border: '1px solid var(--border)',
            }}
          >
            <code>{review.code_preview || '(no code)'}</code>
          </pre>

          {review.code_url && !review.name_taken && canInstall ? (
            <div style={{ marginTop: '12px' }}>
              <div className="cz-banner warn">
                You are about to install <b>{m.name}</b>
                {m.version ? ` ${m.version}` : ''} from <code>{review.code_url}</code>. Exactly the code shown above (pinned by SHA-256)
                will be written to the server&apos;s plugins folder and run in the server process.
              </div>
              <label className="cz-toggle">
                <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I reviewed the code and want to
                install this plugin
              </label>
              <div className="cz-actions">
                <button className="btn accent cz-act" disabled={!ack || installing} onClick={install}>
                  {installing ? 'Installing…' : `Install ${m.name}`}
                </button>
                <button
                  className="btn ghost cz-act"
                  disabled={installing}
                  onClick={() => {
                    setReview(null);
                    setAck(false);
                    setInstallErr('');
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="cz-actions">
              <span className="cz-dim">
                {review.name_taken
                  ? 'A plugin with this name is already installed.'
                  : !review.code_url
                    ? 'The manifest has no code URL, so there is nothing to install.'
                    : 'Installing needs the administrator install permission.'}
              </span>
              <button className="btn ghost cz-act" onClick={() => setReview(null)}>
                Clear review
              </button>
            </div>
          )}
          {installErr && (
            <div className="cz-banner warn" role="alert">
              {installErr}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
