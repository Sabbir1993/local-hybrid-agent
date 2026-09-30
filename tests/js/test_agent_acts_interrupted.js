// node tests/js/test_agent_acts_interrupted.js - a finished/reloaded message never shows "Executing..."; interrupted runs offer Continue
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'agent-acts.js'), 'utf8');
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const ctx = { window: {}, document: { addEventListener() {}, querySelectorAll: () => [] }, esc, md: esc, console,
  $: () => null, toast() {}, navigator: {} };
vm.createContext(ctx);
try { vm.runInContext(src, ctx); } catch (e) { console.error('load failed:', e.message); process.exit(1); }

const acts = [
  { type: 'step', step: 1 },
  { type: 'tool_call', id: 'a', name: 'run_python', args: { code: 'print(1)' } },     // never answered
];
const live = ctx.agentActsHtml(JSON.parse(JSON.stringify(acts)), true);
assert.ok(/Executing/.test(live) && /Working/.test(live), 'a live run still shows progress');
const done = ctx.agentActsHtml(JSON.parse(JSON.stringify(acts)), false);
assert.ok(!/Executing/.test(done), 'a finished message must not show Executing...');
assert.ok(!/Working…/.test(done), 'nor the Working bar');
assert.ok(/interrupted/.test(done), 'it says the call was interrupted');

const stopped = [{ type: 'stopped', reason: 'interrupted', note: 'the connection ended', steps: 3, pending: 0, plan_total: 0 }];
const banner = ctx.agentStoppedHtml(stopped, true);
assert.ok(/Interrupted/.test(banner) && /agent-continue/.test(banner), 'interrupted: message + Continue');
assert.ok(!/agent-continue/.test(ctx.agentStoppedHtml(stopped, false)), 'no Continue while another run is going');
assert.ok(/Stopped by you/.test(ctx.agentStoppedHtml([{ type: 'stopped', reason: 'cancelled' }], true)));
assert.ok(/agent-continue/.test(ctx.agentStoppedHtml([{ type: 'stopped', reason: 'budget' }], true)));
assert.ok(/token budget/.test(ctx.agentStoppedHtml([{ type: 'stopped', reason: 'budget' }], true)));
console.log('agent acts interrupted tests: OK');
