import React, { useState, useEffect } from 'react';
import type { GrillQuestion } from '../../utils/questions';

interface Props {
  questions: GrillQuestion[];
  title?: string;
  submitLabel?: string;
  /** a later message exists: the card is read-only history */
  closed: boolean;
  answeredText?: string;
  /** returns false when the answer could not be sent (the card stays open) */
  onSubmit: (text: string) => boolean;
}

/** Decision card: pick an option (radio or checkbox) or type your own, then send the answers as one message. */
export const QuestionCard: React.FC<Props> = ({
  questions,
  title,
  submitLabel,
  closed,
  answeredText,
  onSubmit,
}) => {
  const [sel, setSel] = useState<Record<number, Set<number | 'other'>>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});
  const [done, setDone] = useState(false);
  const [error, setError] = useState('');
  const [isExpanded, setIsExpanded] = useState(false);

  // If answeredText is supplied from subsequent message, parse chosen answers so history restores them
  useEffect(() => {
    if (!answeredText) return;
    const newSel: Record<number, Set<number | 'other'>> = {};
    const newCustom: Record<number, string> = {};
    const lines = answeredText.split('\n');

    questions.forEach((q, qi) => {
      for (const line of lines) {
        const parts = line.split(':');
        const prefix = parts[0]?.trim();
        const ansPart = parts.slice(1).join(':').trim();
        if (
          prefix === String(q.qId) ||
          prefix === String(qi + 1) ||
          (q.qTitle && line.toLowerCase().includes(q.qTitle.toLowerCase()))
        ) {
          q.options.forEach((opt, oi) => {
            if (ansPart.includes(opt) || opt.includes(ansPart)) {
              if (!newSel[qi]) newSel[qi] = new Set();
              newSel[qi].add(oi);
            }
          });
          if (!newSel[qi] && ansPart) {
            newSel[qi] = new Set(['other']);
            newCustom[qi] = ansPart;
          }
        }
      }
    });

    if (Object.keys(newSel).length > 0) {
      setSel(newSel);
      setCustom(newCustom);
      setDone(true);
    }
  }, [answeredText, questions]);

  const locked = done || closed;
  const isFinished = done || closed || Boolean(answeredText);

  const toggle = (qi: number, key: number | 'other') => {
    if (locked) return;
    const q = questions[qi];
    setSel((prev) => {
      const cur = new Set(prev[qi] || []);
      if (q.multi) {
        if (cur.has(key)) cur.delete(key);
        else cur.add(key);
      } else {
        // single choice: picking the same row again clears it
        const had = cur.has(key);
        cur.clear();
        if (!had) cur.add(key);
      }
      return { ...prev, [qi]: cur };
    });
  };

  const submit = () => {
    if (locked) return;
    const lines: string[] = [];
    questions.forEach((q, qi) => {
      const s = sel[qi] || new Set();
      const chosen = q.options.filter((_, oi) => s.has(oi));
      const text = (custom[qi] || '').trim();
      if (text && s.has('other')) chosen.push(text);
      if (chosen.length) lines.push(`${q.qId}: ${chosen.join(', ')}`);
    });
    if (!lines.length) {
      setError('Select an option or write an answer first.');
      return;
    }
    if (!onSubmit(lines.join('\n'))) {
      setError('Could not send right now. Wait for the current answer to finish, then try again.');
      return;
    }
    setError('');
    setDone(true);
  };

  // Compile summary of chosen items
  const summaryList = questions
    .map((q, qi) => {
      const s = sel[qi] || new Set();
      const chosen = q.options.filter((_, oi) => s.has(oi));
      const text = (custom[qi] || '').trim();
      if (text && s.has('other')) chosen.push(text);
      return { qId: q.qId, title: q.qTitle, answers: chosen };
    })
    .filter((it) => it.answers.length > 0);

  // When finished/submitted, collapse into short summary by default!
  if (isFinished && !isExpanded) {
    return (
      <div className="grill-interactive-box submitted" style={{ padding: '10px 14px' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                width: 18,
                height: 18,
                borderRadius: '50%',
                background: 'rgba(34, 197, 94, 0.2)',
                color: '#4ade80',
                fontSize: 11,
                fontWeight: 700,
              }}
            >
              ✓
            </span>
            <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text)' }}>
              {title || 'Decisions recorded'}
            </span>
            <span style={{ fontSize: 11, color: 'var(--dim)', opacity: 0.8 }}>
              ({summaryList.length > 0 ? `${summaryList.length} answered` : 'Submitted'})
            </span>
          </div>
          <button
            type="button"
            onClick={() => setIsExpanded(true)}
            style={{
              background: 'transparent',
              border: '1px solid var(--border-subtle, rgba(255,255,255,0.12))',
              borderRadius: 4,
              padding: '2px 8px',
              color: 'var(--dim)',
              fontSize: 11,
              cursor: 'pointer',
            }}
          >
            Show details ▾
          </button>
        </div>

        {summaryList.length > 0 && (
          <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {summaryList.map((item, idx) => (
              <div key={idx} style={{ fontSize: 12, display: 'flex', gap: 6, alignItems: 'baseline', flexWrap: 'wrap' }}>
                <span style={{ color: 'var(--dim)', fontWeight: 500 }}>{item.title}:</span>
                <span
                  style={{
                    color: 'var(--text)',
                    fontWeight: 600,
                    background: 'var(--panel, rgba(255,255,255,0.06))',
                    padding: '1px 6px',
                    borderRadius: 4,
                    border: '1px solid var(--border-subtle, rgba(255,255,255,0.1))',
                  }}
                >
                  {item.answers.join(', ')}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={`grill-interactive-box${locked ? ' submitted' : ''}`}>
      <div className="grill-box-header">
        <span>{title || '🎯 Decision Options'}</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="grill-box-sub">
            {done ? 'Answers submitted' : closed ? 'Earlier question (closed)' : 'Select an option or write your own'}
          </span>
          {isFinished && (
            <button
              type="button"
              onClick={() => setIsExpanded(false)}
              style={{
                background: 'transparent',
                border: '1px solid var(--border-subtle, rgba(255,255,255,0.12))',
                borderRadius: 4,
                padding: '1px 6px',
                color: 'var(--dim)',
                fontSize: 10,
                cursor: 'pointer',
              }}
            >
              Collapse ▴
            </button>
          )}
        </div>
      </div>
      {questions.map((q, qi) => {
        const s = sel[qi] || new Set<number | 'other'>();
        const role = q.multi ? 'checkbox' : 'radio';
        const kind = q.multi ? 'multi' : 'single';
        const textOnly = q.kind === 'text';
        const otherOn = s.has('other');
        return (
          <div className="grill-q-block" key={qi}>
            <div className="grill-q-label">
              {!q.hideId && <strong>{q.qId} </strong>}
              {q.qTitle}
              <span className="grill-q-mode">
                {textOnly ? 'Type your answer' : q.multi ? 'Select all that apply' : 'Select one'}
              </span>
            </div>
            <div className="grill-options-list" role={q.multi ? 'group' : 'radiogroup'}>
              {q.options.map((o, oi) => {
                const isRec =
                  q.recommendation &&
                  (o === q.recommendation || o.includes(q.recommendation) || q.recommendation.includes(o));
                const on = s.has(oi);
                return (
                  <div
                    key={oi}
                    className={`grill-opt ${kind}${isRec ? ' recommended' : ''}${on ? ' active' : ''}`}
                    role={role}
                    tabIndex={locked ? -1 : 0}
                    aria-checked={on}
                    aria-disabled={locked}
                    onClick={() => toggle(qi, oi)}
                    onKeyDown={(e) => {
                      if (e.key === ' ' || e.key === 'Enter') {
                        e.preventDefault();
                        toggle(qi, oi);
                      }
                    }}
                  >
                    <span className="grill-opt-mark" aria-hidden="true" />
                    <span className="grill-opt-num">{oi + 1}.</span>
                    <span className="grill-opt-text">{o}</span>
                    {isRec && <span className="grill-pill-badge">★ Recommended</span>}
                  </div>
                );
              })}
              <div
                className={`grill-opt other ${kind}${otherOn ? ' active' : ''}`}
                role={role}
                aria-checked={otherOn}
                aria-disabled={locked}
                onClick={() => {
                  if (!otherOn) toggle(qi, 'other');
                }}
              >
                <span className="grill-opt-mark" aria-hidden="true" />
                {!textOnly && <span className="grill-opt-num">{q.options.length + 1}.</span>}
                <input
                  type="text"
                  className="grill-custom-input"
                  value={custom[qi] || ''}
                  disabled={locked}
                  placeholder={textOnly ? 'Type your answer…' : 'Other: type your own answer…'}
                  onChange={(e) => {
                    const v = e.target.value;
                    setCustom((prev) => ({ ...prev, [qi]: v }));
                    if (v.trim() && !s.has('other')) toggle(qi, 'other');
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      submit();
                    }
                  }}
                />
              </div>
            </div>
          </div>
        );
      })}
      {error && (
        <div role="alert" style={{ padding: '4px 12px', fontSize: 12, color: '#fca5a5' }}>
          {error}
        </div>
      )}
      <div className="grill-footer">
        <button
          type="button"
          className="btn primary grill-submit-btn"
          disabled={locked}
          onClick={submit}
        >
          {done ? '✓ Answer Submitted' : closed ? 'Closed' : submitLabel || '✓ Submit answers'}
        </button>
      </div>
    </div>
  );
};
