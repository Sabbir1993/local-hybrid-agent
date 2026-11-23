/* runtime-config.js - machine-level llama.cpp runtime in Settings > Model Config.
 * GET/PUT /control/runtime. Saving needs model.local.configure. The server validates
 * every value with the same select_runtime() used at startup, so a bad choice cannot
 * leave the app unbootable. Changes apply after a model reload / restart. */
'use strict';

var RT_DATA = null;

function rtEl(id) { return document.getElementById(id); }

function rtNote(text, bad) {
  var el = rtEl('rt-note');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = bad ? 'var(--error, #f87171)' : '';
}

function rtFillSelect(sel, values, current) {
  if (!sel) return;
  sel.innerHTML = '';
  for (var i = 0; i < values.length; i++) {
    var opt = document.createElement('option');
    opt.value = values[i];
    opt.textContent = values[i];
    sel.appendChild(opt);
  }
  if (current) sel.value = current;
}

function rtFillForm() {
  var d = RT_DATA;
  if (!d) return;
  var nameSel = rtEl('rt-name');
  var name = (nameSel && nameSel.value) ? nameSel.value : d.runtime;
  var preset = (d.runtimes || {})[name] || {};
  rtFillSelect(rtEl('rt-backend'), d.available_backends || ['vulkan', 'cuda', 'sycl'],
               preset.backend || name);
  var bin = rtEl('rt-bin');
  if (bin) bin.value = preset.llama_bin_dir || '';
  var gpus = rtEl('rt-gpus');
  if (gpus) {
    var list = preset.gpu_devices;
    gpus.value = Array.isArray(list) ? list.join(',') : (list || 'auto');
  }
  var small = rtEl('rt-small-gpu');
  if (small) small.value = (preset.small_model_gpu === null || preset.small_model_gpu === undefined)
    ? '' : String(preset.small_model_gpu);

  var note = 'Saved in config/app.json. Reload the model (or restart the app) to apply.';
  if (d.env_override) {
    note += ' LLAMA_RUNTIME=' + d.env_override + ' is set and wins over the saved preset.';
  }
  if (d.active && d.active.error) {
    note += ' Current config is invalid: ' + d.active.error;
    rtNote(note, true);
  } else {
    rtNote(note, false);
  }
}

async function rtLoad() {
  var r = await fetch('/control/runtime', { cache: 'no-store' });
  var d = await r.json().catch(function () { return {}; });
  if (!r.ok) throw new Error(d.detail || d.error || ('HTTP ' + r.status));
  RT_DATA = d;
  var names = Object.keys(d.runtimes || {});
  if (!names.length) names = [d.runtime];
  rtFillSelect(rtEl('rt-name'), names, d.runtime);
  rtFillForm();
}

async function rtSave() {
  var nameSel = rtEl('rt-name');
  var backendSel = rtEl('rt-backend');
  var bin = rtEl('rt-bin');
  var gpus = rtEl('rt-gpus');
  var small = rtEl('rt-small-gpu');
  if (!nameSel || !backendSel || !bin || !gpus) return;
  var body = {
    runtime: nameSel.value.trim(),
    backend: backendSel.value,
    llama_bin_dir: bin.value.trim(),
    gpu_devices: gpus.value.trim()
  };
  var smallText = small ? small.value.trim() : '';
  if (smallText !== '') body.small_model_gpu = Number(smallText);
  try {
    var r = await fetch('/control/runtime', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    var j = await r.json().catch(function () { return {}; });
    if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    await rtLoad();
    rtNote(j.warning || 'Saved. Reload the model (or restart the app) to apply.', false);
    if (typeof toast === 'function') toast('Runtime settings saved');
  } catch (e) {
    rtNote('Save failed: ' + e.message, true);
    if (typeof toast === 'function') toast('Runtime save failed: ' + e.message, true);
  }
}

(function rtInit() {
  var sel = rtEl('rt-name');
  if (!sel) return;
  sel.addEventListener('change', rtFillForm);
  var save = rtEl('rt-save');
  if (save) save.addEventListener('click', rtSave);
  rtLoad().catch(function (e) { rtNote('Could not load runtime settings: ' + e.message, true); });
})();
