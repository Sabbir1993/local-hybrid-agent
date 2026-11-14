/**
 * Pan and zoom arithmetic for the large diagram view. Pure functions, no DOM, so they can be tested on their own.
 *
 * A view is the transform applied to the diagram: screen = diagram * scale + (x, y), with the transform origin at the
 * diagram's top-left corner. Scale 1 is the diagram's natural size.
 */
export interface View {
  scale: number;
  x: number;
  y: number;
}

export const MIN_SCALE = 0.1;
export const MAX_SCALE = 8;

export function clampScale(s: number): number {
  if (!Number.isFinite(s) || s <= 0) return 1;
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s));
}

/**
 * The view that shows the whole diagram, centred, with a margin. A small diagram is enlarged at most `maxFit` times, so a
 * three-box diagram does not fill the screen with huge boxes.
 */
export function fitView(viewW: number, viewH: number, diagramW: number, diagramH: number, margin = 24, maxFit = 2): View {
  if (!(viewW > 0 && viewH > 0 && diagramW > 0 && diagramH > 0)) return { scale: 1, x: 0, y: 0 };
  const scale = clampScale(Math.min(maxFit, (viewW - 2 * margin) / diagramW, (viewH - 2 * margin) / diagramH));
  return { scale, x: (viewW - diagramW * scale) / 2, y: (viewH - diagramH * scale) / 2 };
}

/** Natural size (100%), centred. */
export function actualSizeView(viewW: number, viewH: number, diagramW: number, diagramH: number): View {
  return { scale: 1, x: (viewW - diagramW) / 2, y: (viewH - diagramH) / 2 };
}

/** Zoom by `factor`, keeping the diagram point that is under (px, py), in view coordinates, where it is. */
export function zoomAt(v: View, factor: number, px: number, py: number): View {
  const scale = clampScale(v.scale * factor);
  const k = scale / v.scale;
  return { scale, x: px - (px - v.x) * k, y: py - (py - v.y) * k };
}

/** Two fingers: scale by the ratio of their distances, around their midpoint, and follow the midpoint as it moves. */
export function pinch(
  v: View,
  prevDist: number,
  dist: number,
  prevMid: [number, number],
  mid: [number, number],
): View {
  const ratio = prevDist > 0 && dist > 0 ? dist / prevDist : 1;
  const zoomed = zoomAt(v, ratio, prevMid[0], prevMid[1]);
  return { ...zoomed, x: zoomed.x + (mid[0] - prevMid[0]), y: zoomed.y + (mid[1] - prevMid[1]) };
}

/** The factor for one mouse-wheel or trackpad event. A pinch on a trackpad arrives as a wheel event with ctrlKey set. */
export function wheelFactor(deltaY: number, deltaMode: number, ctrlKey: boolean): number {
  const dy = deltaMode === 1 ? deltaY * 16 : deltaY;           // some browsers report lines, not pixels
  return Math.exp(-dy * (ctrlKey ? 0.01 : 0.0015));
}
