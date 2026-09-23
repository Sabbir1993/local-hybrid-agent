/* ---------------- preview.js: Interactive file & artifact previewer ---------------- */
"use strict";

let curPreviewData = null;

// Initialize Mermaid with current theme settings
function initMermaidTheme() {
  if (typeof mermaid === 'undefined') return;
  const t = (typeof getSavedTheme === 'function') ? getSavedTheme() : 'slate';
  const isDark = t !== 'light';
  mermaid.initialize({
    startOnLoad: false,
    theme: isDark ? 'dark' : 'default',
    securityLevel: 'loose',
    themeVariables: {
      darkMode: isDark,
      fontFamily: 'inherit',
      primaryColor: isDark ? '#1f293d' : '#e2e8f0',
      primaryBorderColor: isDark ? '#7aa2f7' : '#2563eb',
      primaryTextColor: isDark ? '#f0f6fc' : '#0f172a',
      lineColor: isDark ? '#79c0ff' : '#0284c7',
    }
  });
}

// Render all inline mermaid blocks present in container
async function renderInlineMermaid(container) {
  if (typeof mermaid === 'undefined' || !container) return;
  initMermaidTheme();
  const els = container.querySelectorAll('.mermaid-code-raw:not([data-processed="true"])');
  for (let i = 0; i < els.length; i++) {
    const el = els[i];
    el.setAttribute('data-processed', 'true');
    const code = el.textContent.trim();
    const id = 'mermaid-render-' + Math.random().toString(36).slice(2, 9);
    const parentBox = el.closest('.mermaid-box');
    const viewport = parentBox ? parentBox.querySelector('.mermaid-viewport') : null;
    if (!viewport) continue;
    try {
      const { svg } = await mermaid.render(id, code);
      viewport.innerHTML = svg;
      el.style.display = 'none';
    } catch (err) {
      console.warn('Mermaid render error:', err);
      viewport.innerHTML = `<div style="color:var(--amber); font-size:11px; padding:8px;">⚠️ Could not render diagram (${esc(err.message || 'Syntax error')})</div>`;
      el.style.display = 'block';
    }
  }
}

// Global modal preview opener
async function openFilePreview(filePath, title = '', directContent = null) {
  const modal = $('preview-modal');
  const titleEl = $('preview-title');
  const iconEl = $('preview-icon');
  const contentEl = $('preview-content');
  const controlsEl = $('preview-controls');
  const dlBtn = $('preview-dl');
  const rawBtn = $('preview-raw-link');
  if (!modal || !contentEl) return;

  const fname = (filePath || title || 'file').split('\\').pop().split('/').pop();
  const ext = fname.includes('.') ? fname.split('.').pop().toLowerCase() : '';
  
  titleEl.textContent = title || fname;
  titleEl.title = filePath || fname;
  controlsEl.innerHTML = '';
  contentEl.innerHTML = '<div style="display:flex; align-items:center; justify-content:center; height:100%; color:var(--dim); font-size:12px;">⏳ Loading preview...</div>';
  
  const rawUrl = `/agent/raw?path=${encodeURIComponent(filePath)}`;
  const dlUrl = `/agent/download?path=${encodeURIComponent(filePath)}`;
  if (dlBtn) {
    dlBtn.href = dlUrl;
    dlBtn.download = fname;
    dlBtn.style.display = filePath ? '' : 'none';
  }
  if (rawBtn) {
    rawBtn.href = rawUrl;
    rawBtn.style.display = filePath ? '' : 'none';
  }

  // Choose icon based on filetype
  const iconMap = {
    html: '🌐', htm: '🌐', svg: '🖼️', mermaid: '📊', mmd: '📊',
    csv: '📋', xlsx: '📊', xls: '📊', pdf: '📕',
    md: '📝', py: '🐍', js: '⚡', json: '🧩', txt: '📄'
  };
  iconEl.textContent = iconMap[ext] || '👁️';

  modal.hidden = false;
  modal.style.display = 'flex';

  curPreviewData = { filePath, fname, ext, directContent };

  try {
    if (ext === 'html' || ext === 'htm') {
      renderHtmlPreview(rawUrl, directContent, contentEl, controlsEl);
    } else if (ext === 'mermaid' || ext === 'mmd') {
      let code = directContent;
      if (!code && filePath) {
        const r = await fetch(rawUrl);
        code = await r.text();
      }
      renderMermaidModalPreview(code, contentEl, controlsEl);
    } else if (ext === 'csv') {
      let text = directContent;
      if (!text && filePath) {
        const r = await fetch(rawUrl);
        text = await r.text();
      }
      renderCsvPreview(text, contentEl, controlsEl);
    } else if (ext === 'xlsx' || ext === 'xls') {
      let buf = (directContent instanceof ArrayBuffer) ? directContent : null;
      if (!buf && filePath) {
        const r = await fetch(rawUrl);
        if (!r.ok) throw new Error('Failed to fetch Excel file (' + r.status + '): ' + r.statusText);
        buf = await r.arrayBuffer();
      }
      renderExcelPreview(buf, contentEl, controlsEl);
    } else if (ext === 'pdf') {
      renderPdfPreview(rawUrl, contentEl, controlsEl);
    } else if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'ico'].includes(ext) || (filePath && filePath.startsWith('http') && !filePath.includes('.pdf') && !filePath.includes('.csv'))) {
      renderImagePreview(filePath && filePath.startsWith('http') ? filePath : rawUrl, contentEl, controlsEl);
    } else if (ext === 'md') {
      let text = directContent;
      if (!text && filePath) {
        const r = await fetch(rawUrl);
        text = await r.text();
      }
      renderMarkdownModalPreview(text, contentEl, controlsEl);
    } else {
      // Default: Code or text file
      let text = directContent;
      if (!text && filePath) {
        const r = await fetch(rawUrl);
        text = await r.text();
      }
      renderCodePreview(text, ext, contentEl, controlsEl);
    }
  } catch (err) {
    contentEl.innerHTML = `<div style="padding:24px; color:var(--red); font-size:12px;">Failed to preview file: ${esc(err.message)}</div>`;
  }
}

