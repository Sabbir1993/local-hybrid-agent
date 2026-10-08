/* voice.js - talk to the agent and hear it answer (hands-free, interruptible).
 *
 *   mic -> worklet frames (16 kHz) -> VAD + endpointing -> one WAV per utterance -> /media/transcribe
 *       -> the normal prompt box + submitPrompt() (chat, agent, custom or Personal Agent: same path as typing)
 *       -> the growing answer is read sentence by sentence -> /media/speak (local CPU TTS) -> speakers
 *
 * What makes it feel like a conversation (all tunable, see voice-core.js for the tested logic):
 *   - eager turn end: the question is sent after ~0.5 s of silence; if you were only pausing, the sent text is
 *     merged into what you say next instead of being answered twice; a sentence that ends on "and" / "but" /
 *     "..." waits a little longer first
 *   - a short acknowledgement ("Mm-hm.") if the real answer is slow
 *   - talking over the agent ducks its voice at once and stops it when it is clear you mean it
 *   - after an interruption the chat history keeps only what you actually heard
 *
 * The network side sits behind VoiceTransport (HTTP here); a WebSocket or WebRTC transport can replace
 * HttpVoiceTransport without touching the loop: transcribe(wavBlob, lang) -> text, speak(text, signal) -> ArrayBuffer(wav).
 * Spoken text is cleaned and card numbers are masked on the server (core/media/tts.py), never trusted to this file.
 * An approval card is never answered by voice: the agent says it is waiting and the card stays on screen.
 */
