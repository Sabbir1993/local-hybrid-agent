import React, { useEffect, useState, useSyncExternalStore } from 'react';
import {
  MEDIA_SETUP_URL, MEDIA_SIZES, MEDIA_SIZE_WORD, PIC_OK, cachedMediaStatus, compModeStore, editPicsStore, mediaOpts, mediaReady, mediaSizeFor,
  picAttachments, picCaps, picMode, picStrength, saveMediaOpts, setCompMode, setEditPics, setPicMode, setPicStrength, whereText,
  type MediaKind, type MediaStatus,
} from '../../services/media';

interface PicAtt { id: string; name: string; isImage?: boolean; b64?: string; mime?: string; dataUrl?: string }

/** The composer's "Chat / Image" select: what Send does. Shown once an image model is set up. */
export const ImageModeSelect: React.FC = () => {
  const mode = useSyncExternalStore(compModeStore.subscribe, compModeStore.snapshot);
  const [st, setSt] = useState<MediaStatus | null>(null);
  useEffect(() => { void cachedMediaStatus().then(setSt); }, []);
  if (!mediaReady('image', st)) return null;
  return (
    <select
      id="comp-mode"
      value={mode}
      onChange={(e) => setCompMode(e.target.value === 'image' ? 'image' : 'chat')}
      title={`What Send does: Chat, or make an image with ${whereText(st?.image)}`}
      aria-label="What Send does"
      style={{ background: 'transparent', border: 'none', color: 'var(--dim)', fontSize: 12, cursor: 'pointer', padding: '2px 0' }}
    >
      <option value="chat">💬 Chat</option>
      <option value="image">🖼 Image</option>
    </select>
  );
};

/**
 * The options row above the composer while a /image or /video command is typed (or Image mode is on):
 * shape, size, length, and for images the pictures to start from. Choices are saved in media_opts_image / media_opts_video.
 */
