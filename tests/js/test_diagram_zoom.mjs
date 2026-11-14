// node tests/js/test_diagram_zoom.mjs - the pan / zoom arithmetic of the large diagram view (frontend/src/utils/diagramZoom.ts)
import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const z = await import(pathToFileURL(path.join(here, '..', '..', 'frontend', 'src', 'utils', 'diagramZoom.ts')).href);

let failed = 0;
const t = (name, fn) => {
  try { fn(); console.log('ok  ', name); } catch (e) { failed++; console.log('FAIL', name, '-', e.message); }
};
const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} is not ${b}`);

t('fit centres a wide diagram and leaves a margin', () => {
  const v = z.fitView(1000, 600, 2000, 500);           // limited by width: (1000 - 48) / 2000
  close(v.scale, 0.476);
  close(v.x, 24);
  close(v.y, (600 - 500 * 0.476) / 2);
});

t('fit never blows a small diagram up past maxFit', () => {
  const v = z.fitView(1000, 600, 100, 50);
  assert.equal(v.scale, 2);
  close(v.x, (1000 - 200) / 2);
});

t('fit with no size is a harmless identity view', () => {
  assert.deepEqual(z.fitView(0, 0, 100, 100), { scale: 1, x: 0, y: 0 });
  assert.deepEqual(z.fitView(500, 500, 0, 100), { scale: 1, x: 0, y: 0 });
});

t('actual size is centred at 100 percent', () => {
  assert.deepEqual(z.actualSizeView(1000, 600, 400, 200), { scale: 1, x: 300, y: 200 });
});

t('zooming keeps the point under the cursor fixed', () => {
  const v = { scale: 1, x: 100, y: 50 };
  const px = 400, py = 300;
  const before = { dx: (px - v.x) / v.scale, dy: (py - v.y) / v.scale };       // the diagram point under the cursor
  const w = z.zoomAt(v, 2, px, py);
  close(w.scale, 2);
  close((px - w.x) / w.scale, before.dx);
  close((py - w.y) / w.scale, before.dy);
});

t('zoom in then out by the same factor returns to the same view', () => {
  const v = { scale: 1.3, x: -40, y: 25 };
  const back = z.zoomAt(z.zoomAt(v, 1.25, 321, 123), 1 / 1.25, 321, 123);
  close(back.scale, v.scale); close(back.x, v.x); close(back.y, v.y);
});

t('zoom is limited at both ends', () => {
  let v = { scale: 1, x: 0, y: 0 };
  for (let i = 0; i < 100; i++) v = z.zoomAt(v, 1.5, 0, 0);
  assert.equal(v.scale, z.MAX_SCALE);
  for (let i = 0; i < 100; i++) v = z.zoomAt(v, 0.5, 0, 0);
  assert.equal(v.scale, z.MIN_SCALE);
});

t('a bad scale is treated as 1, never NaN', () => {
  assert.equal(z.clampScale(NaN), 1);
  assert.equal(z.clampScale(-3), 1);
  assert.equal(z.clampScale(Infinity), 1);
});

t('pinch scales by the finger distance ratio and follows the midpoint', () => {
  const v = { scale: 1, x: 0, y: 0 };
  const w = z.pinch(v, 100, 200, [300, 200], [310, 190]);
  close(w.scale, 2);
  // the point that was under the old midpoint is now under the new one
  close((310 - w.x) / w.scale, (300 - v.x) / v.scale);
  close((190 - w.y) / w.scale, (200 - v.y) / v.scale);
});

t('pinch ignores a zero distance', () => {
  const v = { scale: 1.5, x: 10, y: 10 };
  assert.equal(z.pinch(v, 0, 50, [0, 0], [0, 0]).scale, 1.5);
});

t('wheel: scrolling up zooms in, down zooms out, a trackpad pinch is stronger', () => {
  assert.ok(z.wheelFactor(-100, 0, false) > 1);
  assert.ok(z.wheelFactor(100, 0, false) < 1);
  assert.ok(z.wheelFactor(-5, 0, true) > z.wheelFactor(-5, 0, false));
  close(z.wheelFactor(-100, 0, false) * z.wheelFactor(100, 0, false), 1);
  assert.equal(z.wheelFactor(-1, 1, false), z.wheelFactor(-16, 0, false));      // line mode counts 16 pixels
});

if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log('all passed');
