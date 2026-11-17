// Minimal self-contained syntax highlighter. Input is RAW text; output is escaped HTML.

const HL_ALIAS: Record<string, string | null> = {
  python: 'py', py: 'py', py3: 'py',
  javascript: 'js', js: 'js', jsx: 'js', mjs: 'js', cjs: 'js', node: 'js',
  typescript: 'ts', ts: 'ts', tsx: 'ts',
  html: 'html', htm: 'html', xml: 'html', svg: 'html', vue: 'html',
  css: 'css', scss: 'css', less: 'css',
  json: 'json', jsonc: 'json',
  bash: 'cmd', sh: 'cmd', shell: 'cmd', zsh: 'cmd',
  powershell: 'cmd', ps1: 'cmd', bat: 'cmd', cmd: 'cmd', cli: 'cmd',
  terminal: 'cmd', console: 'cmd', cisco: 'cmd', juniper: 'cmd', junos: 'cmd',
  router: 'cmd', network: 'cmd', conf: 'cmd',
  php: 'php', sql: 'sql', mysql: 'sql', postgres: 'sql', pgsql: 'sql',
  yaml: 'yaml', yml: 'yaml',
  md: null, txt: null, log: null, code: null,
};

/* rules: regex with numbered capture groups -> class names */
const HL_RULES: Record<string, { re: RegExp; cls: Record<number, string | null> }> = {
  cmd: {
    re: /(![^\n]*|#[^\n]*|\/\/[^\n]*)|("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')|(<[\w\s.:/-]+>)|((?:^|\n)\s*[$#>]\s+)|(\B-{1,2}[\w-]+)|(\$\w+|%\w+%)|\b(\d+(?:\.\d+)?)\b|\b(commit|rollback|save|load|show|set|delete|edit|run|config|configure|terminal|interface|router|ip|route|vlan|switchport|no|shutdown|tftp|ftp|scp|ping|traceroute|write|memory|reload|copy|enable|disable|exit|quit|end|status|version|changed|undoes|diff|git|npm|npx|pnpm|yarn|docker|docker-compose|podman|kubectl|helm|sudo|apt|yum|dnf|pacman|brew|systemctl|journalctl|service|curl|wget|tar|zip|unzip|chmod|chown|grep|sed|awk|cat|tail|head|less|echo|kill|ps|top|htop|df|du|ls|cd|mkdir|rm|cp|mv|find|tree|touch|clear|export|alias|source|python|python3|pip|pip3|node|deno|bun|cargo|go|rustc|powershell|pwsh|dir|type|cls|netstat|ipconfig|tracert|if|then|else|elif|fi|for|while|do|done|case|esac|return)\b/g,
    cls: { 1: 'cmt', 2: 'str', 3: 'ph', 4: 'prompt', 5: 'flag', 6: 'var', 7: 'num', 8: 'kw' },
  },
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
  yaml: {
    re: /(#[^\n]*)|(true|false|null)|(\b\d+(?:\.\d+)?\b)|([\w.-]+)(?=\s*:)|("(?:\\.|[^"\\])*"|'[^']*')/g,
    cls: { 1: 'cmt', 2: 'kw', 3: 'num', 4: 'key', 5: 'str' },
  },
  php: {
    re: /(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|\$(\w+)|\b(\d+(?:\.\d+)?)\b|\b(function|return|if|else|elseif|foreach|for|while|switch|case|break|continue|new|class|extends|implements|public|private|protected|static|echo|print|try|catch|finally|throw|null|true|false|use|namespace|require|include)\b/g,
    cls: { 1: 'cmt', 2: 'str', 3: 'var', 4: 'num', 5: 'kw' },
  },
  sql: {
    re: /(--[^\n]*)|('(?:''|[^'])*')|\b(\d+(?:\.\d+)?)\b|\b(SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|DROP|ALTER|INDEX|VIEW|JOIN|INNER|LEFT|RIGHT|OUTER|ON|GROUP|BY|ORDER|HAVING|LIMIT|OFFSET|AS|AND|OR|NOT|NULL|PRIMARY|KEY|FOREIGN|REFERENCES|DISTINCT|COUNT|SUM|AVG|MIN|MAX|CASE|WHEN|THEN|ELSE|END|UNION|ALL|EXISTS|IN|BETWEEN|LIKE)\b/gi,
    cls: { 1: 'cmt', 2: 'str', 3: 'num', 4: 'kw' },
  },
};

function hlEsc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** Automatically detect language from code content when not provided or generic */
export function detectLang(code: string): string {
  if (!code) return 'cmd';
  const trimmed = code.trim();
  if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
    try { JSON.parse(trimmed); return 'json'; } catch (_) {}
  }
  if (/^<(!DOCTYPE|html|div|svg|xml)/i.test(trimmed)) return 'html';
  if (/(?:^|\n)\s*(?:def\s+\w+|import\s+\w+|from\s+\w+\s+import|class\s+\w+:|elif\s+|print\()/m.test(trimmed)) return 'py';
  if (/(?:^|\n)\s*(?:const\s+\w+|let\s+\w+|function\s+\w+|export\s+(?:default\s+)?|import\s+.*from|=>)/m.test(trimmed)) return 'ts';
  if (/\b(?:SELECT\s+.*FROM|INSERT\s+INTO|CREATE\s+TABLE|UPDATE\s+\w+\s+SET)\b/i.test(trimmed)) return 'sql';
  return 'cmd';
}

/* lang is the canonical key from HL_ALIAS (py/js/ts/html/css/json/cmd/yaml/php/sql) */
export function hlCode(code: string, lang: string | null): string {
  const resolved = (lang && HL_RULES[lang]) ? lang : detectLang(code);
  const rule = HL_RULES[resolved] || HL_RULES.cmd;
  if (!rule || !code || code.length > 100000) return hlEsc(code || '');
  const re = new RegExp(rule.re.source, rule.re.flags);
  let out = '', last = 0, m: RegExpExecArray | null;
  while ((m = re.exec(code)) !== null) {
    out += hlEsc(code.slice(last, m.index));
    let cls: string | null = null;
    for (let g = 1; g < m.length; g++) {
      if (m[g] !== undefined && rule.cls[g] !== null && rule.cls[g] !== undefined) {
        cls = rule.cls[g];
        break;
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
  if (HL_ALIAS[s] !== undefined) return HL_ALIAS[s];
  const ext = s.includes('.') ? s.split('.').pop() : s;
  return (ext && HL_ALIAS[ext] !== undefined) ? HL_ALIAS[ext] : null;
}