export const MediaOptions: React.FC<{ text: string; attachments: PicAtt[] }> = ({ text, attachments }) => {
  const mode = useSyncExternalStore(compModeStore.subscribe, compModeStore.snapshot);
  const editPics = useSyncExternalStore(editPicsStore.subscribe, editPicsStore.snapshot);
  const [st, setSt] = useState<MediaStatus | null>(null);
  const [, bump] = useState(0);   // re-read the saved choices after a click

  const cmd = text.match(/^\/(image|video)(?=\s|$)/i);
  const kind: MediaKind | null = cmd ? (cmd[1].toLowerCase() as MediaKind) : mode === 'image' && !text.startsWith('/') ? 'image' : null;
  useEffect(() => { if (kind) void cachedMediaStatus().then(setSt); }, [kind]);
  if (!kind) return null;
  if (!cmd && !mediaReady('image', st)) return null;   // Image mode without a ready image model: Send goes to chat, so no image options

  const o = mediaOpts(kind);
  const info = st?.[kind];
  const set = (patch: Partial<typeof o>) => { saveMediaOpts(kind, patch); bump((n) => n + 1); };
  const atts = kind === 'image' ? picAttachments(attachments) : [];
  const nPics = atts.length + editPics.length;

  const who = info && info.ready
    ? <span className="media-who">{info.cloud ? '☁' : '🖥'} {whereText(info)}</span>
    : st ? <a className="media-who warn" href={MEDIA_SETUP_URL}>Not set up yet — set it up</a> : null;

  // the size picker; shape = null when the pictures decide the shape (cloud models pick their own sizes)
  const sizeSelect = (shape: string | null) => {
    if (kind !== 'image' || (info && info.cloud)) return null;
    const def = info && MEDIA_SIZES[info.default_size] ? info.default_size : 'large';
    const label = (k: string) => (shape ? mediaSizeFor(k, shape).join('×') : `~${+(MEDIA_SIZES[k] ** 2 / 1e6).toFixed(2)} MP`);
    return (
      <label className="media-len" title="Bigger images take longer (Large is about 4× Small)">Size{' '}
        <select value={o.size || ''} aria-label="Image size" onChange={(e) => set({ size: e.target.value || undefined })}>
          <option value="">Model default · {MEDIA_SIZE_WORD[def]} {label(def)}</option>
          {Object.keys(MEDIA_SIZES).map((k) => <option key={k} value={k}>{MEDIA_SIZE_WORD[k]} · {label(k)}</option>)}
        </select>
      </label>
    );
  };

  if (kind === 'image' && nPics > 0) {
    const caps = picCaps(st);
    const pm = picMode(nPics, caps);
    const strength = picStrength();
    const seg = (v: 'img2img' | 'edit', label: string, ok: boolean, why: string) => (
      <button type="button" className={`media-seg${pm === v ? ' on' : ''}`} aria-pressed={pm === v} disabled={!ok} title={ok ? undefined : why}
        onClick={() => { setPicMode(v); bump((n) => n + 1); }}>{label}</button>
    );
    let warn = '';
    if (atts.some((a) => !PIC_OK.test(a.mime || ''))) warn = 'Only PNG, JPEG or WEBP pictures can be used.';
    else if (nPics > 1 && !caps.refs) warn = 'This image model can’t combine pictures yet — add its vision weights in Settings → Models & Jobs.';
    else if (pm === 'edit' && caps.max_refs && nPics > caps.max_refs) warn = `This model takes at most ${caps.max_refs} pictures.`;
    else if (!caps.img2img && !caps.refs) warn = 'Load the image model on this PC to work from pictures.';
    return (
      <div className="media-pics" role="group" aria-label="Pictures to start from">
        <span className="media-pics-t">🖼 Start from</span>
        {editPics.map((p, i) => (
          <span key={p.path} className="media-pic" title={p.name}>
            <img src={'/agent/raw?path=' + encodeURIComponent(p.path)} alt="" /><code>&lt;image{i + 1}&gt;</code>
            <button type="button" aria-label={`Don’t use ${p.name}`} onClick={() => setEditPics(editPics.filter((x) => x.path !== p.path))}>✕</button>
          </span>
        ))}
        {atts.map((a, i) => (
          <span key={a.id} className={`media-pic${PIC_OK.test(a.mime || '') ? '' : ' bad'}`} title={a.name}>
            <img src={a.dataUrl} alt="" /><code>&lt;image{editPics.length + i + 1}&gt;</code>
          </span>
        ))}
        {seg('img2img', '🎨 Change this picture', caps.img2img && nPics === 1, nPics > 1 ? 'Only with one picture' : 'The image model can’t do this')}
        {seg('edit', '🧩 Edit / combine', caps.refs, 'Needs the model’s vision weights — add them in Settings → Models & Jobs')}
        {pm === 'img2img'
          ? (
            <label className="media-strength">How much to change{' '}
              <input type="range" min="0.1" max="1" step="0.05" value={strength} aria-label="How much to change"
                onChange={(e) => { setPicStrength(+e.target.value); bump((n) => n + 1); }} />{' '}
              <output>{Math.round(strength * 100)}%</output>
            </label>
          )
          : <span className="dim">Say what to do, e.g. “put the logo from &lt;image2&gt; on the shirt in &lt;image1&gt;”</span>}
        {sizeSelect(null)}
        <span className="dim media-pics-note" title="Pictures are never sent to a cloud model">🔒 stays on this PC</span>
        {warn && <div className="media-pics-warn" role="alert">⚠ {warn}</div>}
      </div>
    );
  }

  return (
    <div className="media-opts" role="group" aria-label={`${kind} options`}>
      {([['square', '◻ Square'], ['wide', '▭ Wide'], ['tall', '▯ Tall']] as const).map(([v, l]) => (
        <button key={v} type="button" className={`media-seg${o.aspect === v ? ' on' : ''}`} aria-pressed={o.aspect === v} onClick={() => set({ aspect: v })}>{l}</button>
      ))}
      {sizeSelect(o.aspect)}
      {kind === 'video' && (
        <label className="media-len">Length{' '}
          <select value={o.seconds} aria-label="Video length" onChange={(e) => set({ seconds: +e.target.value })}>
            {[4, 8, 12].map((s) => <option key={s} value={s}>{s} s</option>)}
          </select>
        </label>
      )}
      {who}
    </div>
  );
};
