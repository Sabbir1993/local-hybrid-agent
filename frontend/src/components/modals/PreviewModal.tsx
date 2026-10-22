import React, { useState, useEffect } from 'react';
import { useAppStore } from '../../stores/useAppStore';

export const PreviewModal: React.FC = () => {
  const { previewModal, closePreviewModal } = useAppStore();
  const { isOpen, filePath, title, directContent, source } = previewModal;

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [textContent, setTextContent] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1.0);
  const [deviceWidth, setDeviceWidth] = useState<'100%' | '768px' | '390px'>('100%');
  const [iframeKey, setIframeKey] = useState(1);
  const [imgSrc, setImgSrc] = useState('');

  const cleanPath = (filePath || '').split('?')[0];
  const fname = (cleanPath || title || 'file').split('\\').pop()?.split('/').pop() || 'file';
  const ext = fname.includes('.') ? fname.split('.').pop()?.toLowerCase() || '' : '';

  const enc = encodeURIComponent(filePath || '');
  const isWs = source === 'ws';
  const isDirectUrl = filePath.startsWith('http://') || filePath.startsWith('https://') || filePath.startsWith('/agent/') || filePath.startsWith('data:') || filePath.startsWith('blob:');
  const rawUrl = isDirectUrl
    ? filePath
    : isWs
      ? `/agent/ws/raw?path=${enc}`
      : `/agent/raw?path=${enc}`;
  const dlUrl = filePath.startsWith('http') || filePath.startsWith('data:') || filePath.startsWith('blob:')
    ? filePath
    : `/agent/download?path=${enc}`;

  const isImage = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'ico', 'bmp'].includes(ext) ||
    filePath.startsWith('data:image/') ||
    filePath.startsWith('blob:') ||
    (filePath.startsWith('http') && !filePath.includes('.pdf') && !filePath.includes('.html') && !filePath.includes('.zip') && !filePath.includes('.csv') && !filePath.includes('.xlsx') && !filePath.includes('.pptx')) ||
    (title === 'Attachment' && (ext === 'png' || ext === 'jpg' || ext === 'jpeg' || ext === 'webp' || !ext)) ||
    (typeof title === 'string' && /\.(png|jpe?g|webp|gif|svg|bmp)$/i.test(title));
  const isHtml = ext === 'html' || ext === 'htm';
  const isZip = ext === 'zip';
  const isPdf = ext === 'pdf';
  const isExcel = ext === 'xlsx' || ext === 'xls';
  const isCsv = ext === 'csv';
  const isPptx = ext === 'pptx' || ext === 'ppt';

  // Excel multi-sheet state
  const [sheets, setSheets] = useState<Record<string, string[][]>>({});
  const [activeSheet, setActiveSheet] = useState<string>('');

  // PPTX presentation state
  const [slidesDeck, setSlidesDeck] = useState<{ slides: any[]; width?: number; height?: number } | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    setZoom(1.0);
    setDeviceWidth('100%');
    setError('');
    setTextContent(null);
    setSheets({});
    setActiveSheet('');
    setSlidesDeck(null);
    setImgSrc(rawUrl);

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closePreviewModal();
    };
    window.addEventListener('keydown', handleKeyDown);

    // 1. Direct Content (if supplied)
    if (directContent != null) {
      if (isCsv) {
        const lines = directContent.split(/\r?\n/).filter((l) => l.trim().length > 0);
        const rows = lines.map((l) => l.split(',').map((c) => c.trim().replace(/^"|"$/g, '')));
        setSheets({ 'CSV Data': rows });
        setActiveSheet('CSV Data');
      } else {
        setTextContent(directContent);
      }
      return () => window.removeEventListener('keydown', handleKeyDown);
    }

    // 2. CSV parsing
    if (isCsv) {
      setLoading(true);
      fetch(rawUrl)
        .then(async (r) => {
          if (!r.ok) {
            const alt = await fetch(`/agent/raw?path=${enc}`).catch(() => null);
            if (alt && alt.ok) return alt.text();
            throw new Error(`HTTP ${r.status}`);
          }
          return r.text();
        })
        .then((txt) => {
          const lines = txt.split(/\r?\n/).filter((l) => l.trim().length > 0);
          const rows = lines.map((l) => l.split(',').map((c) => c.trim().replace(/^"|"$/g, '')));
          setSheets({ 'CSV Data': rows });
          setActiveSheet('CSV Data');
          setLoading(false);
        })
        .catch((err) => {
          setError(err.message || 'Failed to parse CSV');
          setLoading(false);
        });
      return () => window.removeEventListener('keydown', handleKeyDown);
    }

    // 3. Excel (XLSX / XLS) parsing with SheetJS
    if (isExcel) {
      setLoading(true);
      const parseWb = (buffer: ArrayBuffer | Uint8Array) => {
        const XLSX = (window as any).XLSX;
        if (!XLSX) throw new Error('SheetJS XLSX library is not loaded');
        const data = new Uint8Array(buffer);
        const workbook = XLSX.read(data, { type: 'array', cellFormula: true, cellDates: true });
        const sheetNames = workbook.SheetNames || [];
        if (!sheetNames.length) throw new Error('Workbook contains no sheets');
        const sData: Record<string, string[][]> = {};
        sheetNames.forEach((sName: string) => {
          const sheet = workbook.Sheets[sName];
          const json = XLSX.utils.sheet_to_json(sheet, { header: 1, defval: '', raw: false });
          sData[sName] = json as string[][];
        });
        setSheets(sData);
        setActiveSheet(sheetNames[0]);
        setLoading(false);
      };

      if (filePath.startsWith('data:')) {
        try {
          const b64 = filePath.split(',')[1] || '';
          const binary = atob(b64);
          const bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
          parseWb(bytes.buffer);
        } catch (e: any) {
          setError('Failed to read Excel data: ' + e.message);
          setLoading(false);
        }
      } else {
        fetch(rawUrl)
          .then(async (r) => {
            if (!r.ok) {
              const alt = await fetch(`/agent/raw?path=${enc}`).catch(() => null);
              if (alt && alt.ok) return alt.arrayBuffer();
              throw new Error(`HTTP ${r.status}`);
            }
            return r.arrayBuffer();
          })
          .then((ab) => parseWb(ab))
          .catch((err) => {
            setError(err.message || 'Failed to load Excel file');
            setLoading(false);
          });
      }
      return () => window.removeEventListener('keydown', handleKeyDown);
    }

    // 4. PPTX Presentation Parsing
    if (isPptx) {
      setLoading(true);
      fetch(`/agent/slides?path=${enc}`)
        .then(async (r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          return r.json();
        })
        .then((deck) => {
          if (deck && Array.isArray(deck.slides) && deck.slides.length > 0) {
            setSlidesDeck(deck);
          } else {
            setSlidesDeck(null);
          }
          setLoading(false);
        })
        .catch(() => {
          setSlidesDeck(null);
          setLoading(false);
        });
      return () => window.removeEventListener('keydown', handleKeyDown);
    }

    // 5. Code & Plain Text Files (excluding binary files)
    const isSpecial = isImage || isHtml || isZip || isPdf || isExcel || isCsv || isPptx;
    if (!isSpecial && filePath && !filePath.startsWith('http') && !filePath.startsWith('data:') && !filePath.startsWith('blob:')) {
      setLoading(true);
      fetch(rawUrl)
        .then(async (r) => {
          if (!r.ok) {
            const alt = await fetch(`/agent/raw?path=${enc}`).catch(() => null);
            if (alt && alt.ok) return alt.text();
            throw new Error(`HTTP ${r.status}`);
          }
          return r.text();
        })
        .then((txt) => {
          setTextContent(txt);
          setLoading(false);
        })
        .catch((err) => {
          setError(err.message || 'Failed to load file');
          setLoading(false);
        });
    }

    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, filePath, directContent, isImage, isHtml, isZip, isPdf, isExcel, isCsv, isPptx, rawUrl, enc, closePreviewModal]);

  if (!isOpen) return null;

  const iconMap: Record<string, string> = {
    html: '🌐',
    htm: '🌐',
    svg: '🖼️',
    png: '🖼️',
    jpg: '🖼️',
    jpeg: '🖼️',
    webp: '🖼️',
    gif: '🖼️',
    pdf: '📕',
    xlsx: '📊',
    xls: '📊',
    csv: '📋',
    pptx: '📽️',
    ppt: '📽️',
    md: '📝',
    py: '🐍',
    js: '⚡',
    ts: '⚡',
    json: '🧩',
    zip: '📦',
  };
  const icon = title === 'Attachment' ? '📎' : (iconMap[ext] || '📄');

  return (
    <div
      id="preview-modal"
      className="fixed inset-0 z-[99999] flex items-center justify-center bg-black/80 backdrop-blur-md select-none"
      onClick={closePreviewModal}
      style={{ display: 'flex' }}
    >
      <div
        id="preview-box"
        className="relative flex flex-col rounded-xl border border-border bg-panel shadow-2xl overflow-hidden w-[94vw] max-w-6xl h-[88vh]"
        onClick={(e) => e.stopPropagation()}
        style={{ background: 'var(--panel, #0f172a)', border: '1px solid var(--border, #334155)' }}
      >
        {/* Header */}
        <div
          id="preview-header"
          className="flex items-center justify-between px-5 py-3 border-b border-border bg-panel2"
          style={{ borderBottom: '1px solid var(--border, #334155)', background: 'var(--panel2, #1e293b)' }}
        >
          <div className="preview-title-group flex items-center gap-2.5 overflow-hidden">
            <span id="preview-icon" style={{ fontSize: '15px' }}>{icon}</span>
            <span id="preview-title" className="mono font-semibold text-xs text-text truncate max-w-md">
              {title || fname}
            </span>
            {title !== 'Attachment' && (
              <span className="badge-main text-[10px] px-2 py-0.5 rounded font-bold uppercase tracking-wider bg-accent/20 text-accent border border-accent/30">
                Preview
              </span>
            )}
          </div>

          <div className="preview-actions flex items-center gap-2">
            {/* HTML Device Toggles */}
            {isHtml && (
              <div className="flex items-center gap-1 mr-2">
                <button
                  type="button"
                  className={`btn ghost text-xs px-2.5 py-1 rounded ${deviceWidth === '100%' ? 'bg-accent text-white font-bold' : ''}`}
                  onClick={() => setDeviceWidth('100%')}
                  title="Desktop (100%)"
                >
                  🖥️ Desktop
                </button>
                <button
                  type="button"
                  className={`btn ghost text-xs px-2.5 py-1 rounded ${deviceWidth === '768px' ? 'bg-accent text-white font-bold' : ''}`}
                  onClick={() => setDeviceWidth('768px')}
                  title="Tablet (768px)"
                >
                  📱 Tablet
                </button>
                <button
                  type="button"
                  className={`btn ghost text-xs px-2.5 py-1 rounded ${deviceWidth === '390px' ? 'bg-accent text-white font-bold' : ''}`}
                  onClick={() => setDeviceWidth('390px')}
                  title="Mobile (390px)"
                >
                  📱 Mobile
                </button>
                <button
                  type="button"
                  className="btn ghost text-xs px-2 py-1 rounded"
                  onClick={() => setIframeKey((k) => k + 1)}
                  title="Refresh"
                >
                  🔄
                </button>
              </div>
            )}

            {/* Image Zoom Controls */}
            {isImage && (
              <div className="flex items-center gap-1 mr-2">
                <button
                  type="button"
                  className="btn ghost text-xs px-2 py-1 rounded"
                  onClick={() => setZoom((z) => Math.min(4.0, z + 0.25))}
                  title="Zoom In"
                >
                  🔍+
                </button>
                <button
                  type="button"
                  className="btn ghost text-xs px-2 py-1 rounded"
                  onClick={() => setZoom((z) => Math.max(0.25, z - 0.25))}
                  title="Zoom Out"
                >
                  🔍-
                </button>
                <button
                  type="button"
                  className="btn ghost text-xs px-2 py-1 rounded"
                  onClick={() => setZoom(1.0)}
                  title="Reset Zoom"
                >
                  {Math.round(zoom * 100)}%
                </button>
              </div>
            )}

            <a
              id="preview-dl"
              href={dlUrl}
              download={fname && fname !== 'file' ? fname : 'attachment.png'}
              className="btn ghost text-xs px-3 py-1 rounded flex items-center gap-1 text-text border border-border hover:bg-panel"
            >
              ⬇ Download
            </a>
            {!filePath.startsWith('data:') && title !== 'Attachment' && (
              <a
                id="preview-raw-link"
                href={rawUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="btn ghost text-xs px-3 py-1 rounded flex items-center gap-1 text-text border border-border hover:bg-panel"
              >
                ↗ New Tab
              </a>
            )}
            <button
              id="preview-close"
              className="btn ghost text-sm px-2.5 py-1 rounded hover:bg-panel text-text-muted hover:text-text"
              onClick={closePreviewModal}
              title="Close (Esc)"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Content Body */}
        <div id="preview-body" className="flex-1 overflow-hidden relative flex flex-col bg-bg">
          {loading && (
            <div className="flex items-center justify-center h-full text-dim text-xs">
              ⏳ Loading file preview…
            </div>
          )}

          {error && (
            <div className="flex items-center justify-center h-full text-red text-xs p-6" style={{ color: 'var(--red, #ef4444)' }}>
              ⚠️ {error}
            </div>
          )}

          {!loading && !error && isImage && (
            <div
              id="modal-img-viewport"
              className="flex-1 overflow-auto flex items-center justify-center p-6 bg-bg"
              style={{ background: 'var(--bg, #0b0f19)' }}
              onWheel={(e) => {
                if (e.ctrlKey) {
                  e.preventDefault();
                  const delta = e.deltaY < 0 ? 0.2 : -0.2;
                  setZoom((z) => Math.min(4.0, Math.max(0.25, z + delta)));
                }
              }}
            >
              <img
                src={imgSrc || rawUrl}
                alt={title || fname}
                title="Click to toggle zoom"
                onClick={() => setZoom((z) => (z === 1.0 ? 1.8 : 1.0))}
                onError={() => {
                  if (isWs && imgSrc !== `/agent/raw?path=${enc}`) {
                    setImgSrc(`/agent/raw?path=${enc}`);
                  }
                }}
                style={{
                  maxWidth: '100%',
                  maxHeight: '100%',
                  objectFit: 'contain',
                  borderRadius: '8px',
                  boxShadow: '0 8px 30px rgba(0,0,0,0.5)',
                  transform: `scale(${zoom})`,
                  transition: 'transform 0.15s ease',
                  cursor: zoom === 1.0 ? 'zoom-in' : 'zoom-out',
                }}
              />
            </div>
          )}

          {!loading && !error && isHtml && (
            <div className="flex-1 w-full h-full flex justify-center bg-[#111] overflow-hidden">
              <iframe
                key={iframeKey}
                src={rawUrl}
                title={title || fname}
                className="preview-frame"
                sandbox="allow-scripts allow-forms allow-popups allow-modals"
                style={{
                  width: deviceWidth,
                  maxWidth: '100%',
                  height: '100%',
                  border: 'none',
                  background: '#ffffff',
                  transition: 'width 0.2s ease',
                }}
              />
            </div>
          )}

          {!loading && !error && isZip && (
            <div className="flex flex-col items-center justify-center h-full text-center p-8 gap-3">
              <span style={{ fontSize: '36px' }}>📦</span>
              <div className="font-semibold text-sm text-text">{fname}</div>
              <p className="text-xs text-dim max-w-sm">
                This is a compressed archive containing project files. Click <b>Download</b> above to save and extract the full site.
              </p>
              <a
                href={dlUrl}
                download={fname}
                className="btn primary text-xs px-4 py-2 rounded-lg font-bold"
                style={{ background: 'var(--accent, #38bdf8)', color: '#000' }}
              >
                ⬇ Download Archive (.zip)
              </a>
            </div>
          )}

          {!loading && !error && isPdf && (
            <div className="flex-1 w-full h-full flex justify-center bg-[#525659] overflow-hidden">
              <iframe
                key={iframeKey}
                src={`${rawUrl}#toolbar=1`}
                title={title || fname}
                className="preview-frame"
                style={{
                  width: '100%',
                  height: '100%',
                  border: 'none',
                  background: '#525659',
                }}
              />
            </div>
          )}

          {!loading && !error && (isExcel || isCsv) && (
            <div className="flex-1 flex flex-col h-full overflow-hidden bg-panel">
              {Object.keys(sheets).length > 1 && (
                <div className="sheet-tabs-bar" style={{ display: 'flex', gap: '4px', padding: '6px 12px', background: 'var(--panel2)', borderBottom: '1px solid var(--border)', overflowX: 'auto', flexShrink: 0 }}>
                  {Object.keys(sheets).map((sName) => (
                    <button
                      key={sName}
                      type="button"
                      className={`sheet-tab-btn ${sName === activeSheet ? 'active' : ''}`}
                      onClick={() => setActiveSheet(sName)}
                      style={{
                        padding: '3px 10px',
                        fontSize: '11px',
                        background: sName === activeSheet ? 'var(--accent-bg, rgba(56,189,248,0.15))' : 'var(--panel)',
                        color: sName === activeSheet ? 'var(--accent, #38bdf8)' : 'var(--dim)',
                        border: sName === activeSheet ? '1px solid var(--accent, #38bdf8)' : '1px solid var(--border)',
                        borderRadius: '4px',
                        cursor: 'pointer',
                        whiteSpace: 'nowrap',
                        fontWeight: sName === activeSheet ? 600 : 400,
                      }}
                    >
                      📊 {sName}
                    </button>
                  ))}
                </div>
              )}
              <div className="table-preview-wrapper" style={{ flex: 1, overflow: 'auto', padding: '12px' }}>
                {!sheets[activeSheet] || !sheets[activeSheet].length ? (
                  <div className="text-dim text-xs p-4">Sheet is empty</div>
                ) : (
                  <table className="preview-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '11.5px', fontFamily: 'monospace', background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: '6px' }}>
                    <thead>
                      <tr>
                        <th style={{ width: '40px', textAlign: 'center', background: 'var(--panel2)', color: 'var(--text)', padding: '7px 10px', borderBottom: '1px solid var(--border)', position: 'sticky', top: 0, zIndex: 2 }}>#</th>
                        {(sheets[activeSheet][0] || []).map((h, hIdx) => (
                          <th key={`th-${hIdx}`} style={{ background: 'var(--panel2)', color: 'var(--text)', padding: '7px 10px', textAlign: 'left', borderBottom: '1px solid var(--border)', borderRight: '1px solid var(--border-subtle)', position: 'sticky', top: 0, zIndex: 2, fontWeight: 600 }}>
                            {String(h || `Col ${hIdx + 1}`)}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {sheets[activeSheet].slice(1, 300).map((row, rIdx) => (
                        <tr key={`tr-${rIdx}`}>
                          <td style={{ textAlign: 'center', color: 'var(--dim)', padding: '5px 10px', borderBottom: '1px solid var(--border-subtle)', borderRight: '1px solid var(--border-subtle)' }}>{rIdx + 1}</td>
                          {(sheets[activeSheet][0] || []).map((_, cIdx) => (
                            <td key={`td-${cIdx}`} style={{ padding: '5px 10px', borderBottom: '1px solid var(--border-subtle)', borderRight: '1px solid var(--border-subtle)', color: 'var(--text)', whiteSpace: 'nowrap', maxWidth: '320px', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                              {String(row[cIdx] !== undefined ? row[cIdx] : '')}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          )}

          {!loading && !error && isPptx && (
            <div className="flex-1 overflow-auto p-6 bg-bg flex flex-col items-center" style={{ background: 'var(--bg, #0b0f19)' }}>
              {slidesDeck && slidesDeck.slides && slidesDeck.slides.length > 0 ? (
                <div className="pptx-deck w-full flex flex-col items-center gap-6" style={{ width: '100%', maxWidth: '960px' }}>
                  {slidesDeck.slides.map((sl: any, sIdx: number) => {
                    const W = slidesDeck.width || 12192000;
                    const H = slidesDeck.height || 6858000;
                    return (
                      <div key={sIdx} className="pptx-slide-wrap relative w-full mb-4" style={{ position: 'relative' }}>
                        <div className="pptx-slide-num text-xs text-dim mb-1 font-semibold" style={{ color: 'var(--dim, #8b949e)', fontSize: '11px', marginBottom: '4px' }}>Slide {sIdx + 1}</div>
                        <div
                          className="pptx-slide relative w-full overflow-hidden rounded-lg shadow-xl"
                          style={{
                            aspectRatio: `${W} / ${H}`,
                            background: sl.bg || '#ffffff',
                            color: '#1e293b',
                            border: '1px solid rgba(255,255,255,0.1)',
                            position: 'relative',
                            borderRadius: '8px',
                            boxShadow: '0 6px 24px rgba(0,0,0,0.45)',
                          }}
                        >
                          {(sl.shapes || []).map((sh: any, shIdx: number) => {
                            const left = `${(sh.x / W) * 100}%`;
                            const top = `${(sh.y / H) * 100}%`;
                            const width = `${(sh.w / W) * 100}%`;
                            const height = `${(sh.h / H) * 100}%`;
                            return (
                              <div
                                key={shIdx}
                                className="pptx-shape absolute"
                                style={{
                                  position: 'absolute',
                                  left,
                                  top,
                                  width,
                                  height,
                                  boxSizing: 'border-box',
                                  overflow: 'hidden',
                                  padding: '4px',
                                  display: 'flex',
                                  flexDirection: 'column',
                                  justifyContent: sh.anchor === 'middle' ? 'center' : 'flex-start',
                                  background: sh.fill || 'transparent',
                                  border: sh.line ? `1px solid ${sh.line}` : 'none',
                                }}
                              >
                                {sh.kind === 'picture' && sh.src ? (
                                  <img src={sh.src} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
                                ) : (
                                  (sh.paras || []).map((p: any, pIdx: number) => (
                                    <div key={pIdx} style={{ textAlign: p.align || 'left', fontSize: `${Math.max(10, (p.style?.size || 16) * 0.9)}px` }}>
                                      {(p.runs || []).map((r: any, rIdx: number) => (
                                        <span
                                          key={rIdx}
                                          style={{
                                            fontWeight: r.bold ? 700 : 400,
                                            fontStyle: r.italic ? 'italic' : 'normal',
                                            color: r.color || '#1e293b',
                                          }}
                                        >
                                          {r.text}
                                        </span>
                                      ))}
                                    </div>
                                  ))
                                )}
                              </div>
                            );
                          })}
                        </div>
                        {sl.notes && <div className="pptx-notes text-xs text-dim mt-2 italic" style={{ fontSize: '11px', color: 'var(--dim)', marginTop: '4px' }}>📝 {sl.notes}</div>}
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="flex flex-col items-center justify-center h-full text-center p-8 gap-3 my-auto">
                  <span style={{ fontSize: '42px' }}>📊</span>
                  <div className="font-semibold text-sm text-text" style={{ fontSize: '14px', color: 'var(--text)' }}>{fname}</div>
                  <p className="text-xs text-dim max-w-sm" style={{ color: 'var(--dim)', fontSize: '12px' }}>
                    PowerPoint Presentation file ready to view or download.
                  </p>
                  <a
                    href={dlUrl}
                    download={fname}
                    className="btn primary text-xs px-4 py-2 rounded-lg font-bold"
                    style={{ background: 'var(--accent, #38bdf8)', color: '#000', textDecoration: 'none', display: 'inline-flex', alignItems: 'center', gap: '6px' }}
                  >
                    ⬇ Download Presentation ({ext.toUpperCase()})
                  </a>
                </div>
              )}
            </div>
          )}

          {!loading && !error && !isImage && !isHtml && !isZip && !isPdf && !isExcel && !isCsv && !isPptx && (
            <div className="flex-1 overflow-auto p-4 bg-bg font-mono text-xs text-text leading-relaxed">
              <pre className="m-0 p-4 rounded-lg bg-panel2 border border-border overflow-x-auto">
                <code>{textContent || '(Empty file)'}</code>
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