// 1. HTML Viewer with Desktop / Tablet / Mobile device simulation
function renderHtmlPreview(url, content, container, controls) {
  controls.innerHTML = `
    <button class="btn ghost device-toggle-btn active" data-w="100%" title="Full width">🖥️ Desktop</button>
    <button class="btn ghost device-toggle-btn" data-w="768px" title="Tablet width (768px)">📱 Tablet</button>
    <button class="btn ghost device-toggle-btn" data-w="390px" title="Mobile width (390px)">📲 Mobile</button>
    <button class="btn ghost device-toggle-btn" id="btn-html-refresh" title="Reload iframe">🔄 Refresh</button>
  `;

  const frameWrap = document.createElement('div');
  frameWrap.style.cssText = 'flex:1; width:100%; height:100%; display:flex; justify-content:center; background:#111; overflow:hidden; transition:width 0.2s ease;';
  
  const iframe = document.createElement('iframe');
  iframe.className = 'preview-frame';
  iframe.sandbox = 'allow-scripts allow-forms allow-same-origin allow-popups allow-modals';
  iframe.style.cssText = 'width:100%; height:100%; border:none; background:#ffffff; transition:max-width 0.2s ease;';
  
  if (content) {
    iframe.srcdoc = content;
  } else if (url) {
    // Check if server returns 200 before setting iframe.src, so 404 JSON isn't rendered directly in the iframe
    fetch(url).then(async res => {
      if (res.ok) {
        iframe.src = url;
      } else {
        const errJson = await res.json().catch(() => ({}));
        container.innerHTML = `
          <div style="padding:40px 24px; text-align:center; color:var(--dim); font-size:12px; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100%;">
            <span style="font-size:32px; margin-bottom:12px;">📁</span>
            <div style="font-weight:600; color:var(--text); font-size:14px; margin-bottom:6px;">File Not Found On Disk</div>
            <div style="max-width:440px; margin-bottom:16px; line-height:1.5;">${esc(errJson.error || 'The requested file could not be located in workspace or storage.')}</div>
            <div class="dim" style="font-size:11px;">If this was generated in a previous chat turn, ask the assistant to write or export it again.</div>
          </div>`;
      }
    }).catch(err => {
      iframe.src = url;
    });
  }
  frameWrap.appendChild(iframe);

  container.innerHTML = '';
  container.appendChild(frameWrap);

  controls.querySelectorAll('.device-toggle-btn[data-w]').forEach(btn => {
    btn.onclick = () => {
      controls.querySelectorAll('.device-toggle-btn[data-w]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      iframe.style.maxWidth = btn.dataset.w;
    };
  });

  const refBtn = controls.querySelector('#btn-html-refresh');
  if (refBtn) {
    refBtn.onclick = () => {
      if (content) iframe.srcdoc = content;
      else if (url) iframe.src = url;
    };
  }
}


// 2. Mermaid full preview in modal
async function renderMermaidModalPreview(code, container, controls) {
  initMermaidTheme();
  controls.innerHTML = `
    <button class="btn ghost device-toggle-btn active" id="btn-m-diagram">📊 Diagram</button>
    <button class="btn ghost device-toggle-btn" id="btn-m-source">📝 Source</button>
    <button class="btn ghost device-toggle-btn" id="btn-m-zoom-in" title="Zoom in">🔍＋</button>
    <button class="btn ghost device-toggle-btn" id="btn-m-zoom-out" title="Zoom out">🔍－</button>
    <button class="btn ghost device-toggle-btn" id="btn-m-zoom-reset" title="Reset zoom">100%</button>
    <button class="btn ghost device-toggle-btn" id="btn-m-copy">📋 Copy</button>
  `;

  container.innerHTML = `
    <div id="modal-mermaid-viewport" style="flex:1; overflow:auto; display:flex; align-items:center; justify-content:center; padding:32px; background:var(--bg); user-select:none;">
      <div style="color:var(--dim); font-size:12px;">Rendering diagram...</div>
    </div>
    <pre id="modal-mermaid-code" style="display:none; flex:1; margin:0; padding:20px; overflow:auto; background:var(--bg-code); font-size:12.5px; font-family:monospace; color:var(--text); line-height:1.5;"><code>${esc(code)}</code></pre>
  `;

  const vp = container.querySelector('#modal-mermaid-viewport');
  const pre = container.querySelector('#modal-mermaid-code');
  const diaBtn = controls.querySelector('#btn-m-diagram');
  const srcBtn = controls.querySelector('#btn-m-source');
  const copyBtn = controls.querySelector('#btn-m-copy');
  const zoomInBtn = controls.querySelector('#btn-m-zoom-in');
  const zoomOutBtn = controls.querySelector('#btn-m-zoom-out');
  const zoomResetBtn = controls.querySelector('#btn-m-zoom-reset');

  let currentZoom = 1.0;

  function applyZoom() {
    const svgEl = vp.querySelector('svg');
    if (svgEl) {
      svgEl.style.transform = `scale(${currentZoom})`;
      svgEl.style.transformOrigin = 'center center';
      svgEl.style.transition = 'transform 0.15s ease';
    }
  }

  if (zoomInBtn) {
    zoomInBtn.onclick = () => {
      currentZoom = Math.min(3.0, currentZoom + 0.2);
      applyZoom();
    };
  }
  if (zoomOutBtn) {
    zoomOutBtn.onclick = () => {
      currentZoom = Math.max(0.4, currentZoom - 0.2);
      applyZoom();
    };
  }
  if (zoomResetBtn) {
    zoomResetBtn.onclick = () => {
      currentZoom = 1.0;
      applyZoom();
    };
  }

  // Ctrl + Mouse Wheel zooming on diagram viewport
  vp.addEventListener('wheel', e => {
    if (e.ctrlKey) {
      e.preventDefault();
      const delta = e.deltaY < 0 ? 0.15 : -0.15;
      currentZoom = Math.min(4.0, Math.max(0.3, currentZoom + delta));
      applyZoom();
      if (zoomResetBtn) {
        zoomResetBtn.textContent = `${Math.round(currentZoom * 100)}%`;
      }
    }
  }, { passive: false });

  diaBtn.onclick = () => {
    diaBtn.classList.add('active');
    srcBtn.classList.remove('active');
    vp.style.display = 'flex';
    pre.style.display = 'none';
  };
  srcBtn.onclick = () => {
    srcBtn.classList.add('active');
    diaBtn.classList.remove('active');
    vp.style.display = 'none';
    pre.style.display = 'block';
  };
  copyBtn.onclick = () => {
    navigator.clipboard.writeText(code).then(() => toast('Diagram code copied!'));
  };

  try {
    const cleanCode = (code || '').trim();
    const id = 'mermaid-modal-' + Math.random().toString(36).slice(2, 9);
    const { svg } = await mermaid.render(id, cleanCode);
    vp.innerHTML = svg;
    const svgEl = vp.querySelector('svg');
    if (svgEl) {
      svgEl.style.maxWidth = '100%';
      svgEl.style.maxHeight = '100%';
      svgEl.style.height = 'auto';
      svgEl.style.display = 'block';
    }
  } catch (err) {
    console.error('Modal mermaid error:', err);
    vp.innerHTML = `<div style="color:var(--amber); font-size:12px; padding:16px;">⚠️ Could not render diagram (${esc(err.message || 'Syntax error')}). You can view the raw code using the 📝 Source button.</div>`;
  }
}


// 3. CSV Viewer
function renderCsvPreview(csvText, container, controls) {
  if (!csvText) {
    container.innerHTML = '<div style="padding:20px; color:var(--dim);">File is empty.</div>';
    return;
  }
  const lines = csvText.trim().split(/\r?\n/).slice(0, 1000); // Display up to 1000 rows
  if (!lines.length) {
    container.innerHTML = '<div style="padding:20px; color:var(--dim);">No records found.</div>';
    return;
  }

  // Parse simple CSV line (respecting quotes)
  function parseCsvRow(rowStr) {
    const res = [];
    let cur = '';
    let inQuote = false;
    for (let i = 0; i < rowStr.length; i++) {
      const c = rowStr[i];
      if (c === '"') inQuote = !inQuote;
      else if (c === ',' && !inQuote) {
        res.push(cur);
        cur = '';
      } else cur += c;
    }
    res.push(cur);
    return res.map(s => s.replace(/^"|"$/g, '').trim());
  }

  const headers = parseCsvRow(lines[0]);
  const rows = lines.slice(1).map(parseCsvRow);

  controls.innerHTML = `
    <span style="font-size:11px; color:var(--dim);">${rows.length.toLocaleString()} row${rows.length !== 1 ? 's' : ''}</span>
  `;

  let tableHtml = `<div class="table-preview-wrapper"><table class="preview-table"><thead><tr>`;
  tableHtml += `<th style="width:40px; color:var(--dim); text-align:center;">#</th>`;
  headers.forEach(h => { tableHtml += `<th>${esc(h)}</th>`; });
  tableHtml += `</tr></thead><tbody>`;

  rows.forEach((r, idx) => {
    tableHtml += `<tr><td style="color:var(--dim); text-align:center;">${idx + 1}</td>`;
    for (let i = 0; i < headers.length; i++) {
      tableHtml += `<td>${esc(r[i] !== undefined ? r[i] : '')}</td>`;
    }
    tableHtml += `</tr>`;
  });
  tableHtml += `</tbody></table></div>`;

  container.innerHTML = tableHtml;
}

// 4. Excel Multi-Sheet Viewer (using XLSX / SheetJS)
function renderExcelPreview(arrayBuffer, container, controls) {
  if (typeof XLSX === 'undefined') {
    container.innerHTML = '<div style="padding:24px; color:var(--amber); font-size:12px;">SheetJS library is loading or blocked by network. You can download the file directly via the button in the header.</div>';
    return;
  }
  try {
    const data = new Uint8Array(arrayBuffer);
    const workbook = XLSX.read(data, { type: 'array' });
    const sheetNames = workbook.SheetNames || [];

    if (!sheetNames.length) {
      container.innerHTML = '<div style="padding:20px; color:var(--dim);">No sheets found in workbook.</div>';
      return;
    }

    let activeSheet = sheetNames[0];

    function showSheet(sheetName) {
      activeSheet = sheetName;
      const sheet = workbook.Sheets[sheetName];
      const jsonRows = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '' });

      const tabsBar = container.querySelector('.sheet-tabs-bar');
      if (tabsBar) {
        tabsBar.querySelectorAll('.sheet-tab-btn').forEach(btn => {
          btn.classList.toggle('active', btn.dataset.sheet === sheetName);
        });
      }

      const tableBox = container.querySelector('#excel-table-box');
      if (!jsonRows || !jsonRows.length) {
        controls.innerHTML = `<span style="font-size:11px; color:var(--dim);">0 rows</span>`;
        tableBox.innerHTML = '<div style="padding:20px; color:var(--dim);">Sheet is empty.</div>';
        return;
      }

      // Find max column count across all rows in case row 0 is shorter than other rows
      let maxCols = 0;
      jsonRows.forEach(r => { if (Array.isArray(r) && r.length > maxCols) maxCols = r.length; });
      if (maxCols === 0) maxCols = 1;

      const rawHeaders = jsonRows[0] || [];
      const headers = [];
      for (let i = 0; i < maxCols; i++) {
        headers.push(rawHeaders[i] !== undefined && String(rawHeaders[i]).trim() !== '' ? String(rawHeaders[i]) : `Col ${i + 1}`);
      }

      const rows = jsonRows.slice(1);
      controls.innerHTML = `
        <span style="font-size:11px; color:var(--dim);">${rows.length.toLocaleString()} row${rows.length !== 1 ? 's' : ''} · ${maxCols} column${maxCols !== 1 ? 's' : ''}</span>
      `;

      let html = `<table class="preview-table"><thead><tr>`;
      html += `<th style="width:40px; color:var(--dim); text-align:center;">#</th>`;
      headers.forEach((h) => {
        html += `<th>${esc(h)}</th>`;
      });
      html += `</tr></thead><tbody>`;

      rows.forEach((r, idx) => {
        let tableHtmlRow = `<tr><td style="color:var(--dim); text-align:center;">${idx + 1}</td>`;
        for (let i = 0; i < maxCols; i++) {
          const val = (r && r[i] !== undefined) ? r[i] : '';
          tableHtmlRow += `<td>${esc(String(val))}</td>`;
        }
        tableHtmlRow += `</tr>`;
        html += tableHtmlRow;
      });
      html += `</tbody></table>`;
      tableBox.innerHTML = html;
    }

    let uiHtml = `<div class="sheet-tabs-bar">`;
    sheetNames.forEach(name => {
      uiHtml += `<button class="sheet-tab-btn ${name === activeSheet ? 'active' : ''}" data-sheet="${esc(name)}">📄 ${esc(name)}</button>`;
    });
    uiHtml += `</div><div class="table-preview-wrapper" id="excel-table-box"></div>`;

    container.innerHTML = uiHtml;

    container.querySelectorAll('.sheet-tab-btn').forEach(btn => {
      btn.onclick = () => showSheet(btn.dataset.sheet);
    });

    showSheet(activeSheet);
  } catch (err) {
    container.innerHTML = `<div style="padding:24px; color:var(--red); font-size:12px;">Failed to parse Excel workbook: ${esc(err.message)}</div>`;
  }
}

