// Question cards: a typed ```questionnaire block, or numbered / bulleted choices the model writes in prose
// (port of renderInteractiveQuestions in static/js/chat.js and parseQuestionnaire in static/js/markdown.js).

export interface GrillQuestion {
  qId: string;
  qTitle: string;
  recommendation: string;
  options: string[];
  multi: boolean;
  hideId?: boolean;
  kind?: 'radio' | 'checkbox' | 'text';
}

export const GRILL_MAX_OPTIONS = 12;
const _GRILL_MULTI_RE = /\b(?:select|choose|pick|check|tick)\s+(?:all|any|one or more|multiple|several)\b|\ball that apply\b|\bmulti-?select\b|\bmultiple (?:choices|answers|options)\b/i;
const _GRILL_OTHER_RE = /^(?:other|something else|none of (?:the )?above|custom(?: answer)?)\b/i;

export function parseProseQuestions(rawText: string): GrillQuestion[] {
  // Check if text matches grill-me format or question frontiers
  // Look for ❓, Q1/Q2, Question 1, or ➡️ / recommended markers
  const hasTrigger = rawText.includes('❓') ||
                     /\b(?:Q[0-9]+|Question\s+[0-9]+)\b/i.test(rawText) ||
                     rawText.includes('➡️') ||
                     /(?:^|\n)\s*(?:[-*•]|\([a-zA-Z0-9]+\)|[0-9]+\))\s*\[[ x]\]/i.test(rawText);
  if (!hasTrigger) return [];

  // Split into question chunks: match starting with ❓, Q1:, Question 1:, etc.
  const blocks = rawText.split(/(?:^|\n)(?=(?:❓|#{1,4}\s*Q[0-9]+|#{1,4}\s*Question\s+[0-9]+|\bQ[0-9]+|\bQuestion\s+[0-9]+|\*\*[0-9]+\.\*\*)[.:\s-])/gi).filter((b) => b.trim());
  const parsedQuestions: GrillQuestion[] = [];

  for (const block of blocks) {
    // Match Q identifier and title
    const qMatch = block.match(/(?:❓\s*)?(?:#{1,4}\s*)?(?:[*_]{0,2})(Q[0-9]+|Question\s+[0-9]+|\b[0-9]+)[*_]{0,2}\s*[-–:.)]?\s*([^\n]+)/i);
    if (!qMatch) continue;
    let rawQId = qMatch[1].trim();
    if (!rawQId.toLowerCase().startsWith('q')) {
      const numOnly = rawQId.replace(/\D/g, '');
      rawQId = numOnly ? `Q${numOnly}` : rawQId;
    } else {
      rawQId = rawQId.replace(/Question\s*/i, 'Q').toUpperCase();
    }
    const qId = rawQId;
    const qTitle = qMatch[2].replace(/[*_#]/g, '').trim();

    // Look for recommended answer (➡️ ... or Recommendation: ... or (Recommended: ...))
    let recommendation = '';
    const recMatch = block.match(/(?:➡️|Recommendation:?|Recommended:?)\s*([^\n]+)/i);
    if (recMatch) {
      recommendation = recMatch[1].replace(/[*_]/g, '').trim();
    }

    // Look for options like: - [ ] Option or - Option A or (A) Option or A) Option
    const options: string[] = [];
    let sawCheckbox = false;
    const lines = block.split('\n');
    for (const line of lines) {
      const optMatch = line.match(/^\s*(?:[-*•]|\([a-zA-Z0-9]+\)|[a-zA-Z0-9]+[.)])\s*(\[[ x]\]\s*)?([^\n]+)/i);
      if (!optMatch) continue;
      let optText = optMatch[2].replace(/[*_`]/g, '').trim();
      if (optText.startsWith('❓') || optText.startsWith('➡️') || optText.toLowerCase().startsWith('recommend')) continue;
      if (optText.length <= 1 || optText.length >= 200) continue;
      // Lead-in lines ("However, here are a few things I can do:") are headers, not choices
      if (/:\s*$/.test(optText)) continue;
      if (optMatch[1]) sawCheckbox = true;
      if (/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i.test(optText)) {
        optText = optText.replace(/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i, '').trim();
        if (!recommendation) recommendation = optText;
      }
      // The card always renders its own "Other" row
      if (_GRILL_OTHER_RE.test(optText)) continue;
      if (optText && !options.includes(optText)) {
        options.push(optText);
      }
    }

    if (recommendation) {
      const cleanRec = recommendation.replace(/^(?:\(Recommended\)|\(Rec\)|\bRecommended:?\b)/i, '').trim();
      if (cleanRec && !options.some(o => o.toLowerCase().includes(cleanRec.toLowerCase()) || cleanRec.toLowerCase().includes(o.toLowerCase()))) {
        options.unshift(cleanRec);
      }
    }

    if (options.length > GRILL_MAX_OPTIONS) continue;     // a long checklist / outline, not a decision
    if (options.length > 0 || recommendation) {
      const multi = sawCheckbox || _GRILL_MULTI_RE.test(block);
      parsedQuestions.push({ qId, qTitle, recommendation, options, multi });
    }
  }

  return parsedQuestions;
}

const QZ_MAX_Q = 6, QZ_MAX_OPT = 8;

export function parseQuestionnaire(text: string): { prose: string; quiz: { title: string; submit: string; questions: GrillQuestion[] } | null; partial: boolean } | null {
  const src = String(text ?? '');
  const m = /```questionnaire[ \t]*\r?\n([\s\S]*?)(```|$)/.exec(src);
  if (!m) return null;
  const closed = m[2] === '```';
  const prose = (src.slice(0, m.index) + (closed ? src.slice(m.index + m[0].length) : '')).replace(/\s+$/, '');
  if (!closed) return { prose, quiz: null, partial: true };
  let data: any;
  try { data = JSON.parse(m[1]); } catch (_) { return { prose: src, quiz: null, partial: false }; }
  const str = (v: unknown, n: number) => (typeof v === 'string' ? v.trim().slice(0, n) : '');
  const qs = Array.isArray(data && data.questions) ? data.questions.slice(0, QZ_MAX_Q) : [];
  const questions: GrillQuestion[] = [];
  for (const q of qs) {
    const title = str(q && q.q, 200);
    if (!title) continue;
    const opts = Array.isArray(q.options) ? q.options.map((o: unknown) => str(o, 120)).filter(Boolean).slice(0, QZ_MAX_OPT) : [];
    let type: string = ['radio', 'checkbox', 'text'].includes(q.type) ? q.type : (opts.length ? 'radio' : 'text');
    if (type !== 'text' && !opts.length) type = 'text';
    questions.push({ qId: title, qTitle: title, hideId: true, recommendation: '', options: type === 'text' ? [] : opts,
      multi: type === 'checkbox', kind: type as GrillQuestion['kind'] });
  }
  if (!questions.length) return { prose: src, quiz: null, partial: false };
  return { prose, partial: false, quiz: { title: str(data.title, 120), submit: str(data.submit_label, 40), questions } };
}
