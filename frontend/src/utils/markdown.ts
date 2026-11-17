import { marked, type Tokens } from 'marked';
import DOMPurify from 'dompurify';
import { hlCode, hlLangFor } from './highlight';

marked.use({
  gfm: true,
  breaks: true,
  renderer: {
    code({ text, lang }: Tokens.Code): string {
      const rawLang = (lang || '').trim();
      const label = rawLang || 'code';
      const escaped = hlCode(text, hlLangFor(rawLang));
      return `<div class="code-block"><div class="code-block-head"><span class="code-block-lang">${label}</span><span class="code-block-actions"><button type="button" class="code-block-btn" data-click="code-copy" title="Copy code">📋 Copy</button></span></div><pre><code>${escaped}</code></pre></div>`;
    },
    codespan({ text }: Tokens.Codespan): string {
      const escaped = hlCode(text, 'cmd');
      return `<code class="inline-cmd">${escaped}</code>`;
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