// 5. PDF Viewer
function renderPdfPreview(url, container, controls) {
  controls.innerHTML = `
    <span style="font-size:11px; color:var(--dim);">Native browser PDF reader</span>
  `;
  container.innerHTML = `<div style="padding:40px 24px; text-align:center; color:var(--dim); font-size:12px;">Loading PDF…</div>`;

  fetch(url).then(async res => {
    const contentType = res.headers.get('content-type') || '';
    if (res.ok && contentType.includes('application/pdf')) {
      container.innerHTML = `<iframe class="preview-frame" src="${url}#toolbar=1" style="width:100%; height:100%; border:none; background:#525659;"></iframe>`;
    } else {
      const errJson = await res.json().catch(() => ({}));
      container.innerHTML = `
        <div style="padding:40px 24px; text-align:center; color:var(--dim); font-size:12px; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100%;">
          <span style="font-size:32px; margin-bottom:12px;">📁</span>
          <div style="font-weight:600; color:var(--text); font-size:14px; margin-bottom:6px;">File Not Found On Disk</div>
          <div style="max-width:440px; margin-bottom:16px; line-height:1.5;">${esc(errJson.error || 'The requested file could not be located in workspace or storage.')}</div>
          <div class="dim" style="font-size:11px;">If this was generated in a previous chat turn, ask the assistant to write or export it again.</div>
        </div>`;
    }
  }).catch(err => {
    container.innerHTML = `<div style="padding:24px; color:var(--red); font-size:12px;">Failed to load PDF: ${esc(err.message)}</div>`;
  });
}

