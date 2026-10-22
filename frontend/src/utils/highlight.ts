// Minimal self-contained syntax highlighter (port of static/js/highlight.js). Input is RAW text; output is escaped HTML.

const HL_ALIAS: Record<string, string | null> = {
  python: 'py', py: 'py',
  javascript: 'js', js: 'js', jsx: 'js', mjs: 'js',
  typescript: 'ts', ts: 'ts', tsx: 'ts',
  html: 'html', htm: 'html', xml: 'html', svg: 'html', vue: 'html',
  css: 'css', scss: 'css',
  json: 'json',
  bash: 'sh', sh: 'sh', shell: 'sh', powershell: 'sh', ps1: 'sh', bat: 'sh',
  php: 'php', sql: 'sql',
  md: null, txt: null, log: null,
};

/* rules: regex with numbered capture groups -> class names */
const HL_RULES: Record<string, { re: RegExp; cls: Record<number, string | null> }> = {
  py: {
    re: /(#[^\n]*)|("""[\s\S]*?"""|'''[\s\S]*?''')|((?:[rbfu]{0,2})(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'))|\b(\d+(?:\.\d+)?)\b|\b(and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|match|case|not|or|pass|raise|return|try|while|with|yield|True|False|None|self)\b|(@[\w.]+)/g,
    cls: { 1: 'cmt', 2: 'str', 3: 'str', 4: 'num', 5: 'kw', 6: 'fn' },
  },
  js: {
    re: /(\/\/[^\n]*|\/\*[\s\S]*?\*\/)|(`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|\b(\d+(?:\.\d+)?)\b|\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|super|import|from|export|default|async|await|try|catch|finally|throw|typeof|instanceof|delete|void|in|of|this|null|undefined|true|false|yield|static|get|set)\b/g,
    cls: { 1: 'cmt', 2: 'str', 3: 'num', 4: 'kw' },
  },
  ts: {
    re: /(\/\/[^\n]*|\/\*[\s\S]*?\*\/)|(`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|\b(\d+(?:\.\d+)?)\b|\b(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|super|implements|interface|type|enum|import|from|export|default|async|await|try|catch|finally|throw|typeof|instanceof|delete|void|in|of|this|null|undefined|true|false|yield|public|private|protected|readonly|as)\b/g,
    cls: { 1: 'cmt', 2: 'str', 3: 'num', 4: 'kw' },
  },
  html: {
    re: /(<!--[\s\S]*?-->)|(<!DOCTYPE[^>]*>)|(<\/?[\w-]+)|([\w-]+)=|("[^"]*"|'[^']*')/g,
    cls: { 1: 'cmt', 2: 'kw', 3: 'tag', 4: 'attr', 5: 'str' },
  },
  css: {
    re: /(\/\*[\s\S]*?\*\/)|("[^"]*"|'[^']*')|(#[0-9a-fA-F]{3,8}\b)|(\b\d+\.?\d*(?:px|em|rem|%|vh|vw|vmin|vmax|s|ms|deg|fr|ch)?\b)|(!important)|(@[\w-]+)|([\w-]+)(?=\s*:)/g,
    cls: { 1: 'cmt', 2: 'str', 3: 'num', 4: 'num', 5: 'kw', 6: 'kw', 7: 'attr' },
  },
  json: {
    re: /("(?:\\.|[^"\\])*")(\s*:)|("(?:\\.|[^"\\])*")|\b(true|false|null)\b|\b(\d+(?:\.\d+)?)\b/g,
    cls: { 1: 'key', 2: null, 3: 'str', 4: 'kw', 5: 'num' },
  },
  sh: {
    re: /(#[^\n]*)|("(?:\\.|[^"\\])*"|'[^']*')|(\$\w+)|\b(if|then|else|elif|fi|for|while|do|done|case|esac|function|return|export|local|echo|set|exit)\b/g,
    cls: { 1: 'cmt', 2: 'str', 3: 'var', 4: 'kw' },
  },
  php: {
    re: /(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|\$(\w+)|\b(\d+(?:\.\d+)?)\b|\b(function|return|if|else|elseif|foreach|for|while|switch|case|break|continue|new|class|extends|implements|public|private|protected|static|echo|print|try|catch|finally|throw|null|true|false|use|namespace|require|include)\b/g,
    cls: { 1: 'cmt', 2: 'str', 3: 'var', 4: 'num', 5: 'kw' },
  },
  sql: {
    re: /(--[^\n]*)|('(?:''|[^'])*')|\b(\d+(?:\.\d+)?)\b|\b(SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|DROP|ALTER|INDEX|VIEW|JOIN|INNER|LEFT|RIGHT|OUTER|ON|GROUP|BY|ORDER|HAVING|LIMIT|OFFSET|AS|AND|OR|NOT|NULL|PRIMARY|KEY|FOREIGN|REFERENCES|DISTINCT|COUNT|SUM|AVG|MIN|MAX|CASE|WHEN|THEN|ELSE|END|UNION|ALL|EXISTS|IN|BETWEEN|LIKE)\b/g,
    cls: { 1: 'cmt', 2: 'str', 3: 'num', 4: 'kw' },
  },
};

function hlEsc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* lang is the canonical key from HL_ALIAS (py/js/ts/html/css/json/sh/php/sql) */
export function hlCode(code: string, lang: string | null): string {
  const rule = lang ? HL_RULES[lang] : undefined;
  if (!rule || !code || code.length > 100000) return hlEsc(code || '');   // very large text is shown plain
  const re = new RegExp(rule.re.source, rule.re.flags);
  let out = "", last = 0, m: RegExpExecArray | null;
  while ((m = re.exec(code)) !== null) {
    out += hlEsc(code.slice(last, m.index));
    // first defined group with a class wins; class null = emit raw (e.g. the colon)
    let cls: string | null = null;
    for (let g = 1; g < m.length; g++) {
      if (m[g] !== undefined && rule.cls[g] !== null && rule.cls[g] !== undefined) {
        cls = rule.cls[g]; break;
      }
    }
    if (m[0] === '' || cls === null) {
      out += hlEsc(m[0]);
    } else {
      out += `<span class="tk-${cls}">${hlEsc(m[0])}</span>`;
    }
    last = m.index + m[0].length;
    if (re.lastIndex === m.index) re.lastIndex++;
  }
  out += hlEsc(code.slice(last));
  return out;
}

/* map a filename or fence info string to a canonical language key */
export function hlLangFor(nameOrInfo: string | null | undefined): string | null {
  if (!nameOrInfo) return null;
  const s = String(nameOrInfo).toLowerCase().trim();
  if (HL_ALIAS[s]) return HL_ALIAS[s];
  const ext = s.includes('.') ? s.split('.').pop() : s;
  return (ext && HL_ALIAS[ext]) || null;
}
