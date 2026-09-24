/* attachments: text files get appended to the prompt on send */
const attachments = [];

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}

function refreshAttachUI() {
  const b = $('btn-attach');
  const info = $('attach-info');
  const previewBar = $('attach-preview-bar');
  b.classList.toggle('has-file', attachments.length > 0);
  b.querySelectorAll('.badge').forEach(x => x.remove());

  if (previewBar) {
    if (!attachments.length) {
      previewBar.innerHTML = '';
    } else {
      previewBar.innerHTML = attachments.map((a, i) => {
        let thumb;
        if (a.isImage && a.dataUrl) {
          thumb = `<img src="${a.dataUrl}" alt="${esc(a.name)}" class="chat-img-thumb" title="Click to enlarge">`;
        } else if (a.isDoc) {
          const docIcon = a.uploading ? '⏳' : (a.truncated ? '📄⚡' : '📄');
          const docTitle = a.uploading ? 'Uploading & extracting...' : (a.truncated ? 'Truncated — agent will use read_file_chunk for more' : 'Document uploaded & extracted');
          thumb = `<span style="font-size:20px; line-height:1;" title="${docTitle}">${docIcon}</span>`;
        } else {
          thumb = `<span style="font-size:20px; line-height:1;">📝</span>`;
        }
        const statusBadge = a.isDoc && a.uploading
          ? `<span class="attach-doc-uploading">uploading…</span>`
          : (a.isDoc && a.truncated ? `<span class="attach-doc-badge truncated" title="Content truncated — agent will page through using read_file_chunk">chunked</span>` : (a.isDoc ? `<span class="attach-doc-badge ok">extracted</span>` : ''));
        return `<div class="attach-card ${a.isDoc && a.uploading ? 'uploading' : ''}">
          ${thumb}
          <div class="attach-card-info">
            <span class="attach-card-name" title="${esc(a.name)}">${esc(a.name)}</span>
            <span class="attach-card-size">${fmtBytes(a.size || 0)}${statusBadge}</span>
          </div>
          <span class="attach-card-remove" onclick="removeAttachment(${i})" title="Remove attachment">✕</span>
        </div>`;
      }).join('');
    }
  }

  if (attachments.length) {
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = attachments.length;
    b.appendChild(badge);
    const names = attachments.map(a => (a.isImage ? '🖼 ' : '') + a.name).join(', ');
    info.textContent = `${attachments.length} attachment${attachments.length > 1 ? 's' : ''} (${names.slice(0, 45)}…) · Click to clear all`;
  } else {
    info.textContent = 'Enter to send · Shift+Enter for newline';
  }
}

function removeAttachment(idx) {
  if (idx >= 0 && idx < attachments.length) {
    const removed = attachments.splice(idx, 1)[0];
    refreshAttachUI();
    toast(`Removed ${removed.name || 'attachment'}`);
  }
}

function preloadAttachmentVision(att) {
  if (!att || !att.isImage || !att.b64 || att.visionPromise) return;
  att.visionPromise = (async () => {
    try {
      const r = await fetch('/agent/vision', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          image_b64: att.b64,
          mime: att.mime || 'image/png',
          question: 'Describe this image in detail for a coding agent. Include any visible text, errors, or UI elements.'
        })
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
      att.visionDescription = j.description || '';
      return att.visionDescription;
    } catch (err) {
      att.visionDescription = `(vision unavailable: ${err.message})`;
      return att.visionDescription;
    }
  })();
}

$('btn-attach').onclick = () => $('file-input').click();
const IMAGE_RE = /\.(png|jpe?g|webp|gif|bmp|svg)$/i;
const DOC_RE = /\.(xlsx?|pdf|pptx?|docx?|csv)$/i;