// 5b. Image Viewer
function renderImagePreview(url, container, controls) {
  controls.innerHTML = `
    <button class="btn ghost device-toggle-btn" id="btn-img-zoom-in" title="Zoom In">🔍＋</button>
    <button class="btn ghost device-toggle-btn" id="btn-img-zoom-out" title="Zoom Out">🔍－</button>
    <button class="btn ghost device-toggle-btn" id="btn-img-zoom-reset" title="Reset Zoom">100%</button>
    <a class="btn ghost device-toggle-btn" href="${url}" target="_blank" rel="noopener">↗ Open Full</a>
  `;
  container.innerHTML = `
    <div id="modal-img-viewport" style="flex:1; overflow:auto; display:flex; align-items:center; justify-content:center; padding:24px; background:var(--bg); user-select:none;">
      <img src="${url}" style="max-width:100%; max-height:100%; object-fit:contain; border-radius:8px; box-shadow:0 8px 30px rgba(0,0,0,0.5); transition:transform 0.15s ease;" />
    </div>
  `;
  const imgEl = container.querySelector('img');
  let currentZoom = 1.0;
  function applyZoom() {
    if (imgEl) imgEl.style.transform = `scale(${currentZoom})`;
  }
  const inBtn = controls.querySelector('#btn-img-zoom-in');
  const outBtn = controls.querySelector('#btn-img-zoom-out');
  const resetBtn = controls.querySelector('#btn-img-zoom-reset');
  if (inBtn) inBtn.onclick = () => { currentZoom = Math.min(4.0, currentZoom + 0.25); applyZoom(); };
  if (outBtn) outBtn.onclick = () => { currentZoom = Math.max(0.25, currentZoom - 0.25); applyZoom(); };
  if (resetBtn) resetBtn.onclick = () => { currentZoom = 1.0; applyZoom(); };
}

