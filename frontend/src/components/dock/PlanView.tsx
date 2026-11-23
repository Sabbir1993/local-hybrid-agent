import React, { useMemo } from 'react';
import { Check, Circle, X } from 'lucide-react';
import { useAppStore } from '../../stores/useAppStore';
import { latestPlanItems } from '../../stores/agentEvents';

type PlanStatus = 'done' | 'failed' | 'in_progress' | 'pending' | string;

const ordinal = (item: any, index: number) => {
  const n = Number(item?.ord);
  return Number.isFinite(n) && n > 0 ? String(n).padStart(2, '0') : String(index + 1).padStart(2, '0');
};

/** The agent's live checklist: the latest `plan` snapshot found in the conversation. */
export const PlanView: React.FC = () => {
  const messages = useAppStore((s) => s.messages);
  const items = useMemo(() => latestPlanItems(messages), [messages]);

  if (!items.length) {
    return (
      <div className="plan-empty">
        <div className="plan-empty-rail" aria-hidden="true">
          <span /><span /><span />
        </div>
        <div className="plan-empty-copy">
          <span className="plan-empty-kicker">Standby</span>
          <h2>Waiting for a task</h2>
          <p>When the agent starts a multi-step task, each step appears here as it runs.</p>
        </div>
      </div>
    );
  }

  const total = items.length;
  const done = items.filter((i: any) => i.status === 'done').length;
  const failed = items.filter((i: any) => i.status === 'failed').length;
  const cur = items.find((i: any) => i.status === 'in_progress') as any | undefined;
  const settled = done + failed;
  const pct = total ? Math.round((settled / total) * 100) : 0;

  const headline = cur
    ? `Step ${ordinal(cur, items.indexOf(cur))} in progress`
    : failed
      ? `${failed} step${failed === 1 ? '' : 's'} need attention`
      : 'All steps complete';

  return (
    <div className="plan-shell">
      <header className="plan-head">
        <div className="plan-head-row">
          <span className="plan-head-label">Execution plan</span>
          <span className="plan-head-count">{settled}<span>/{total}</span></span>
        </div>
        <div
          className="plan-progress"
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Plan progress"
        >
          <div
            className={`plan-progress-fill${failed ? ' has-failed' : ''}`}
            style={{ width: `${pct}%` }}
          />
        </div>
        <div className={`plan-head-status${cur ? ' is-live' : failed ? ' is-failed' : ''}`}>
          {headline}
        </div>
      </header>

      <ol className="plan-steps">
        {items.map((it: any, i: number) => {
          const st: PlanStatus = it.status || 'pending';
          const isLast = i === total - 1;
          return (
            <li key={`${ordinal(it, i)}-${i}`} className={`plan-step is-${st}${isLast ? ' is-last' : ''}`}>
              <div className="plan-step-rail" aria-hidden="true">
                <span className="plan-step-node">
                  {st === 'done' ? <Check size={12} strokeWidth={2.6} />
                    : st === 'failed' ? <X size={12} strokeWidth={2.6} />
                      : st === 'in_progress' ? null
                        : <Circle size={7} strokeWidth={2} />}
                </span>
              </div>
              <div className="plan-step-body">
                <div className="plan-step-top">
                  <span className="plan-step-index">{ordinal(it, i)}</span>
                  <span className="plan-step-text">{it.text || ''}</span>
                </div>
                {it.note ? <div className="plan-step-note">{it.note}</div> : null}
              </div>
            </li>
          );
        })}
      </ol>

      {failed > 0 ? (
        <div className="plan-foot">
          Review the failed steps and tell the agent how to continue.
        </div>
      ) : null}
    </div>
  );
};
