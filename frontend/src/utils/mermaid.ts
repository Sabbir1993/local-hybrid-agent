/**
 * Draws ```mermaid blocks in chat messages (the box comes from chatRender.ts: .mermaid-box with the source in
 * .mermaid-code-raw). The library is the vendored static/vendor/mermaid.min.js, loaded on first use only, and runs with
 * securityLevel "strict": labels are sanitized and click/script directives are ignored.
 *
 * A message's HTML is replaced on every streamed chunk, which wipes a drawn diagram. drawCached() puts already drawn
 * SVGs back synchronously (no flicker); renderMermaidIn() draws the new ones once the block has stopped changing.
 * Until then, and when the source is not valid Mermaid, the source stays visible as a normal code block.
 */

const MERMAID_SRC = '/static/vendor/mermaid.min.js';
const svgCache = new Map<string, string>();      // `${theme}\n${source}` -> svg
let loader: Promise<any> | null = null;
let initializedTheme = '';
let seq = 0;

function theme(): 'default' | 'dark' {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'default' : 'dark';
}

function loadMermaid(): Promise<any> {
  const w = window as any;
  if (w.mermaid) return Promise.resolve(w.mermaid);
  if (!loader) {
    loader = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = MERMAID_SRC;
      s.async = true;
      s.onload = () => (w.mermaid ? resolve(w.mermaid) : reject(new Error('mermaid did not load')));
      s.onerror = () => {
        loader = null;                                   // allow a retry on the next message
        reject(new Error('mermaid script failed to load'));
      };
      document.head.appendChild(s);
    });
  }
  return loader;
}

function sourceOf(box: HTMLElement): string {
  return (box.querySelector('.mermaid-code-raw code')?.textContent || '').trim();
}

function show(box: HTMLElement, svg: string): void {
  const view = box.querySelector<HTMLElement>('.mermaid-viewport');
  if (!view) return;
  view.innerHTML = svg;
  box.classList.add('drawn');
  box.querySelector('.mermaid-error')?.remove();
}

function fail(box: HTMLElement, message: string): void {
  if (box.querySelector('.mermaid-error')) return;
  const note = document.createElement('div');
  note.className = 'mermaid-error';
  note.textContent = message;
  box.appendChild(note);
}

/**
 * Auto-sanitizes LLM-generated Mermaid flowchart/graph syntax so that unquoted labels
 * containing parentheses, brackets, colons, slashes, currency symbols, or HTML tags
 * are safely wrapped in double quotes. This prevents lexer parse errors like:
 * "Diagram could not be drawn: Parse error on line 3: ... C[GlobalSign ... (~৳59,741 (≈ $480)/yr)]".
 */