// 6. Markdown Modal Preview
function renderMarkdownModalPreview(mdText, container, controls) {
  controls.innerHTML = `
    <button class="btn ghost device-toggle-btn active" id="btn-md-rich">📄 Rendered</button>
    <button class="btn ghost device-toggle-btn" id="btn-md-raw">📝 Source</button>
  `;
  container.innerHTML = `
    <div id="modal-md-rich" style="flex:1; padding:24px 32px; overflow:auto; line-height:1.6; color:var(--text);" class="bubble">${md(mdText)}</div>
    <pre id="modal-md-raw" style="display:none; flex:1; margin:0; padding:16px; overflow:auto; background:var(--bg-code); font-size:12px; font-family:monospace; color:var(--text); line-height:1.5;"><code>${esc(mdText)}</code></pre>
  `;
  const richDiv = container.querySelector('#modal-md-rich');
  const rawPre = container.querySelector('#modal-md-raw');
  const rBtn = controls.querySelector('#btn-md-rich');
  const sBtn = controls.querySelector('#btn-md-raw');

  rBtn.onclick = () => {
    rBtn.classList.add('active');
    sBtn.classList.remove('active');
    richDiv.style.display = 'block';
    rawPre.style.display = 'none';
  };
  sBtn.onclick = () => {
    sBtn.classList.add('active');
    rBtn.classList.remove('active');
    richDiv.style.display = 'none';
    rawPre.style.display = 'block';
  };
}

// 7. General Code / Text Preview
function renderCodePreview(text, ext, container, controls) {
  const lang = (typeof hlLangFor === 'function') ? hlLangFor(`file.${ext}`) : '';
  controls.innerHTML = `
    <button class="btn ghost device-toggle-btn" id="btn-code-copy">📋 Copy</button>
  `;
  container.innerHTML = `<pre style="flex:1; margin:0; padding:16px; overflow:auto; background:var(--bg-code); font-size:12px; font-family:monospace; color:var(--text); line-height:1.5;"><code>${(typeof hlCode === 'function' && lang) ? hlCode(text, lang) : esc(text)}</code></pre>`;
  
  const copyBtn = controls.querySelector('#btn-code-copy');
  if (copyBtn) {
    copyBtn.onclick = () => {
      navigator.clipboard.writeText(text).then(() => toast('Copied to clipboard!'));
    };
  }
}

function closeFilePreview() {
  const modal = $('preview-modal');
  if (modal) {
    modal.hidden = true;
    modal.style.display = 'none';
    const c = $('preview-content');
    if (c) c.innerHTML = '';
  }
}

window.openFilePreview = openFilePreview;
window.closeFilePreview = closeFilePreview;
window.renderInlineMermaid = renderInlineMermaid;
