import React, { useState } from 'react';

/** Same rules the server enforces (core/auth.py password_policy_error), shown live so a change cannot fail on submit. */
export const MIN_PASSWORD_LEN = 12;

export interface PasswordCheck {
  label: string;
  ok: boolean;
}

export function passwordChecks(password: string, confirm: string, username = ''): PasswordCheck[] {
  const pw = password || '';
  return [
    { label: `At least ${MIN_PASSWORD_LEN} characters`, ok: pw.length >= MIN_PASSWORD_LEN },
    { label: 'Contains a letter', ok: /\p{L}/u.test(pw) },
    { label: 'Contains a digit', ok: /\p{Nd}/u.test(pw) },
    { label: 'Not the same as your username', ok: pw.length > 0 && (!username || pw.toLowerCase() !== username.toLowerCase()) },
    { label: 'Both new passwords match', ok: pw.length > 0 && pw === confirm },
  ];
}

export const allPasswordChecksPass = (checks: PasswordCheck[]) => checks.every((c) => c.ok);

const EYE = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);
const EYE_OFF = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M9.9 5.2A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.2M6.6 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7c1.6 0 3.1-.4 4.4-1" />
    <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
    <path d="m2 2 20 20" />
  </svg>
);

type InputProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type'>;

/** A password input with an eye button that shows or hides what was typed. */
export const PasswordInput: React.FC<InputProps> = ({ style, className, ...props }) => {
  const [visible, setVisible] = useState(false);
  // the input's top margin moves to the wrapper, so the eye button centres on the input itself
  const { marginTop, ...inputStyle } = style || {};
  return (
    <div style={{ position: 'relative', width: '100%', marginTop }}>
      <input
        {...props}
        type={visible ? 'text' : 'password'}
        className={className}
        style={{ ...inputStyle, paddingRight: 34 }}
      />
      <button
        type="button"
        aria-label={visible ? 'Hide password' : 'Show password'}
        aria-pressed={visible}
        title={visible ? 'Hide password' : 'Show password'}
        onClick={(e) => {
          e.stopPropagation();
          setVisible((v) => !v);
        }}
        style={{
          position: 'absolute',
          right: 6,
          top: '50%',
          transform: 'translateY(-50%)',
          width: 26,
          height: 26,
          padding: 0,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'transparent',
          border: 'none',
          borderRadius: 6,
          color: 'var(--dim, #8b949e)',
          cursor: 'pointer',
        }}
      >
        {visible ? EYE_OFF : EYE}
      </button>
    </div>
  );
};

/** The rules with a tick or a cross each; nothing is shown until the new password has been typed into. */
export const PasswordChecklist: React.FC<{ checks: PasswordCheck[]; show: boolean; fontSize?: number }> = ({ checks, show, fontSize = 12 }) => {
  if (!show) return null;
  return (
    <ul aria-label="Password requirements" style={{ listStyle: 'none', margin: '6px 0 0', padding: 0, fontSize, lineHeight: 1.6 }}>
      {checks.map((c) => (
        <li key={c.label} style={{ display: 'flex', alignItems: 'center', gap: 6, color: c.ok ? '#3fb950' : 'var(--dim, #8b949e)' }}>
          <span aria-hidden="true" style={{ width: 12, textAlign: 'center', fontWeight: 700 }}>{c.ok ? '✓' : '✗'}</span>
          <span>{c.label}</span>
          <span className="sr-only" style={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clip: 'rect(0 0 0 0)' }}>
            {c.ok ? 'met' : 'not met'}
          </span>
        </li>
      ))}
    </ul>
  );
};