export function sanitizeMermaid(src: string): string {
  if (!src || !src.trim()) return src;

  const lines = src.split('\n');
  let diagType = 'unknown';

  for (const line of lines) {
    const s = line.trim().toLowerCase();
    if (s && !s.startsWith('%%')) {
      if (s.startsWith('flowchart') || s.startsWith('graph')) {
        diagType = 'flowchart';
      } else if (s.startsWith('sequencediagram')) {
        diagType = 'sequence';
      } else if (s.startsWith('classdiagram')) {
        diagType = 'class';
      } else if (s.startsWith('statediagram')) {
        diagType = 'state';
      } else if (s.startsWith('erdiagram')) {
        diagType = 'er';
      }
      break;
    }
  }

  // Only flowchart / graph syntax relies on overloaded bracket shapes that crash on parentheses
  if (diagType !== 'flowchart') {
    return src;
  }

  const shapes: Array<[string, string, RegExp]> = [
    ['([', '])', /\(\[/],
    ['[[', ']]', /\[\[/],
    ['[(', ')]', /\[\(/],
    ['{{', '}}', /\{\{/],
    ['(((', ')))', /\(\(\(/],
    ['((', '))', /\(\(/],
    ['[/', '/]', /\[\//],
    ['[\\', '\\]', /\[\\/],
    ['[', ']', /\[/],
    ['{', '}', /\{/],
    ['(', ')', /\(/],
  ];

  const nodeRegex = /(^|[\s;&,\-\-><])([a-zA-Z0-9_.-]+)\s*(?:\(\[(?<stadium>.*?)\]\)|\[\[(?<subroutine>.*?)\]\]|\[\((?<cylinder>.*?)\)\]|\{\{(?<hexagon>.*?)\}\}|\(\(\((?<dcircle>.*?)\)\)\)|\(\((?<circle>.*?)\)\)|\[\/(?<trapezoid1>.*?)[\\\/]\]|\[\\(?<trapezoid2>.*?)[\\\/]\]|\[(?<rect>.*?)\]|\{(?<rhombus>.*?)\}|\((?<round>.*?)\))/g;

  function fixContent(content: string): string {
    const raw = content.trim();
    if (!raw) return '';
    if (raw.startsWith('"') && raw.endsWith('"') && raw.length >= 2) {
      return `"${raw.slice(1, -1).replace(/"/g, "'")}"`;
    }
    const inner = raw.replace(/"/g, "'");
    return `"${inner}"`;
  }

  const outLines: string[] = [];

  for (const line of lines) {
    const stripped = line.trim();
    if (!stripped || stripped.startsWith('%%') || stripped.startsWith('classDef') || stripped.startsWith('style') || stripped.startsWith('linkStyle') || stripped.startsWith('click') || stripped.startsWith('flowchart') || stripped.startsWith('graph')) {
      outLines.push(line);
      continue;
    }

    // 1. Protect and sanitize edge labels |...|
    const edgeLabels: string[] = [];
    let lineNoEdges = line.replace(/\|([^\|\r\n]+)\|/g, (_, rawEdge) => {
      const raw = (rawEdge || '').trim();
      let lbl = raw;
      if (raw.startsWith('"') && raw.endsWith('"') && raw.length >= 2) {
        lbl = `"${raw.slice(1, -1).replace(/"/g, "'")}"`;
      } else if (/[()[\]{}:;~$৳&<>]/.test(raw)) {
        lbl = `"${raw.replace(/"/g, "'")}"`;
      }
      const idx = edgeLabels.length;
      edgeLabels.push(lbl);
      return `|__EDGE_LABEL_${idx}__|`;
    });

    // Auto-close truncated node definitions if missing closing delimiter at end of line
    for (const [opStr, clStr, opRe] of shapes) {
      const escapedCloseFirst = clStr[0].replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const unclosedRe = new RegExp(`([a-zA-Z0-9_.-]+)\\s*${opRe.source}([^${escapedCloseFirst}\\r\\n]*)$`);
      const unclosedMatch = unclosedRe.exec(lineNoEdges);
      if (unclosedMatch) {
        const prefixIdx = unclosedMatch.index;
        const nid = unclosedMatch[1];
        const cnt = unclosedMatch[2].trim();
        lineNoEdges = lineNoEdges.slice(0, prefixIdx) + `${nid}${opStr}${cnt}${clStr}`;
        break;
      }
    }

    // 2. Match nodes in flowchart and wrap labels in quotes
    let cleanedLine = lineNoEdges.replace(nodeRegex, (match, prefix, nid, ...args) => {
      const groups = (args[args.length - 1] || {}) as Record<string, string | undefined>;
      const shapeDefs: Array<[string, string, string]> = [
        ['stadium', '([', '])'],
        ['subroutine', '[[', ']]'],
        ['cylinder', '[(', ')]'],
        ['hexagon', '{{', '}}'],
        ['dcircle', '(((', ')))'],
        ['circle', '((', '))'],
        ['trapezoid1', '[/', '/]'],
        ['trapezoid2', '[\\', '\\]'],
        ['rect', '[', ']'],
        ['rhombus', '{', '}'],
        ['round', '(', ')'],
      ];
      for (const [key, op, cl] of shapeDefs) {
        const val = groups[key];
        if (val !== undefined && val !== null) {
          return `${prefix}${nid}${op}${fixContent(val)}${cl}`;
        }
      }
      return match;
    });

    // 3. Restore edge labels
    edgeLabels.forEach((lbl, idx) => {
      cleanedLine = cleanedLine.replace(`__EDGE_LABEL_${idx}__`, lbl);
    });

    outLines.push(cleanedLine);
  }

  return outLines.join('\n');
}

/** Put diagrams that were drawn before back into freshly rendered HTML. Synchronous: call it in a layout effect. */
export function drawCached(root: HTMLElement | null): void {
  if (!root) return;
  const t = theme();
  root.querySelectorAll<HTMLElement>('.mermaid-box:not(.drawn)').forEach((box) => {
    const svg = svgCache.get(`${t}\n${sourceOf(box)}`);
    if (svg) show(box, svg);
  });
}

/** Draw every not-yet-drawn Mermaid block under root. Failures leave the source visible with a short note. */
export async function renderMermaidIn(root: HTMLElement | null): Promise<void> {
  if (!root) return;
  drawCached(root);
  const todo = Array.from(root.querySelectorAll<HTMLElement>('.mermaid-box:not(.drawn)')).filter((b) => sourceOf(b));
  if (!todo.length) return;
  let mermaid: any;
  try {
    mermaid = await loadMermaid();
  } catch {
    todo.forEach((b) => fail(b, 'Diagram renderer could not be loaded - showing the source.'));
    return;
  }
  const t = theme();
  if (initializedTheme !== t) {
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: t });
    initializedTheme = t;
  }
  for (const box of todo) {
    const rawSrc = sourceOf(box);
    const cleanSrc = sanitizeMermaid(rawSrc);
    const id = `mmd-${Date.now().toString(36)}-${seq++}`;
    try {
      let svg: string;
      try {
        const res = await mermaid.render(id, cleanSrc);
        svg = res.svg;
      } catch (firstErr) {
        // If sanitized render failed and cleanSrc differed from raw, retry with rawSrc as fallback
        if (cleanSrc !== rawSrc) {
          const fallbackId = `mmd-raw-${Date.now().toString(36)}-${seq++}`;
          try {
            const res = await mermaid.render(fallbackId, rawSrc);
            svg = res.svg;
          } catch {
            throw firstErr;
          } finally {
            for (const el of [document.getElementById(fallbackId), document.getElementById(`d${fallbackId}`)]) {
              if (el && !el.closest('.mermaid-viewport')) el.remove();
            }
          }
        } else {
          throw firstErr;
        }
      }
      svgCache.set(`${t}\n${rawSrc}`, svg);
      if (box.isConnected) show(box, svg);
    } catch (e: any) {
      if (box.isConnected) fail(box, `Diagram could not be drawn: ${String(e?.message || e).split('\n')[0].slice(0, 140)}`);
    } finally {
      // mermaid can leave its temporary element (and an error graphic on a syntax error) in <body>. The drawn SVG we just
      // put into the message has the same id, so only remove elements outside a diagram box - never the diagram itself.
      for (const el of [document.getElementById(id), document.getElementById(`d${id}`)]) {
        if (el && !el.closest('.mermaid-viewport')) el.remove();
      }
    }
  }
}
