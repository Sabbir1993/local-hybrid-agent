import React, { useEffect, useState } from 'react';
import { useAppStore, type ChatMessage } from '../../stores/useAppStore';
import { MEDIA_SETUP_URL, cancelMedia, confirmCloud, dismissMedia, editMediaPrompt, editResultPicture, fmtClock, loadModelAndRun, makeAnother, mediaStatusNow, picCaps, rawUrl, retryMedia, type MediaMeta } from '../../services/media';

/** The assistant bubble of an /image or /video job: progress, questions, errors, and the finished result. */
export const MediaCard: React.FC<{ m: ChatMessage }> = ({ m }) => {
  const md: MediaMeta = m.meta.media;
  const { openPreviewModal, showToast } = useAppStore();
  const word = md.kind === 'video' ? 'video' : 'image';
  const icon = md.kind === 'video' ? '🎬' : '🎨';
  const busy = md.state === 'starting' || md.state === 'running' || (md.state === 'not_loaded' && md.loading);

  // elapsed clock while something is in progress
  const [, tick] = useState(0);
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [busy]);
  const elapsed = md.t0 ? fmtClock((Date.now() - md.t0) / 1000) : '0:00';
  const prompt = <div className="media-prompt">“{md.prompt}”</div>;

  if (md.state === 'confirm') {
    return (
      <div className="media-live media-confirm" role="alert">
        <div>
          💳 This {word} will be made by <b>{md.provider || 'a cloud provider'}</b>
          {md.cloudModel ? ` (${md.cloudModel})` : ''} in the cloud and <b>may cost money</b>.
        </div>
        {prompt}
        <div className="media-actions-row">
          <button type="button" className="btn accent" onClick={() => confirmCloud(m.id)}>Make it</button>
          <button type="button" className="btn ghost" onClick={() => dismissMedia(m.id)}>Cancel</button>
        </div>
      </div>
    );
  }

  if (md.state === 'not_loaded') {
    const what = md.laneLabel ? <b>{md.laneLabel}</b> : <>the {word} model</>;
    if (md.loading) {
      return (
        <div className="media-live" role="status" aria-live="polite">
          <div className="media-live-head">
            <span className="ac-shimmer" />
            <span>{icon} Loading {what} on this PC · {elapsed}</span>
          </div>
          {prompt}
          <div className="media-live-text dim">
            Big models take a few minutes to load. Your {word} starts as soon as it’s ready, and the model stays loaded for the next one.
          </div>
        </div>
      );
    }
    return (
      <div className="media-live media-confirm" role="note">
        <div>{icon} {what} runs on this PC and isn’t loaded yet.</div>
        {prompt}
        <div className="media-actions-row">
          {md.canLoad
            ? <button type="button" className="btn accent" onClick={() => loadModelAndRun(m.id)}>▶ Load it and make the {word}</button>
            : <span className="dim">Ask an admin to load it.</span>}
          <button type="button" className="btn ghost" onClick={() => dismissMedia(m.id)}>Cancel</button>
        </div>
      </div>
    );
  }

  if (md.state === 'error' || md.state === 'cancelled') {
    return (
      <div className="media-live media-error" role="note">
        <div>
          {md.state === 'cancelled'
            ? `⏹ Stopped waiting for the ${word}.`
            : <>⚠ Couldn’t make the {word}: {md.error || ''}{md.notSetup && <> <a href={MEDIA_SETUP_URL}>Set it up</a></>}</>}
        </div>
        {prompt}
        <div className="media-actions-row">
          <button type="button" className="btn ghost" onClick={() => retryMedia(m.id)}>↻ Try again</button>
          <button type="button" className="btn ghost" onClick={() => editMediaPrompt(md.kind, md.prompt)}>✎ Edit description</button>
        </div>
      </div>
    );
  }

  if (md.state === 'done') {
    const files = md.files || [];
    const caps = picCaps(mediaStatusNow());
    return (
      <div className="media-done">
        {files.map((f) => {
          const url = rawUrl(f.path);
          return /^video\//.test(f.mime || '') || md.kind === 'video' ? (
            <video key={f.path} src={url} controls style={{ maxWidth: '100%', borderRadius: 8 }} />
          ) : (
            <img
              key={f.path}
              src={url}
              alt={md.prompt}
              style={{ maxWidth: '100%', maxHeight: 420, borderRadius: 8, cursor: 'zoom-in', marginRight: 6 }}
              onClick={(e) => { e.stopPropagation(); openPreviewModal(url, 'Attachment', null, 'modal', true); }}
            />
          );
        })}
        <div className="media-done-row" style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginTop: 6 }}>
          <span className="dim" style={{ fontSize: 11.5 }}>
            Made with {md.model || '?'}{md.where ? ` (${md.where})` : ''}{md.secs ? ` · ${md.secs}s` : ''}
          </span>
          <button type="button" className="btn ghost media-mini" title="Same description, new variation" onClick={() => makeAnother(m.id)}>↻ Make another</button>
          {md.kind === 'image' && files.some((f) => /^image\//.test(f.mime || '')) && (caps.img2img || caps.refs) && (
            <button type="button" className="btn ghost media-mini" title="Start a new image from this one" onClick={() => editResultPicture(m.id)}>✏️ Edit this picture</button>
          )}
          <button type="button" className="btn ghost media-mini" onClick={() => editMediaPrompt(md.kind, md.prompt)}>✎ Edit description</button>
          <button type="button" className="btn ghost media-mini" onClick={() => { navigator.clipboard?.writeText(md.prompt).then(() => showToast('Description copied', 'info'), () => {}); }}>⧉ Copy description</button>
        </div>
      </div>
    );
  }

  // starting / running
  return (
    <div className="media-live" role="status" aria-live="polite">
      <div className="media-live-head">
        <span className="ac-shimmer" />
        <span>{icon} Making {word === 'image' ? 'an image' : 'a video'}{md.model ? <> with <b>{md.model}</b></> : null} · {elapsed}</span>
      </div>
      {prompt}
      <div className="media-live-text dim">{md.text || ''}{md.kind === 'video' ? ' · videos can take several minutes' : ''}</div>
      {typeof md.pct === 'number' && (
        <div className="media-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={md.pct}>
          <span style={{ width: `${Math.max(3, Math.min(100, md.pct))}%` }} />
        </div>
      )}
      <div className="media-actions-row">
        <button type="button" className="btn ghost" onClick={() => cancelMedia(m.id)}>Cancel</button>
      </div>
    </div>
  );
};
