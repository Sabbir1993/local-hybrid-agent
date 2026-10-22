import { marked, type Tokens } from 'marked';
import DOMPurify from 'dompurify';

marked.use({
  gfm: true,
  breaks: true,
  renderer: {
    code({ text, lang }: Tokens.Code): string {
      const rawLang = (lang || '').trim();
      const label = rawLang || 'code';
      const escaped = text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
      return `<div class="code-block"><div class="code-block-head"><span class="code-block-lang">${label}</span><span class="code-block-actions"><button type="button" class="code-block-btn" data-click="code-copy" title="Copy code">📋 Copy</button></span></div><pre><code>${escaped}</code></pre></div>`;
    },
  },
});

export function renderMarkdown(raw: string): string {
  if (!raw) return '';
  const parsed = marked.parse(raw, { async: false }) as string;
  return DOMPurify.sanitize(parsed, {
    ADD_ATTR: ['data-click', 'data-arg', 'target', 'class'],
  });
}
