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

function theme(): 'dark' | 'default' {
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
    const src = sourceOf(box);
    const id = `mmd-${Date.now().toString(36)}-${seq++}`;
    try {
      const { svg } = await mermaid.render(id, src);
      svgCache.set(`${t}\n${src}`, svg);
      if (box.isConnected) show(box, svg);
    } catch (e: any) {
      if (box.isConnected) fail(box, `Diagram could not be drawn: ${String(e?.message || e).split('\n')[0].slice(0, 140)}`);
    } finally {
      // mermaid leaves a temporary element (and an error graphic on a syntax error) in <body>
      document.getElementById(id)?.remove();
      document.getElementById(`d${id}`)?.remove();
    }
  }
}
