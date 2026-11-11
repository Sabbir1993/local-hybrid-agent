import { useEffect, useState } from 'react';

/** True while the CSS media query matches (e.g. '(max-width: 640px)' or '(pointer: coarse)'); follows rotation / resize. */
export function useMediaQuery(query: string): boolean {
  const get = () => typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(query).matches;
  const [matches, setMatches] = useState<boolean>(get);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const mq = window.matchMedia(query);
    const on = () => setMatches(mq.matches);
    on();
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [query]);
  return matches;
}

/** The widths the responsive layout switches at (frontend/src/styles/responsive.css uses the same numbers). */
export const PHONE_QUERY = '(max-width: 640px)';
export const NARROW_QUERY = '(max-width: 1023px)';
export const TOUCH_QUERY = '(pointer: coarse)';
