import React, { useState } from 'react';
import type { GrillQuestion } from '../../utils/questions';

interface Props {
  questions: GrillQuestion[];
  title?: string;
  submitLabel?: string;
  /** a later message exists: the card is read-only history */
  closed: boolean;
  /** returns false when the answer could not be sent (the card stays open) */
  onSubmit: (text: string) => boolean;
}

/** Decision card: pick an option (radio or checkbox) or type your own, then send the answers as one message. */
export const QuestionCard: React.FC<Props> = ({ questions, title, submitLabel, closed, onSubmit }) => {
  const [sel, setSel] = useState<Record<number, Set<number | 'other'>>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});
  const [done, setDone] = useState(false);
  const [error, setError] = useState('');
  const locked = done || closed;

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

  return (
    <div className={`grill-interactive-box${locked ? ' submitted' : ''}`}>
      <div className="grill-box-header">
        <span>{title || '🎯 Decision Options'}</span>
        <span className="grill-box-sub">{done ? 'Answers submitted' : closed ? 'Earlier question (closed)' : 'Select an option or write your own'}</span>
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
              <span className="grill-q-mode">{textOnly ? 'Type your answer' : q.multi ? 'Select all that apply' : 'Select one'}</span>
            </div>
            <div className="grill-options-list" role={q.multi ? 'group' : 'radiogroup'}>
              {q.options.map((o, oi) => {
                const isRec = q.recommendation && (o === q.recommendation || o.includes(q.recommendation) || q.recommendation.includes(o));
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
                    onKeyDown={(e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggle(qi, oi); } }}
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
                onClick={() => { if (!otherOn) toggle(qi, 'other'); }}
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
                    // typing in "Other" selects it (and clears the others for single choice)
                    if (v.trim() && !s.has('other')) toggle(qi, 'other');
                  }}
                  onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } }}
                />
              </div>
            </div>
          </div>
        );
      })}
      {error && <div role="alert" style={{ padding: '4px 12px', fontSize: 12, color: '#fca5a5' }}>{error}</div>}
      <div className="grill-footer">
        <button type="button" className="btn primary grill-submit-btn" disabled={locked} onClick={submit}>
          {done ? '✓ Answer Submitted' : closed ? 'Closed' : submitLabel || '✓ Submit answers'}
        </button>
      </div>
    </div>
  );
};