async function addAttachmentFile(f, namePrefix = 'screenshot') {
  const isImage = IMAGE_RE.test(f.name || '') || (f.type && f.type.startsWith('image/'));
  const isDoc = !isImage && DOC_RE.test(f.name || '');

  if (isDoc) {
    // --- Document files: upload to server for extraction ---
    const maxDocBytes = 200 * 1024 * 1024; // 200MB cap for docs
    if (f.size > maxDocBytes) {
      toast(`"${f.name || 'File'}" is ${fmtBytes(f.size)} — max size is ${fmtBytes(maxDocBytes)}`, true);
      return;
    }
    const att = { name: f.name, size: f.size, isImage: false, isDoc: true, uploading: true, truncated: false };
    attachments.push(att);
    refreshAttachUI();
    try {
      const fd = new FormData();
      fd.append('files', f, f.name);
      const spaceParam = agentMode ? 'workspace' : 'common';
      const res = await fetch(`/agent/upload?space=${spaceParam}`, { method: 'POST', body: fd });
      const j = await res.json();
      if (!res.ok) throw new Error(j.detail || j.error || 'HTTP ' + res.status);
      const fileData = (j.files || [])[0] || {};
      const idx = attachments.indexOf(att);
      if (idx >= 0) {
        attachments[idx].uploading = false;
        attachments[idx].preview = fileData.preview || '';
        attachments[idx].truncated = fileData.truncated || false;
        attachments[idx].serverPath = fileData.path || f.name;
        // the server de-dupes names (deck-2.pptx); later edits must use that name
        if (fileData.name) attachments[idx].name = fileData.name;
        attachments[idx].content = fileData.preview || ''; // for buildPromptText compat
      }
      toast(`📄 ${f.name} uploaded & extracted`);
    } catch (err) {
      const idx = attachments.indexOf(att);
      if (idx >= 0) {
        attachments[idx].uploading = false;
        attachments[idx].preview = `(upload failed: ${err.message})`;
        attachments[idx].content = attachments[idx].preview;
      }
      toast(`Failed to upload ${f.name}: ${err.message}`, true);
    }
    refreshAttachUI();
    return;
  }

  // --- Images and text/code files: local FileReader ---
  const maxBytes = isImage ? (15 * 1024 * 1024) : (512 * 1024);
  if (f.size > maxBytes) {
    toast(`"${f.name || 'File'}" is ${fmtBytes(f.size)} — max size is ${fmtBytes(maxBytes)}`, true);
    return;
  }
  const timeStr = new Date().toTimeString().split(' ')[0].replace(/:/g, '');
  const fname = f.name && !f.name.startsWith('image.') ? f.name : `${namePrefix}_${timeStr}.png`;
  const att = { name: fname, size: f.size, isImage, truncated: false };
  attachments.push(att);
  const idx = attachments.length - 1;
  const reader = new FileReader();
  reader.onload = ev => {
    const url = String(ev.target.result || '');
    if (isImage) {
      attachments[idx].dataUrl = url;
      attachments[idx].b64 = (url.split(',')[1] || '');
      attachments[idx].mime = f.type || 'image/png';
      preloadAttachmentVision(attachments[idx]);
    } else {
      attachments[idx].content = url;
    }
    refreshAttachUI();
  };
  reader[isImage ? 'readAsDataURL' : 'readAsText'](f);
  refreshAttachUI();
}

$('file-input').onchange = e => {
  [...e.target.files].forEach(f => addAttachmentFile(f, 'upload'));
  e.target.value = '';
};

// Clipboard paste listener: paste screenshots directly anywhere on screen
window.addEventListener('paste', e => {
  const cd = e.clipboardData || window.clipboardData;
  if (!cd) return;
  let hasImage = false;
  const items = cd.items;
  if (items && items.length) {
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.type && it.type.startsWith('image/')) {
        const file = it.getAsFile();
        if (file) {
          hasImage = true;
          addAttachmentFile(file, 'clipboard_screenshot');
        }
      }
    }
  } else if (cd.files && cd.files.length) {
    for (let i = 0; i < cd.files.length; i++) {
      const file = cd.files[i];
      if (file.type && file.type.startsWith('image/')) {
        hasImage = true;
        addAttachmentFile(file, 'clipboard_screenshot');
      }
    }
  }
  if (hasImage) {
    e.preventDefault();
  }
});

// Drag & drop files or images anywhere on the window
window.addEventListener('dragover', e => {
  e.preventDefault();
});
window.addEventListener('drop', e => {
  e.preventDefault();
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
    [...e.dataTransfer.files].forEach(f => addAttachmentFile(f, 'dropped_file'));
  }
});

// Clicking attach-info clears attachments
$('attach-info').style.cursor = 'pointer';
$('attach-info').title = 'Click to clear attachments';
$('attach-info').onclick = () => {
  if (attachments.length) {
    clearAttachments();
    toast('Attachments cleared');
  }
};

async function buildPromptText(text, attList = null, signal = null) {
  const list = (attList && attList.length) ? attList : attachments;
  const textParts = list.filter(a => a.content != null).map(a =>
    `--- FILE: ${a.name} ---\n${a.content}\n--- END ${a.name} ---`);
  const imgParts = [];
  for (const a of list.filter(x => x.isImage && (x.b64 || x.visionPromise || x.visionDescription))) {
    try {
      let desc = a.visionDescription;
      if (!desc && a.visionPromise) {
        desc = await a.visionPromise;
      } else if (!desc && a.b64) {
        const r = await fetch('/agent/vision', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image_b64: a.b64,
            mime: a.mime || 'image/png',
            question: 'Describe this image in detail for a coding agent. Include any visible text, errors, or UI elements.',
            cloud_model_override: (typeof $ === 'function' && $('cloud-model-sel') && $('cloud-model-sel').value)
              ? $('cloud-model-sel').value
              : (localStorage.getItem('cloud_model_override') || undefined)
          }),
          signal: signal || undefined
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
        desc = j.description || '';
      }
      imgParts.push(`--- IMAGE: ${a.name} ---\n${desc}\n--- END ${a.name} ---`);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      imgParts.push(`--- IMAGE: ${a.name} ---\n(vision unavailable: ${err.message})\n--- END ${a.name} ---`);
    }
  }
  const parts = [...textParts, ...imgParts];
  const base = (text ? text + '\n\n' : '') + parts.join('\n\n');
  if (!agentMode) return base;
  return await expandAtTags(base);
}

function clearAttachments() {
  attachments.length = 0;
  refreshAttachUI();
}
