import React from 'react';
import { useAppStore } from '../../stores/useAppStore';

// Copy for requests that are not a plain shell command (mirrors showPermModal in static/js/workspace.js).
const KIND_COPY: Record<string, { title: string; lead: string }> = {
  browser_eval: { title: '🌐 Run JavaScript in the agent’s browser', lead: 'The agent wants to run this script in its test browser:' },
  browser_open: { title: '🌐 Open a website', lead: 'The agent wants to open this site in its test browser:' },
  device: { title: '📱 Device action', lead: 'The agent wants to do this on a phone / emulator:' },
  edit: { title: '✏️ Edit a file', lead: 'The agent wants to change this file:' },
  rule: { title: '🛡 Policy check', lead: 'A rule set by your administrator needs your approval for this action:' },
  media: { title: '💳 Cloud media — may cost money', lead: 'The agent wants to make this with a paid cloud service:' },
  mcp: { title: '🔌 Connector action', lead: 'The agent wants a connected service to do this:' },
  python: { title: '🔑 Run Python code', lead: 'The agent wants to run this code:' },
};

export const PermissionModal: React.FC = () => {
  const { permRequest, answerPermission, appMode, hasPerm } = useAppStore();
  if (!permRequest) return null;

  const { cmd, kind, saveable, tainted } = permRequest;
  const copy = KIND_COPY[kind] || { title: '🔑 Shell Execution Permission', lead: 'The agent wants to run a shell command:' };
  const deviceKind = ['browser_eval', 'browser_open', 'device', 'edit', 'rule'].includes(kind);
  const chained = kind !== 'python' && kind !== 'media' && kind !== 'mcp' && !deviceKind && saveable === false;
  const onceOnly = deviceKind || kind === 'python' || kind === 'media' || kind === 'mcp' || chained;
  // code / device actions can only be allowed for the rest of THIS run (nothing saved)
  const runScoped = deviceKind || kind === 'python';
  const firstWord = (cmd.trim().split(/\s+/)[0] || '*') + ' *';

  let note: React.ReactNode;
  if (deviceKind) note = 'Allow it once, or for the rest of this run. Nothing is remembered after the run ends.';
  else if (kind === 'media' || kind === 'mcp') note = 'Allow it just this once, or deny. The agent asks again next time.';
  else if (kind === 'python') note = 'Code is never remembered. Allow it once, or allow Python scripts for the rest of this run (nothing is saved after the run ends).';
  else if (chained) note = 'This command chains steps or uses special options (& | > ;), so it can only be allowed once. Run each step as its own command to be able to remember it.';
  else {
    note = (
      <>
        Permission scopes for <code>{firstWord}</code>:<br />
        • <b>Just once:</b> Run this command now without saving.<br />
        • <b>For this project:</b> Automatically permit in the active project.<br />
        • <b>Always allow for me:</b> Permit for your account, across all your projects.<br />
        • <b>Always:</b> Allow globally for every user (admin-visible setting).
      </>
    );
  }

  return (
    <div id="perm-modal" data-app-mode={appMode}>
      <div id="perm-box">
        <h2>
          {copy.title}{' '}
          <button className="rep-close" id="perm-close-x" onClick={() => answerPermission('deny')}>✕</button>
        </h2>
        {tainted && (
          <p className="dim" style={{ margin: '4px 0', fontSize: 11, color: 'var(--amber)' }}>
            ⚠ This action was shaped by content from the web or a file — review it carefully.
          </p>
        )}
        <p className="dim" style={{ margin: '8px 0 4px', fontSize: 11 }}>{copy.lead}</p>
        <pre id="perm-cmd" className="mono" style={{ background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8, padding: '10px 12px', fontSize: 12, whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: '0 0 6px' }}>
          {cmd}
        </pre>
        <p className="dim" id="perm-note" style={{ fontSize: 10.5, margin: '0 0 12px' }}>{note}</p>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <button className="btn ghost" id="perm-deny" style={{ width: 'auto', margin: 0, padding: '7px 14px' }} onClick={() => answerPermission('deny')}>✕ Deny</button>
          <button className="btn accent" id="perm-allow" style={{ width: 'auto', margin: 0, padding: '7px 15px', fontWeight: 600 }} onClick={() => answerPermission('allow')}>▶ Just once</button>
          {(!onceOnly || runScoped) && (
            <button className="btn ghost" id="perm-project" style={{ width: 'auto', margin: 0, padding: '7px 14px', color: 'var(--amber)', borderColor: 'var(--amber)' }} onClick={() => answerPermission('project')}>
              {runScoped ? '⏱ For this run' : '📁 For this project'}
            </button>
          )}
          {!onceOnly && (
            <>
              <button className="btn ghost" id="perm-user" style={{ width: 'auto', margin: 0, padding: '7px 14px', color: 'var(--blue)', borderColor: 'var(--blue)' }} onClick={() => answerPermission('user')}>👤 Always allow for me</button>
              {hasPerm('settings.shell.configure') && <button className="btn ghost" id="perm-always" style={{ width: 'auto', margin: 0, padding: '7px 14px', color: 'var(--green)', borderColor: 'var(--green)' }} onClick={() => answerPermission('always')}>✓ Always (globally)</button>}
            </>
          )}
        </div>
      </div>
    </div>
  );
};
