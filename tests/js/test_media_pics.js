// tests/js/test_media_pics.js -  the pictures bar in static/js/media.js with tiny DOM stubs (no browser).
const fs = require('fs');
const vm = require('vm');
const els = {
  'media-pics-bar': { hidden: true, innerHTML: '' },
  'comp-mode': { hidden: false, value: 'image', addEventListener() {} },
  input: { placeholder: 'Message the model…', dataset: {}, addEventListener() {}, focus() {} },
};
const store = {};
const ctx = {
  console, setTimeout, clearTimeout, Promise, JSON, Math, Date, encodeURIComponent,
  $: id => els[id] || null,
  esc: s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  toast: m => { ctx.lastToast = m; },
  localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } },
  document: { addEventListener() {}, hidden: false, querySelector: () => null },
  window: {},
  fetch: async () => ({ ok: false, json: async () => ({}) }),
  attachments: [],
  refreshAttachUI() {},
  Image: class { set src(v) { setTimeout(() => this.onerror && this.onerror(), 0); } },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(__dirname + '/../../static/js/media.js', 'utf8'), ctx);

const ok = (c, m) => { if (!c) { console.error('FAIL', m); process.exitCode = 1; } else console.log('ok', m); };
vm.runInContext(`MEDIA.status = { image: { ready: true, model: 'Qwen-2.1', edit: { img2img: true, refs: false, max_refs: 0 } } };`, ctx);

ctx.mediaPaintPics();
ok(!els['media-pics-bar'].hidden && /data-mopt="size"/.test(els['media-pics-bar'].innerHTML)
   && !/image1/.test(els['media-pics-bar'].innerHTML), 'no pictures: shape + size only');
ok(/Large · 1024×1024/.test(els['media-pics-bar'].innerHTML), 'size labels show pixels');
ok(/<option value="" selected>Model default · Large 1024×1024/.test(els['media-pics-bar'].innerHTML), 'Model default first');
ok(ctx.mediaSizeFor('large', 'wide').join('x') === '1248x832', 'size matches the server');
vm.runInContext(`MEDIA.status.image.cloud = true;`, ctx);
ctx.mediaPaintPics();
ok(!/data-mopt="size"/.test(els['media-pics-bar'].innerHTML), 'no size choice for a cloud model');
vm.runInContext(`delete MEDIA.status.image.cloud;`, ctx);

ctx.attachments.push({ isImage: true, b64: 'AAAA', dataUrl: 'data:image/png;base64,AAAA', mime: 'image/png', name: 'a.png' });
ctx.mediaPaintPics();
const h1 = els['media-pics-bar'].innerHTML;
ok(!els['media-pics-bar'].hidden && h1.includes('&lt;image1&gt;'), 'shows <image1>');
ok(h1.includes('data-mpic="strength"'), 'one picture without vision -> change + strength slider');
ok(/data-val="edit"[^>]*disabled/.test(h1), 'combine disabled without vision weights');
ok(els.input.placeholder === 'Describe how it should look…', 'placeholder changes');

ctx.attachments.push({ isImage: true, b64: 'BBBB', dataUrl: 'data:image/jpeg;base64,BBBB', mime: 'image/jpeg', name: 'b.jpg' });
ctx.mediaPaintPics();
ok(els['media-pics-bar'].innerHTML.includes('can’t combine pictures yet'), 'two pictures without vision -> warning');

vm.runInContext(`MEDIA.status.image.edit = { img2img: true, refs: true, max_refs: 4 };`, ctx);
ctx.mediaPaintPics();
const h2 = els['media-pics-bar'].innerHTML;
ok(h2.includes('&lt;image2&gt;') && !h2.includes('data-mpic="strength"') && !h2.includes('⚠'), 'two pictures with vision -> combine, no warning');

ctx.attachments.push({ isImage: true, b64: 'CCCC', dataUrl: 'data:image/gif;base64,CCCC', mime: 'image/gif', name: 'c.gif' });
(async () => {
  const bad = await ctx.mediaTakePics();
  ok(bad === false && /PNG, JPEG or WEBP/.test(ctx.lastToast), 'gif refused before sending');
  ctx.attachments.pop();
  vm.runInContext(`MEDIA.editPics = [{ path: 'generated/x.png', name: 'x.png' }];`, ctx);
  const got = await ctx.mediaTakePics();
  ok(got && got.mode === 'edit' && got.count === 3, 'takes 3 pictures in edit mode');
  ok(got.inputs[0].path === 'generated/x.png' && got.inputs[1].b64 === 'AAAA', 'edit-this picture first, then attachments');
  ok(ctx.attachments.length === 0, 'pictures leave the composer');
  ctx.mediaPaintPics();
  ok(els['media-pics-bar'].hidden && els.input.placeholder === 'Describe the image…' || els.input.placeholder === 'Message the model…',
     'bar hidden again, placeholder restored');
})();

// Stop in the chat (or deleting it) stops the images it's making
(() => {
  els['btn-abort'] = { style: { display: 'none' } };
  const calls = [];
  ctx.fetch = async (u, o) => { calls.push([u, o && o.method]); return { ok: true, json: async () => ({}) }; };
  vm.runInContext(`var messages = []; var generating = false;
    var renderLast = () => {}, renderAll = () => {}, persistMsgForSession = () => {};`, ctx);
  const r = vm.runInContext(`(() => {
    const mine = { role: 'assistant', media: { kind: 'image', state: 'running', jobId: 'j1', sid: 7 } };
    const other = { role: 'assistant', media: { kind: 'image', state: 'running', jobId: 'j2', sid: 8 } };
    Object.defineProperty(mine.media, 'list', { value: messages, enumerable: false });
    messages.push(mine);
    _mediaLive(mine); _mediaLive(other);
    const shown = $('btn-abort').style.display === '';
    const n = mediaStopChat(7, messages);
    return { shown, n, state: mine.media.state, other: other.media.state, hidden: $('btn-abort').style.display,
             saved: JSON.stringify(mine).includes('list') };
  })()`, ctx);
  ok(r.shown, 'Stop shows while this chat makes an image');
  ok(r.n === 1 && r.state === 'cancelled' && r.other === 'running', 'Stop stops only this chat\'s image');
  ok(calls.some(c => c[0] === '/media/jobs/j1' && c[1] === 'DELETE'), 'the server job is cancelled');
  ok(r.hidden === 'none', 'Stop hides again');
  ok(!r.saved, 'the chat link is never serialized');
})();