(function () {
  'use strict';

  const PREROLL_FRAMES = 10;            // 320 ms kept from before the speech start, so first syllables are not clipped
  const IDLE_END_MS = 120000;           // leave voice mode after 2 minutes without anyone speaking
  const SPEAK_GATE = 2.2;               // the mic must be this much louder than usual while the agent is talking
  const DEFAULT_END_MS = 500;           // silence that ends a turn (server config voice.end_silence_ms)
  const EXTRA_WAIT_MS = 700;            // extra wait when the sentence sounds unfinished
  const MERGE_WINDOW_MS = 1500;         // resuming within this long after sending, before any speech: same turn
  const FILLER_AFTER_MS = 1100;         // slow answer: say "Mm-hm." after this long
  const DUCK_GAIN = 0.3;

  const V = window.voiceMode = { active: false, state: 'idle', lastTurn: null, onPermission: null };
  const S = {};                         // live session objects (reset on every start)

  /* ---------------- transport (HTTP) ---------------- */
  class HttpVoiceTransport {
    async transcribe(wav, lang) {
      const fd = new FormData();
      fd.append('file', wav, 'speech.wav');
      fd.append('language', lang || 'auto');
      const r = await fetch('/media/transcribe', { method: 'POST', body: fd });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      return (j.text || '').trim();
    }
    async speak(text, signal) {
      const r = await fetch('/media/speak', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }), signal,
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || ('HTTP ' + r.status));
      }
      return r.arrayBuffer();
    }
  }

  /* ---------------- playback: gapless, cancellable, duckable ---------------- */
  class VoicePlayer {
    constructor(ctx) {
      this.ctx = ctx; this.sources = new Set(); this.next = 0; this.gen = 0; this.onFirstSound = null;
      this.gain = ctx.createGain();
      this.gain.connect(ctx.destination);
      this.started = [];            // [{at, end}] one per scheduled piece: `end` = how much of the answer it covers
    }
    get busy() { return this.sources.size > 0; }
    duck(on) { this.gain.gain.setTargetAtTime(on ? DUCK_GAIN : 1, this.ctx.currentTime, 0.03); }
    // how many answer pieces have begun to play (the rest was never heard)
    startedCount() { const t = this.ctx.currentTime; return this.started.filter((p) => p.end != null && p.at <= t).length; }
    schedule(buf, end) {
      const src = this.ctx.createBufferSource();
      src.buffer = buf;
      src.connect(this.gain);
      const at = Math.max(this.ctx.currentTime + 0.02, this.next);
      src.start(at);
      this.next = at + buf.duration;
      this.sources.add(src);
      src.onended = () => this.sources.delete(src);
      this.started.push({ at, end });
      if (end != null && this.onFirstSound) { const f = this.onFirstSound; this.onFirstSound = null; f(); }
    }
    async play(arrayBuffer, gen, end) {
      const buf = await this.ctx.decodeAudioData(arrayBuffer.slice(0));
      if (gen !== this.gen) return;
      this.schedule(buf, end);
    }
    stop() {
      this.gen++;
      for (const s of this.sources) { try { s.stop(); } catch (_) {} }
      this.sources.clear();
      this.next = 0;
      this.started = [];
      this.duck(false);
    }
  }

  /* sentences in, speech out, in order; the next two are fetched while one plays */
  class SpeakQueue {
    constructor(transport, player, onError) {
      this.transport = transport; this.player = player; this.onError = onError;
      this.items = []; this.draining = false; this.gen = 0; this.ctl = new AbortController();
    }
    isSpeaking() { return this.items.length > 0 || this.draining || this.player.busy; }
    push(text, end) {
      this.items.push({ text, end, p: null });
      if (!this.draining) { this.draining = true; this._drain(this.gen); }
    }
    cancel() {
      this.gen++;
      this.ctl.abort();
      this.ctl = new AbortController();
      this.items = [];
      this.draining = false;
      this.player.stop();
    }
    async _drain(g) {
      while (g === this.gen && this.items.length) {
        for (const it of this.items.slice(0, 3)) {
          if (!it.p) {
            const signal = this.ctl.signal;
            it.p = this.transport.speak(it.text, signal).catch((e) => { if (e.name !== 'AbortError') this.onError(e); return null; });
          }
        }
        const it = this.items[0];
        const ab = await it.p;
        if (g !== this.gen) return;
        this.items.shift();
        if (ab) { try { await this.player.play(ab, this.player.gen, it.end); } catch (e) { this.onError(e); } }
      }
      if (g === this.gen) this.draining = false;
    }
  }

  /* ---------------- helpers ---------------- */
  const $id = (id) => document.getElementById(id);
  const note = (msg, bad) => { if (typeof toast === 'function') toast(msg, !!bad); else console.warn('[voice]', msg); };

  function isBusy() {
    try {
      if (typeof generating !== 'undefined' && generating) return true;
      if (typeof curSession !== 'undefined' && curSession && window.bgJobs && window.bgJobs.has(String(curSession.id))) return true;
    } catch (_) {}
    return false;
  }

  function concatFrames(frames) {
    let n = 0;
    for (const f of frames) n += f.length;
    const out = new Float32Array(n);
    let o = 0;
    for (const f of frames) { out.set(f, o); o += f.length; }
    return out;
  }

  const LABELS = {
    listening: 'Listening… just talk',
    hearing: 'Hearing you…',
    thinking: 'Thinking…',
    speaking: 'Speaking… talk over me to interrupt',
  };

  function setState(st) {
    V.state = st;
    const el = $id('voice-panel');
    if (el) { el.dataset.state = st; const l = $id('voice-label'); if (l) l.textContent = LABELS[st] || ''; }
  }

  function paintLevel(level) {
    const el = $id('voice-level');
    if (el) el.style.transform = 'scaleX(' + Math.min(1, level * 12).toFixed(2) + ')';
  }

  // language the person speaks: "auto" lets whisper guess per clip, which is unreliable on short sentences
  function sttLang() {
    const sel = $id('voice-lang');
    const v = (sel && sel.value) || localStorage.getItem('stt_lang') || 'auto';
    return v === 'auto' ? 'auto' : v;
  }

  function paintHeard(text) {
    const el = $id('voice-heard');
    if (el) el.textContent = text ? '“' + text + '”' : '';
  }

  function paintLatency(t) {
    const el = $id('voice-lat');
    if (!el || !t) return;
    const s = (ms) => (ms == null ? '–' : (ms / 1000).toFixed(1) + 's');
    el.textContent = 'last turn: heard ' + s(t.transcribeMs) + ' · first sentence ' + s(t.firstSentenceMs) + ' · first sound ' + s(t.firstAudioMs);
  }

  function showPanel(on) {
    let el = $id('voice-panel');
    if (!on) { if (el) el.remove(); return; }
    if (el) return;
    el = document.createElement('div');
    el.id = 'voice-panel';
    el.setAttribute('role', 'status');
    el.innerHTML = '<div class="voice-orb" aria-hidden="true"></div>'
      + '<div class="voice-body"><div id="voice-label" class="voice-label"></div>'
      + '<div class="voice-meter" aria-hidden="true"><div id="voice-level"></div></div>'
      + '<div id="voice-heard" class="voice-heard"></div>'
      + '<div class="voice-row"><label class="voice-hint" for="voice-lang">I speak</label>'
      + '<select id="voice-lang" class="voice-lang" title="Pick your language for better recognition (Auto guesses for each sentence)">'
      + '<option value="auto">Auto</option><option value="en">English</option><option value="bn">বাংলা</option></select></div>'
      + '<div class="voice-hint">Headphones work best. Approvals stay on screen.</div>'
      + '<div id="voice-lat" class="voice-hint"></div></div>'
      + '<button id="voice-end" type="button" class="voice-end" title="End voice conversation (Esc)">End</button>';
    document.body.appendChild(el);
    $id('voice-end').onclick = () => V.stop();
    const sel = $id('voice-lang');
    const saved = localStorage.getItem('stt_lang') || 'auto';
    sel.value = ['auto', 'en', 'bn'].includes(saved) ? saved : 'auto';
    sel.onchange = () => { try { localStorage.setItem('stt_lang', sel.value); } catch (_) {} };
  }

  function paintButton() {
    const b = $id('btn-voice');
    if (!b) return;
    b.classList.toggle('recording', V.active);
    b.setAttribute('aria-pressed', V.active ? 'true' : 'false');
    const t = V.active ? 'End the voice conversation' : 'Talk with the agent by voice (hands-free)';
    b.title = t; b.setAttribute('aria-label', t);
  }

  /* ---------------- the loop ---------------- */
  function onFrame(f) {
    if (!V.active) return;
    S.ring.push(f);
    if (S.ring.length > PREROLL_FRAMES) S.ring.shift();
    const playing = S.queue.isSpeaking();
    const listening = V.state === 'listening';
    const voiced = S.vad.isVoiced(f, playing ? SPEAK_GATE : 1, listening && !S.ep.inSpeech);
    paintLevel(S.vad.level);
    if (voiced) S.lastActivity = performance.now();

    if (V.state === 'listening' || V.state === 'hearing') {
      if (V.state === 'hearing') S.utter.push(f);
      const ev = S.ep.feed(voiced);
      if (ev === 'start' && V.state === 'listening') {
        S.utter = S.ring.slice();
        setState('hearing');
      } else if (ev === 'end' || ev === 'max') {
        finishUtterance();
      } else if (ev === 'short') {
        S.utter = [];
        setState('listening');
      } else if (V.state === 'listening' && performance.now() - S.lastActivity > IDLE_END_MS) {
        note('Voice conversation ended: no speech for 2 minutes.');
        V.stop();
      }
    } else {                                    // thinking or speaking: did the person start talking?
      if (playing) {                            // duck at the first sign of a voice, restore if it was only a noise
        if (voiced && !S.ducked) { S.player.duck(true); S.ducked = true; S.quietFrames = 0; }
        else if (!voiced && S.ducked && ++S.quietFrames >= 8) { S.player.duck(false); S.ducked = false; }
      }
      if (S.barge.feed(voiced)) bargeIn();
    }
  }

  // text that was sent (or about to be) but the person was not done: it joins what they say next
  function takeBackPending() {
    if (S.pendingSend) {
      clearTimeout(S.pendingSend.timer);
      S.carry = S.pendingSend.text;
      S.pendingSend = null;
      return true;
    }
    const R = S.reply;
    if (R && !R.said && performance.now() - R.sentAt < MERGE_WINDOW_MS && S.sentText) {
      S.carry = S.sentText;
      return true;
    }
    return false;
  }

  function bargeIn() {
    const R = S.reply;
    const mergeBack = takeBackPending();
    // keep only what was heard in the chat history (applied once the aborted run has settled)
    if (!mergeBack && R && R.msg && R.said) {
      S.pendingTrim = { msg: R.msg, text: voiceHeardText(R.lastRaw, R.ends, S.player.startedCount()) };
    }
    S.queue.cancel();
    S.ducked = false;
    if (isBusy()) { const b = $id('btn-abort'); if (b) b.click(); }
    S.reply = null;
    S.barge.reset();
    S.utter = S.ring.slice();
    S.ep.reset();
    S.ep.inSpeech = true; S.ep.voicedFrames = PREROLL_FRAMES; S.ep.frames = PREROLL_FRAMES;
    setState('hearing');
  }

  async function finishUtterance() {
    const frames = S.utter;
    S.utter = [];
    S.barge.reset();
    setState('thinking');
    const turn = S.turn = { endOfSpeech: performance.now() };
    // whisper invents words from a long silent tail: keep only ~190 ms of it
    const keep = frames.length - Math.max(0, S.ep.trailingSilence() - 6);
    const wav = new Blob([audioWavBytes(concatFrames(frames.slice(0, Math.max(1, keep))), 16000)], { type: 'audio/wav' });
    let text = '';
    try {
      text = await S.transport.transcribe(wav, sttLang());
    } catch (e) {
      note('Could not hear that: ' + (e.message || e), true);
      if (V.active) setState('listening');
      return;
    }
    if (!V.active || turn !== S.turn) return;
    if (voiceIsHallucination(text)) { if (V.state === 'thinking') setState('listening'); return; }
    if (S.carry) { text = S.carry + ' ' + text; S.carry = ''; }
    if (V.state === 'hearing') { S.carry = text; return; }      // they kept talking: join it to the next utterance
    turn.transcript = performance.now();
    paintHeard(text);
    if (voiceLooksUnfinished(text)) {                           // "…and", "…but", "um": give them a moment to go on
      S.pendingSend = { text, timer: setTimeout(() => { const p = S.pendingSend; S.pendingSend = null; if (p && V.active) sendToAgent(p.text); }, EXTRA_WAIT_MS) };
      return;
    }
    sendToAgent(text);
  }

  function sendToAgent(text) {
    const input = $id('input');
    if (!input || typeof submitPrompt !== 'function') { note('The chat box is not ready.', true); setState('listening'); return; }
    if (isBusy()) { const b = $id('btn-abort'); if (b) b.click(); }
    S.sentText = text;
    S.reply = { baseLen: (typeof messages !== 'undefined' ? messages.length : 0), msg: null, spoken: 0, first: true, lastRaw: '',
                ends: [], said: false, sawBusy: false, quiet: 0, startedAt: performance.now(), sentAt: performance.now(),
                permNoted: false, errNoted: false, fillerDone: false, lang: voiceFillerLang(text) };
    S.player.onFirstSound = () => { if (S.turn && !S.turn.firstAudio) S.turn.firstAudio = performance.now(); };
    input.value = text;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    submitPrompt();
    setState('thinking');
  }

  function speakNow(text) { S.queue.push(text, null); }

  // a short "Mm-hm." while a slow answer is still being worked out (pre-recorded at start, played at most once per turn)
  function maybeFiller(R) {
    if (R.fillerDone || R.said || !S.fillersOn || performance.now() - R.sentAt < FILLER_AFTER_MS) return;
    const list = S.fillers[R.lang] || [];
    if (!list.length) return;
    R.fillerDone = true;
    S.player.schedule(list[Math.floor(Math.random() * list.length)], null);
    if (V.state === 'thinking') setState('speaking');
  }

  async function loadFillers() {
    S.fillers = { en: [], bn: [] };
    for (const lang of ['en', 'bn']) {
      for (const phrase of VOICE_FILLERS[lang].slice(0, lang === 'en' ? 3 : 2)) {
        if (!V.active) return;
        try {
          const ab = await S.transport.speak(phrase);
          S.fillers[lang].push(await S.ctx.decodeAudioData(ab.slice(0)));
        } catch (_) { /* a missing voice just means no filler in that language */ }
      }
    }
  }

  function applyPendingTrim() {
    const T = S.pendingTrim;
    if (!T || isBusy()) return;
    S.pendingTrim = null;
    try { T.msg.content = T.text || '(interrupted)'; if (typeof renderAll === 'function') renderAll(); } catch (_) {}
  }

  function tickReply() {
    applyPendingTrim();
    const R = S.reply;
    if (!R || !V.active) return;
    const busy = isBusy();
    if (busy) R.sawBusy = true;
    if (!R.msg) {
      const m = (typeof messages !== 'undefined') ? messages[R.baseLen + 1] : null;
      if (m && m.role === 'assistant') R.msg = m;
      else {
        if (performance.now() - R.startedAt > 15000 && !busy) { S.reply = null; setState('listening'); }
        return;
      }
    }
    const m = R.msg;
    const raw = voiceStripThink(m.content || '');
    // the draft was replaced (answer check / rewrite): drop what was queued and start over
    if (R.lastRaw && R.spoken && !raw.startsWith(R.lastRaw.slice(0, R.spoken))) {
      S.queue.cancel(); R.spoken = 0; R.first = true; R.ends = [];
    }
    R.lastRaw = raw;
    const finished = R.sawBusy && !busy;
    R.quiet = finished ? R.quiet + 1 : 0;
    const final = finished && R.quiet >= 3;                    // stable for ~250 ms after the run ended
    if (m.errorAlert && final && !R.errNoted) { R.errNoted = true; speakNow('Sorry, something went wrong.'); }
    const r = voiceTakeSpeakable(raw, R.spoken, { final, first: R.first });
    r.pieces.forEach((p, i) => {
      if (S.turn && !S.turn.firstPiece) S.turn.firstPiece = performance.now();
      R.ends.push(r.ends[i]);
      S.queue.push(p, r.ends[i]);
      R.first = false;
      R.said = true;
    });
    R.spoken = r.spoken;
    if (!R.said) maybeFiller(R);
    if (r.pieces.length && V.state === 'thinking') setState('speaking');
    if (final && R.spoken >= raw.length && !S.queue.isSpeaking()) {
      const t = S.turn;
      if (t) {
        V.lastTurn = {
          transcribeMs: t.transcript ? Math.round(t.transcript - t.endOfSpeech) : null,
          firstSentenceMs: t.firstPiece && t.transcript ? Math.round(t.firstPiece - t.transcript) : null,
          firstAudioMs: t.firstAudio ? Math.round(t.firstAudio - t.endOfSpeech) : null,
          totalMs: Math.round(performance.now() - t.endOfSpeech),
        };
        console.info('[voice] turn', V.lastTurn);
        paintLatency(V.lastTurn);
      }
      S.reply = null;
      S.barge.reset();
      setState('listening');
    } else if (V.state === 'speaking' && !S.queue.isSpeaking() && !final) {
      setState('thinking');                                    // between sentences while the model is still writing
    }
  }

  // the agent is waiting for an approval card: never answered by voice
  V.onPermission = function () {
    const R = S.reply;
    if (!V.active || !R || R.permNoted) return;
    R.permNoted = true;
    speakNow('I need your approval. Please check the screen.');
    if (V.state === 'thinking') setState('speaking');
  };

  /* ---------------- start / stop ---------------- */
  async function ttsStatus() {
    try {
      const r = await fetch('/media/tts/status');
      const st = await r.json();
      if (st.ready) return st;
      const why = !st.installed ? 'install the speech package: pip install sherpa-onnx'
        : 'put a voice model in ' + st.models_dir;
      note('Spoken replies are not set up yet - ' + why, true);
    } catch (_) {
      note('Could not check spoken replies.', true);
    }
    return null;
  }

  V.start = async function () {
    if (V.active) return;
    if (typeof mediaReady === 'function' && !mediaReady('transcribe')) { note('Speech to text is not set up (Settings -> Models & Jobs).', true); return; }
    const st = await ttsStatus();
    if (!st) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    } catch (e) {
      note('Microphone is not available: ' + (e.message || e), true);
      return;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC();
    try {
      await ctx.resume();
      await ctx.audioWorklet.addModule('/static/js/voice-worklet.js?v=1.1');
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      try { ctx.close(); } catch (_) {}
      note('Audio could not start: ' + (e.message || e), true);
      return;
    }
    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, 'voice-frames');
    const mute = ctx.createGain();
    mute.gain.value = 0;
    src.connect(node); node.connect(mute); mute.connect(ctx.destination);   // connected, so the browser keeps pulling frames

    const pol = st.voice || {};
    const endMs = parseInt(localStorage.getItem('voice_end_silence_ms') || '', 10) || pol.end_silence_ms || DEFAULT_END_MS;
    S.transport = new HttpVoiceTransport();
    S.ctx = ctx; S.stream = stream; S.node = node; S.src = src;
    S.player = new VoicePlayer(ctx);
    S.queue = new SpeakQueue(S.transport, S.player, (e) => note('Speech failed: ' + (e.message || e), true));
    S.vad = new VoiceEnergyVad();
    S.ep = new VoiceEndpointer({ endSilenceMs: endMs });
    S.barge = new VoiceBargeDetector();
    S.ring = []; S.utter = []; S.reply = null; S.turn = null; S.carry = ''; S.lastActivity = performance.now();
    S.pendingSend = null; S.pendingTrim = null; S.sentText = ''; S.ducked = false; S.quietFrames = 0;
    S.fillersOn = pol.filler !== false; S.fillers = { en: [], bn: [] };
    node.port.onmessage = (ev) => onFrame(ev.data);
    S.timer = setInterval(tickReply, 80);

    V.active = true;
    showPanel(true);
    setState('listening');
    paintButton();
    // pre-load both models so the first turn does not pay the load time (failures are fine: first use reports them)
    fetch('/media/transcribe/warmup', { method: 'POST' }).catch(() => {});
    fetch('/media/tts/warmup?voice=en', { method: 'POST' }).catch(() => {});
    if (S.fillersOn) loadFillers();
  };

  V.stop = function () {
    if (!V.active) return;
    V.active = false;
    clearInterval(S.timer);
    if (S.pendingSend) { clearTimeout(S.pendingSend.timer); S.pendingSend = null; }
    try { S.queue.cancel(); } catch (_) {}
    if (isBusy()) { const b = $id('btn-abort'); if (b) b.click(); }
    try { S.node.port.onmessage = null; S.node.disconnect(); S.src.disconnect(); } catch (_) {}
    try { S.stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    try { S.ctx.close(); } catch (_) {}
    S.reply = null;
    setState('idle');
    showPanel(false);
    paintButton();
  };

  V.toggle = function () { return V.active ? V.stop() : V.start(); };

  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && V.active) V.stop(); });

  document.addEventListener('DOMContentLoaded', async () => {
    const b = $id('btn-voice');
    if (!b) return;
    b.onclick = () => V.toggle();
    try {
      const r = await fetch('/media/status');
      const st = await r.json();
      b.hidden = !(st.transcribe && st.transcribe.ready);
    } catch (_) { b.hidden = true; }
    paintButton();
  });
})();
