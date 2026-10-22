import React, { useMemo } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { latestPlanItems } from '../../stores/agentEvents';

/** The agent's live checklist: the latest `plan` snapshot found in the conversation. */
export const PlanView: React.FC = () => {
  const messages = useAppStore((s) => s.messages);
  const items = useMemo(() => latestPlanItems(messages), [messages]);

  if (!items.length) {
    return (
      <div style={{ padding: '32px 16px', textAlign: 'center', color: 'var(--dim)', fontSize: '12.5px' }}>
        <div style={{ fontSize: 24, marginBottom: 8 }}>📋</div>
        <b>No active plan</b>
        <p style={{ marginTop: 6, fontSize: '11.5px' }}>When the agent starts a multi-step task, the plan will be tracked live here.</p>
      </div>
    );
  }

  const total = items.length;
  const done = items.filter((i) => i.status === 'done').length;
  const failed = items.filter((i) => i.status === 'failed').length;
  const cur = items.find((i) => i.status === 'in_progress');
  const pct = total ? Math.round(((done + failed) / total) * 100) : 0;

  return (
    <>
      <div className="plan-doc-header">
        <div className="plan-doc-title">Plan: Multi-Step Task Execution</div>
        <div style={{ fontSize: 12, color: 'var(--dim)' }}>
          {cur ? `Step ${cur.ord || items.indexOf(cur) + 1} of ${total} · ` : ''}
          <b>{done}/{total}</b> completed {failed ? <>· <span style={{ color: 'var(--red)' }}>{failed} failed</span></> : null}
        </div>
        <div className="plan-doc-progress-bar">
          <div className="plan-doc-progress-fill" style={{ width: `${pct}%`, ...(failed ? { background: 'var(--amber)' } : {}) }} />
        </div>
      </div>

      <div className="plan-doc-section-title">Checklist &amp; Execution Steps</div>
      <div className="plan-doc-steps-list">
        {items.map((it, i) => {
          const st = it.status || 'pending';
          const mark = st === 'done' ? '✅' : st === 'failed' ? '❌' : st === 'in_progress' ? '⏳' : '☐';
          return (
            <div key={i} className={`plan-doc-step-item st-${st}`}>
              <span className="plan-doc-step-mark">{mark}</span>
              <div style={{ flex: 1 }}>
                <span style={{ fontWeight: st === 'in_progress' ? 600 : 'normal' }}>{it.text || ''}</span>
                {it.note ? <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 2 }}>↳ {it.note}</div> : null}
              </div>
            </div>
          );
        })}
      </div>

      <div className="plan-doc-section-title">Execution Mode</div>
      <div style={{ fontSize: '11.5px', color: 'var(--dim)', lineHeight: 1.5, background: 'var(--panel2)', padding: '10px 12px', borderRadius: 6, border: '1px solid var(--border-subtle)' }}>
        Each step is executed atomically. Tools verify outputs and save diff snapshots after every file edit.
      </div>
    </>
  );
};
