// tests/js/test_kb_policy.js - Knowledge Base cloud-access UI helpers (static/js/knowledge-policy.js)
const fs = require('fs');
const vm = require('vm');
const ok = (c, m) => { if (!c) { console.error('FAIL', m); process.exitCode = 1; } else console.log('ok', m); };
const ctx = { esc: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'), console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(__dirname + '/../../static/js/knowledge-policy.js', 'utf8') + '\nthis.h = kbSourceCloudHtml; this.p = kbPolicyHtml; this.w = kbWebHtml;', ctx);
const cats = [{ name: 'Public', cloud_ok: 1, sources: 2 }, { name: 'HR', cloud_ok: 0, sources: 1 }];

let h = ctx.h({ id: 5, category: null, cloud_ok: 1 }, cats, 'local_only');
ok(h.includes('class="kb-cloud-ok"') && h.includes('checked'), 'uncategorised source shows its own switch');
ok(h.includes('<option value="Public">Public - ☁') && h.includes('HR - local only'), 'category picker lists categories with their cloud setting');

h = ctx.h({ id: 5, category: 'HR', cloud_ok: 1, cloud_effective: false }, cats, 'local_only');
ok(!h.includes('kb-cloud-ok') && h.includes('🔒 local only') && h.includes('value="HR" selected'), 'category replaces the switch and wins');

h = ctx.h({ id: 5, category: 'Public', cloud_effective: true }, cats, 'local_only');
ok(h.includes('☁ cloud may read') && !h.includes('kb-cloud-ok'), 'cleared category shown as cloud-readable');

h = ctx.h({ id: 5 }, cats, 'allow');
ok(h.includes('policy: allow') && !h.includes('<select'), 'policy allow: no controls');

const evil = ctx.p({ cloud_policy: 'local_only', categories: [{ name: '<img onerror=x>', cloud_ok: 0, sources: 0 }],
  rules: [{ id: 1, name: '<b>r</b>', kind: 'regex', pattern: '<script>', enabled: 1, builtin: 0 }] });
ok(!evil.includes('<img onerror') && !evil.includes('<script>') && !evil.includes('<b>r</b>'), 'names and patterns are escaped');
ok(ctx.p({ cloud_policy: 'allow', categories: cats, rules: [] }).includes('disabled'), 'allow policy locks the category switches');

const w = { gap_fill: true, full_cos: 0.7, tiers: ['low', 'medium', 'high', 'deep'],
  budget: { full: { low: 0, medium: 0, high: 0, deep: 3 }, partial: { low: 2, medium: 3, high: 6, deep: 12 }, open: { low: 4, medium: 8, high: 12, deep: 20 } } };
const wh = ctx.w(w);
ok((wh.match(/class="kbp-wb"/g) || []).length === 12, 'web budget grid: 3 rows x 4 tiers');
ok(wh.includes('data-row="partial" data-tier="deep" value="12"') && wh.includes('id="kbp-gap-fill" checked'), 'values and the gap-fill switch are shown');
ok(ctx.w(null) === '' && ctx.p({ cloud_policy: 'local_only', categories: [], rules: [], web: w }).includes('kbp-web-save'), 'card is part of the policy panel');
