import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { actualSizeView, fitView, pinch, wheelFactor, zoomAt, type View } from '../../utils/diagramZoom';

const STEP = 1.25;

/** The diagram's natural size in px: its viewBox, or what it measures on screen. */
function naturalSize(svg: SVGSVGElement): { w: number; h: number } {
  const vb = svg.viewBox && svg.viewBox.baseVal;
  if (vb && vb.width > 0 && vb.height > 0) return { w: vb.width, h: vb.height };
  const r = svg.getBoundingClientRect();
  return { w: r.width || 800, h: r.height || 600 };
}

/**
 * Large view of a diagram drawn in the chat (utils/mermaid.ts). The drawn SVG is copied into this view, so the chat is
 * untouched. Zoom with the buttons, the keys + - 0 1, the mouse wheel, or two fingers; drag to move; double-click toggles
 * between "fit" and the natural size.
 */
export const DiagramModal: React.FC = () => {
  const { diagramModal, closeDiagramModal } = useAppStore();
  const { isOpen, node, title } = diagramModal;

  const stageRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const sizeRef = useRef({ w: 0, h: 0 });
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinchPrev = useRef<{ dist: number; mid: [number, number] } | null>(null);
  const [view, setView] = useState<View>({ scale: 1, x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);

  const stageSize = () => {
    const st = stageRef.current;
    return { w: st ? st.clientWidth : 0, h: st ? st.clientHeight : 0 };
  };

  const fit = useCallback(() => {
    const { w, h } = stageSize();
    setView(fitView(w, h, sizeRef.current.w, sizeRef.current.h));
  }, []);

  const actualSize = useCallback(() => {
    const { w, h } = stageSize();
    setView(actualSizeView(w, h, sizeRef.current.w, sizeRef.current.h));
  }, []);

  const zoomCentre = useCallback((factor: number) => {
    const { w, h } = stageSize();
    setView((v) => zoomAt(v, factor, w / 2, h / 2));
  }, []);

  // copy the drawn diagram in at its natural size, then fit it to the window
  useLayoutEffect(() => {
    if (!isOpen || !node) return;
    const svg = (node instanceof SVGSVGElement ? node : node.querySelector('svg')) as SVGSVGElement | null;
    const canvas = canvasRef.current;
    if (!svg || !canvas) return;
    const { w, h } = naturalSize(svg);
    const copy = svg.cloneNode(true) as SVGSVGElement;
    copy.setAttribute('width', String(w));
    copy.setAttribute('height', String(h));
    copy.style.maxWidth = 'none';
    copy.style.width = `${w}px`;
    copy.style.height = `${h}px`;
    copy.style.display = 'block';
    canvas.replaceChildren(copy);
    sizeRef.current = { w, h };
    pointers.current.clear();
    pinchPrev.current = null;
    setDragging(false);
    fit();
    return () => canvas.replaceChildren();
  }, [isOpen, node, fit]);

  // keys
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeDiagramModal();
      else if (e.key === '+' || e.key === '=') zoomCentre(STEP);
      else if (e.key === '-' || e.key === '_') zoomCentre(1 / STEP);
      else if (e.key === '0') fit();
      else if (e.key === '1') actualSize();
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isOpen, closeDiagramModal, zoomCentre, fit, actualSize]);

  // wheel: not passive, so the page behind does not scroll while zooming
  useEffect(() => {
    const stage = stageRef.current;
    if (!isOpen || !stage) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      const f = wheelFactor(e.deltaY, e.deltaMode, e.ctrlKey);
      setView((v) => zoomAt(v, f, e.clientX - r.left, e.clientY - r.top));
    };
    stage.addEventListener('wheel', onWheel, { passive: false });
    return () => stage.removeEventListener('wheel', onWheel);
  }, [isOpen]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    pinchPrev.current = null;
    setDragging(true);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const prev = pointers.current.get(e.pointerId);
    if (!prev) return;
    const cur = { x: e.clientX, y: e.clientY };
    pointers.current.set(e.pointerId, cur);
    if (pointers.current.size === 1) {
      setView((v) => ({ ...v, x: v.x + cur.x - prev.x, y: v.y + cur.y - prev.y }));
    } else if (pointers.current.size === 2) {
      const r = e.currentTarget.getBoundingClientRect();
      const [a, b] = Array.from(pointers.current.values());
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const mid: [number, number] = [(a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top];
      const before = pinchPrev.current;
      if (before) setView((v) => pinch(v, before.dist, dist, before.mid, mid));
      pinchPrev.current = { dist, mid };
    }
  };

  const onPointerEnd = (e: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(e.pointerId);
    pinchPrev.current = null;
    if (pointers.current.size === 0) setDragging(false);
  };

  const onDoubleClick = () => {
    const { w, h } = stageSize();
    const fitted = fitView(w, h, sizeRef.current.w, sizeRef.current.h);
    if (Math.abs(view.scale - fitted.scale) < 0.02) actualSize();
    else fit();
  };

  const downloadSvg = () => {
    const svg = canvasRef.current?.querySelector('svg');
    if (!svg) return;
    const copy = svg.cloneNode(true) as SVGSVGElement;
    if (!copy.getAttribute('xmlns')) copy.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(copy)], { type: 'image/svg+xml' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = 'diagram.svg';
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  if (!isOpen) return null;

  // not the app's .btn class: it is full width, which stacks the controls in a column
  const btn = 'text-xs px-2.5 py-1 rounded border border-border text-text hover:bg-panel cursor-pointer whitespace-nowrap';
  const btnStyle: React.CSSProperties = { width: 'auto', margin: 0, background: 'transparent' };

  return (
    <div
      id="diagram-modal"
      className="fixed inset-0 z-[100000] flex items-center justify-center bg-black/80 backdrop-blur-md select-none"
      onClick={closeDiagramModal}
      role="dialog"
      aria-modal="true"
      aria-label="Diagram, large view"
    >
      <div
        className="relative flex flex-col overflow-hidden border border-border bg-panel shadow-2xl w-screen h-[100dvh] sm:w-[94vw] sm:h-[88vh] sm:max-w-6xl sm:rounded-xl"
        onClick={(e) => e.stopPropagation()}
        style={{ background: 'var(--panel, #0f172a)', border: '1px solid var(--border, #334155)' }}
      >
        <div
          className="flex items-center justify-between gap-3 px-4 py-2.5 border-b border-border bg-panel2 flex-wrap"
          style={{ borderBottom: '1px solid var(--border, #334155)', background: 'var(--panel2, #1e293b)' }}
        >
          <div className="flex items-center gap-2 min-w-0">
            <span style={{ fontSize: '15px' }}>🗺️</span>
            <span className="mono font-semibold text-xs text-text truncate">{title || 'Diagram'}</span>
            <span className="badge-main text-[10px] px-2 py-0.5 rounded font-bold uppercase tracking-wider bg-accent/20 text-accent border border-accent/30">
              Diagram
            </span>
          </div>
          <div className="flex items-center gap-1.5 flex-wrap">
            <button type="button" className={btn} style={btnStyle} onClick={() => zoomCentre(1 / STEP)} title="Zoom out (-)" aria-label="Zoom out">−</button>
            <button type="button" className={`${btn} min-w-[3.4rem]`} style={btnStyle} onClick={actualSize} title="Natural size (1)" aria-label="Natural size">
              {Math.round(view.scale * 100)}%
            </button>
            <button type="button" className={btn} style={btnStyle} onClick={() => zoomCentre(STEP)} title="Zoom in (+)" aria-label="Zoom in">+</button>
            <button type="button" className={btn} style={btnStyle} onClick={fit} title="Fit to window (0)">Fit</button>
            <button type="button" className={btn} style={btnStyle} onClick={downloadSvg} title="Save the diagram as an SVG file">⬇ SVG</button>
            <button
              type="button"
              className="text-sm px-2.5 py-1 rounded hover:bg-panel text-text-muted hover:text-text cursor-pointer"
              style={btnStyle}
              onClick={closeDiagramModal}
              title="Close (Esc)"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        </div>

        <div
          ref={stageRef}
          className="relative flex-1 overflow-hidden"
          style={{ background: 'var(--bg, #0b0f19)', cursor: dragging ? 'grabbing' : 'grab', touchAction: 'none' }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerEnd}
          onPointerCancel={onPointerEnd}
          onDoubleClick={onDoubleClick}
        >
          <div
            ref={canvasRef}
            style={{
              position: 'absolute',
              left: 0,
              top: 0,
              transformOrigin: '0 0',
              transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
            }}
          />
        </div>

        <div className="px-4 py-1.5 text-[11px] text-dim border-t border-border" style={{ borderTop: '1px solid var(--border, #334155)' }}>
          Scroll or pinch to zoom · drag to move · double-click to fit or show the natural size · keys + − 0 1
        </div>
      </div>
    </div>
  );
};
