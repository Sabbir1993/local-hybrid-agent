import { GitStrip } from './GitStrip';
import React, { useState, useRef, useEffect, useMemo } from 'react';
import { useAppStore, type ReasoningEffort, type AgentMode } from '../../stores/useAppStore';
import { prepareFile, buildPromptText, type AttachmentData } from '../../services/attachments';
import { loadLibraryNames, CMD_GROUP_LABELS, TEMPLATES, currentToken, expandAtTags, expandLibraryCommand, fileItems, initPrompt, isLibraryCommand, slashItems, type CmdItem } from '../../services/commands';
import { apiFetch } from '../../services/api';
import { VoiceMode } from './VoiceMode';
import { editPicsStore, imageComposeMode, loadMediaStatus, mediaReady, picAttachments, runMedia, setEditPics, setupMessage, takeMediaPics, transcribeBlob } from '../../services/media';
import { MediaOptions } from './MediaOptions';
import { startLiveDictation, type LiveDictation } from '../../services/dictation';

const AGENT_MODES: { key: AgentMode; label: string; desc: string; hotkey: string }[] = [
  { key: 'auto', label: 'Auto', desc: 'Agent handles permission decisions', hotkey: '1' },
  { key: 'manual', label: 'Manual', desc: 'Always ask before making changes', hotkey: '2' },
  { key: 'accept_edits', label: 'Accept edits', desc: 'Automatically accept all file edits', hotkey: '3' },
  { key: 'plan', label: 'Plan', desc: 'Create a plan before making changes', hotkey: '4' },
  { key: 'bypass', label: 'Bypass permissions', desc: 'Accepts all permissions', hotkey: '5' },
];

export interface AttachedFile extends AttachmentData {
  id: string;
  file: File;
  name: string;
  size: number;
  type: string;
  dataUrl?: string;
  previewUrl?: string;
}

export const Composer: React.FC = () => {
  const {
    appMode,
    agentMode,
    setAgentMode,
    reasoningEffort,
    setReasoningEffort,
    deepResearch,
    toggleDeepResearch,
    webSearchEnabled,
    toggleWebSearch,
    kbMode,
    setKbMode,
    selectedModel,
    setSelectedModel,
    availableModels,
    serverStatus,
    isStreaming,
    sendPrompt,
    abortGeneration,
    messages,
    openPreviewModal,
    showToast,
    setActiveCustomAgent,
    compactConversation,
  } = useAppStore();

  const [inputVal, setInputVal] = useState('');
  const backdropRef = useRef<HTMLDivElement>(null);
  // /image and /video armed like the old UI: the command leaves the text box and becomes a pill; Send puts it back in front
  const [armed, setArmed] = useState<'image' | 'video' | null>(null);
  useEffect(() => {
    if (armed) return;
    const m = inputVal.match(/^\/(image|video)\s+/i);
    if (m) { setArmed(m[1].toLowerCase() as 'image' | 'video'); setInputVal(inputVal.slice(m[0].length)); }
  }, [inputVal, armed]);
  // /commands and @files typed in the text: shaded behind the textarea and listed as badges above the box (as the old UI did)
  const inputTokens = useMemo(() => {
    const esc = (x: string) => x.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const CMD = /(^|[\s\[({,;:"'])\/([a-zA-Z0-9_\-]+)(?=$|[\s\])}>.,;:!?])/g;
    const TAG = /(^|[\s\[({,;:"'])@([\w\-./\\]+\.[\w]+)(?=$|[\s\])}>.,;:!?])/g;
    const cmds = [...new Set([...inputVal.matchAll(CMD)].map((m) => m[2]))];
    const tags = [...new Set([...inputVal.matchAll(TAG)].map((m) => m[2]))];
    let html = esc(inputVal).replace(CMD, '$1<mark class="hl-cmd">/$2</mark>').replace(TAG, '$1<mark class="hl-tag">@$2</mark>');
    if (inputVal.endsWith('\n')) html += '<br>&nbsp;';
    return { cmds, tags, html };
  }, [inputVal]);
  const [attachments, setAttachments] = useState<AttachedFile[]>([]);
  const [isRecording, setIsRecording] = useState(false);
  const [showVoiceMenu, setShowVoiceMenu] = useState(false);
  const [voiceMode, setVoiceMode] = useState<'transcribe' | 'voice'>('transcribe');
  const [voiceLang, setVoiceLang] = useState('Auto');
  const [voiceNoise, setVoiceNoise] = useState('Standard');
  const [voiceOn, setVoiceOn] = useState(false);                   // hands-free voice chat (VoiceMode.tsx) is running
  const [voiceState, setVoiceState] = useState<string | null>(null);
  const [showModeMenu, setShowModeMenu] = useState(false);
  const [showEffortMenu, setShowEffortMenu] = useState(false);
  const [showModelDropdown, setShowModelDropdown] = useState(false);
  const [showCtxTooltip, setShowCtxTooltip] = useState(false);
  const [modelSearch, setModelSearch] = useState('');

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // dictation: microphone recorder (the recording is turned into text by the server's speech-to-text model)
  const dictRef = useRef<{ rec: MediaRecorder; stream: MediaStream; chunks: Blob[]; caret: [number, number]; text: string; timer: number } | null>(null);
  const liveRef = useRef<LiveDictation | null>(null);   // the live (interim text) path of the current dictation, if it started
  const mountedRef = useRef(true);
  const startingRef = useRef(false);                     // a dictation start (status, permission prompt, recorder) is pending
  const cancelStartRef = useRef(false);                  // a click during the pending start cancels it
  const dictCtlRef = useRef<{ discard: boolean } | null>(null);   // newest dictation; discard = its text must not be written back
  const inputValRef = useRef('');                        // latest text of the prompt box (updated on every change and live write)
  inputValRef.current = inputVal;
  const armedRef = useRef<'image' | 'video' | null>(null);   // read by async code (attachment prep) that must not see a stale value
  armedRef.current = armed;
  const pdfUrlRef = useRef<string | null>(null);

  // Add picked / pasted / dropped files: images are read locally, documents are uploaded and extracted, text is read.
  const addFiles = (files: File[], prefix = 'upload') => {
    files.forEach((f) => {
      const attId = Math.random().toString(36).slice(2, 9);
      const stamp = new Date().toTimeString().split(' ')[0].replace(/:/g, '') + '_' + attId.slice(0, 3);
      // a pasted screenshot has no real name ("image.png"); everything else keeps its own name
      const unnamedImage = f.type.startsWith('image/') && (!f.name || /^image\.\w+$/i.test(f.name));
      const name = unnamedImage ? `${prefix}_${stamp}.png` : f.name;
      setAttachments((prev) => [...prev, { id: attId, file: f, name, size: f.size, type: f.type, uploading: true }]);
      // a picture is described by the cloud model when one is chosen for helper jobs: say so once
      if (f.type.startsWith('image/') && localStorage.getItem('cloud_model_override') && !sessionStorage.getItem('cloud_vision_warned')) {
        sessionStorage.setItem('cloud_vision_warned', '1');
        showToast('Pictures are described by a cloud model. Do not attach pictures that show card numbers or personal data.', 'warn');
      }
      // onUpdate: a video's poster and "reading picture n/m" arrive while it is still being read
      // in Image mode (or after "/image") a picture is a starting picture for the image model, not something to describe
      const forImage = imageComposeMode() || armedRef.current === 'image' || /^\/image(\s|$)/i.test(inputValRef.current.trim());
      prepareFile(f, appMode === 'code', (patch) => setAttachments((prev) => prev.map((x) => (x.id === attId ? { ...x, ...patch } : x))), { skipVision: forImage }).then((res) => {
        if (res.error) {
          showToast(res.error, 'error');
          if (!res.isDoc) {
            setAttachments((prev) => prev.filter((x) => x.id !== attId));
            return;
          }
        }
        const { dataUrl, name: serverName, ...rest } = res as any;
        setAttachments((prev) =>
          prev.map((x) =>
            x.id === attId
              ? { ...x, ...rest, name: serverName || x.name, dataUrl, previewUrl: dataUrl, uploading: false, type: x.type || (dataUrl ? 'image/png' : '') }
              : x,
          ),
        );
      }).catch((e: any) => {
        showToast(`Could not read "${f.name}": ${e?.message || 'unreadable file'}`, 'error');
        setAttachments((prev) => prev.filter((x) => x.id !== attId));
      });
    });
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files && files.length) addFiles(Array.from(files));
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  // Drop files anywhere on the window
  const [dragging, setDragging] = useState(false);
  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types || []).includes('Files');
    const onEnter = (e: DragEvent) => { if (hasFiles(e)) { depth++; setDragging(true); } };
    const onOver = (e: DragEvent) => { if (hasFiles(e)) e.preventDefault(); };
    const onLeave = (e: DragEvent) => { if (hasFiles(e) && --depth <= 0) { depth = 0; setDragging(false); } };
    const onDrop = (e: DragEvent) => {
      depth = 0;
      setDragging(false);
      if (!hasFiles(e)) return;
      e.preventDefault();
      const files = Array.from(e.dataTransfer?.files || []);
      if (files.length) addFiles(files, 'dropped_file');
    };
    window.addEventListener('dragenter', onEnter);
    window.addEventListener('dragover', onOver);
    window.addEventListener('dragleave', onLeave);
    window.addEventListener('drop', onDrop);
    return () => {
      window.removeEventListener('dragenter', onEnter);
      window.removeEventListener('dragover', onOver);
      window.removeEventListener('dragleave', onLeave);
      window.removeEventListener('drop', onDrop);
    };
  }, [appMode]);

  const removeAttachment = (id: string) => {
    setAttachments((prev) => {
      prev.find((a) => a.id === id)?.abort?.();   // stop describing a picture nobody wants any more
      return prev.filter((a) => a.id !== id);
    });
  };

  const handlePreviewAttachment = (att: AttachedFile) => {
    if (att.previewUrl || att.dataUrl) {
      openPreviewModal(att.previewUrl || att.dataUrl || att.name, att.name, null, 'modal', true);
    } else if (att.file) {
      const ext = att.name.split('.').pop()?.toLowerCase() || '';
      if (ext === 'pdf') {
        if (pdfUrlRef.current) URL.revokeObjectURL(pdfUrlRef.current);   // the previous preview is replaced
        const blobUrl = URL.createObjectURL(att.file);
        pdfUrlRef.current = blobUrl;
        openPreviewModal(blobUrl, att.name, null, 'modal', true);
      } else if (ext === 'xlsx' || ext === 'xls') {
        const reader = new FileReader();
        reader.onload = (ev) => {
          openPreviewModal(String(ev.target?.result || ''), att.name, null, 'modal', true);
        };
        reader.readAsDataURL(att.file);
      } else if (att.type.startsWith('video/') || att.type.startsWith('audio/') || /^(mp4|webm|mov|mkv|avi|m4v|mp3|wav|m4a|ogg|flac|aac|opus)$/.test(ext)) {
        // never read a big binary as text
        if (att.thumb) openPreviewModal(att.thumb, att.name, null, 'modal', true);
        else showToast('No preview for audio or video files.', 'info');
      } else {
        const reader = new FileReader();
        reader.onload = (ev) => {
          openPreviewModal(att.name, att.name, String(ev.target?.result || ''), 'modal', true);
        };
        reader.readAsText(att.file);
      }
    } else {
      openPreviewModal(att.name, att.name, null, 'modal', true);
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const imgs: File[] = [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.startsWith('image/')) {
        const f = items[i].getAsFile();
        if (f) imgs.push(f);
      }
    }
    if (imgs.length) {
      e.preventDefault();
      addFiles(imgs, 'screenshot');
    }
  };

  // voice chat and dictation never run together (one microphone)
  const startVoiceChat = () => {
    if (isRecording) stopDictation();
    setVoiceMode('voice');
    setVoiceOn(true);
  };

  const MIC_MAX_S = 10 * 60;

  // Insert dictated text where the caret was when recording started (else at the end).
  const insertDictation = (text: string, caret: [number, number], base: string) => {
    setInputVal((cur) => {
      const [from, to] = cur === base ? caret : [cur.length, cur.length];
      const before = cur.slice(0, from);
      const after = cur.slice(to);
      const pad = before && !/\s$/.test(before) ? ' ' : '';
      return before + pad + text + (after && !/^\s/.test(after) ? ' ' : '') + after;
    });
  };

  const stopDictation = () => {
    const d = dictRef.current;
    if (!d) { setIsRecording(false); return; }
    window.clearTimeout(d.timer);
    try { if (d.rec.state !== 'inactive') d.rec.stop(); } catch (_) { /* already stopped */ }
  };

  const startDictation = async () => {
    if (startingRef.current || dictRef.current) return;   // one start at a time (a second click cancels it, see toggleRecording)
    startingRef.current = true;
    cancelStartRef.current = false;
    try {
      await startDictationInner();
    } finally {
      startingRef.current = false;
      cancelStartRef.current = false;
    }
  };

  const startDictationInner = async () => {
    const gone = () => !mountedRef.current || cancelStartRef.current;   // unmounted, or clicked again while starting
    // 1. the speech-to-text model must be set up on the server
    const st = await loadMediaStatus();
    if (gone()) return;
    if (!mediaReady('transcribe', st)) {
      showToast(setupMessage('Speech to text'), 'error');
      return;
    }
    // 2. the browser must be able to record at all
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
      showToast('This browser cannot record audio. Use the desktop app, Chrome or Edge, and open the app over https or localhost.', 'error');
      return;
    }
    // 3. the microphone: say plainly what is wrong
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch (e: any) {
      if (gone()) return;
      const name = e?.name || '';
      showToast(
        name === 'NotAllowedError' || name === 'SecurityError'
          ? 'The microphone is blocked. Allow it for this app (the lock icon in the address bar, or Windows Settings > Privacy > Microphone), then try again.'
          : name === 'NotFoundError' || name === 'OverconstrainedError'
          ? 'No microphone was found. Plug one in or pick one in Windows Sound settings.'
          : name === 'NotReadableError'
          ? 'The microphone is in use by another program. Close it and try again.'
          : 'Microphone is not available: ' + (e?.message || name),
        'error',
      );
      return;
    }
    if (gone()) { stream.getTracks().forEach((t) => t.stop()); return; }   // the prompt was answered after the click was cancelled / the composer left
    const ta = textareaRef.current;
    const base = ta ? ta.value : inputVal;
    const caret: [number, number] = ta ? [ta.selectionStart ?? base.length, ta.selectionEnd ?? base.length] : [base.length, base.length];
    const chunks: Blob[] = [];
    let rec: MediaRecorder;
    try {
      rec = new MediaRecorder(stream);
    } catch (e: any) {
      stream.getTracks().forEach((t) => t.stop());   // the microphone must not stay on
      showToast('Could not start recording: ' + (e?.message || 'unsupported audio format'), 'error');
      return;
    }
    const lang = voiceLang === 'English' ? 'en' : voiceLang === 'Bengali' ? 'bn' : voiceLang === 'Spanish' ? 'es' : undefined;
    const ctl = { discard: false };   // set by Send: whatever this recording turns into must not reach the (cleared) box
    dictCtlRef.current = ctl;
    // live text goes straight into the prompt box: before + new words + after. If the person typed meanwhile
    // (the box differs from our last write), rebase so their keystrokes are kept and dictation continues at the end.
    // The server's interim/final text is cumulative, so after a rebase `skip` = how much of it is already in the box.
    const live = { before: base.slice(0, caret[0]), after: base.slice(caret[1]), last: base, skip: 0, text: '' };
    const renderLive = (text: string, commit: boolean) => {
      if (ctl.discard || !mountedRef.current) return;
      const cur = inputValRef.current;
      if (cur !== live.last) { live.before = cur; live.after = ''; live.skip = live.text.length; }
      if (text && text.length < live.skip) text = '';   // the transcript got shorter than what is already shown
      live.text = text;
      const shownText = text.slice(live.skip).trimStart();
      const pad = live.before && !/\s$/.test(live.before) ? ' ' : '';
      const mid = shownText ? pad + shownText : '';
      const next = live.before + mid + (live.after && shownText && !/^\s/.test(live.after) ? ' ' : '') + live.after;
      live.last = next;
      inputValRef.current = next;
      setInputVal(next);
      if (commit) {
        const pos = (live.before + mid).length;
        window.requestAnimationFrame(() => { const t = textareaRef.current; if (t) { t.focus(); try { t.setSelectionRange(pos, pos); } catch (_) { /* not focusable */ } } });
      }
    };
    // The live text is stitched from short windows that can start or end mid-word, so it is the least accurate
    // transcript there is: the whole recording is transcribed once more and replaces it, unless it was edited.
    const refine = async () => {
      if (!chunks.length || live.skip > 0) return;   // skip > 0: typed while dictating, the refined text cannot be merged in
      const shown = live.last;
      try {
        const text = await transcribeBlob(new Blob(chunks, { type: rec.mimeType || 'audio/webm' }), lang);
        const cur = textareaRef.current?.value;
        if (ctl.discard || !text || cur === undefined || cur !== shown) return;   // sent / edited meanwhile: keep theirs
        renderLive(text, true);
      } catch (_) { /* the live text stays */ }
    };
    // the full-recording path: also the fallback when live streaming fails
    const transcribeAll = async () => {
      if (!chunks.length) return;
      try {
        const text = await transcribeBlob(new Blob(chunks, { type: rec.mimeType || 'audio/webm' }), lang);
        if (ctl.discard || !mountedRef.current) return;
        if (!text) showToast('No speech was heard. Try again a little closer to the microphone.', 'warn');
        else insertDictation(text, caret, base);
      } catch (e: any) {
        if (ctl.discard) return;
        showToast(`Could not turn the recording into text: ${e.message}`, 'error');
      }
    };
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      window.clearTimeout(dictRef.current?.rec === rec ? dictRef.current.timer : timer);   // the 10-minute limit no longer applies
      stream.getTracks().forEach((t) => t.stop());   // release the microphone
      if (dictRef.current?.rec === rec) dictRef.current = null;
      if (mountedRef.current) setIsRecording(false);
      const lv = liveRef.current;
      liveRef.current = null;
      if (ctl.discard) { lv?.cancel(); return; }   // Send was pressed: this text has nowhere to go
      if (lv && !lv.dead) {
        try {
          const fin = await lv.finish();
          if (ctl.discard) return;
          if (fin) {
            renderLive(fin, true);
            void refine();
            return;
          }
          // nothing came back (every window was quiet or too short for the live path): the whole recording decides
          renderLive('', false);
        } catch (_) {
          renderLive('', false);   // live path broke mid-flight: drop the partial text, use the full recording
        }
      } else if (lv) {
        lv.cancel();
      }
      await transcribeAll();
    };
    const timer: number = window.setTimeout(() => { showToast('Stopped after 10 minutes.', 'info'); stopDictation(); }, MIC_MAX_S * 1000);
    dictRef.current = { rec, stream, chunks, caret, text: base, timer };
    try {
      rec.start(1000);
    } catch (e: any) {
      window.clearTimeout(timer);
      dictRef.current = null;
      stream.getTracks().forEach((t) => t.stop());   // the microphone must not stay on
      showToast('Could not start recording: ' + (e?.message || 'error'), 'error');
      return;
    }
    setIsRecording(true);
    showToast('Listening. Click the microphone again to stop.', 'info');
    // live path: any failure just means no interim text, stopping then transcribes the full recording as before
    let sttLang = 'auto';
    try { sttLang = lang || localStorage.getItem('stt_lang') || 'auto'; } catch (_) { /* storage blocked */ }
    startLiveDictation(stream, sttLang, (t) => renderLive(t, false)).then((lv) => {
      if (dictRef.current?.rec !== rec) { lv.cancel(); return; }   // stopped (or gone) while it was starting
      liveRef.current = lv;
    }).catch(() => { /* no live text */ });
  };

  // the microphone must never stay open after the composer goes away
  useEffect(() => {
    mountedRef.current = true;
    return () => {
    mountedRef.current = false;
    if (pdfUrlRef.current) { URL.revokeObjectURL(pdfUrlRef.current); pdfUrlRef.current = null; }
    if (dictCtlRef.current) dictCtlRef.current.discard = true;
    liveRef.current?.cancel();
    liveRef.current = null;
    const d = dictRef.current;
    if (d) {
      window.clearTimeout(d.timer);
      d.rec.onstop = null;
      dictRef.current = null;   // a live path still starting up cancels itself
      try { d.rec.stop(); } catch (_) { /* already stopped */ }
      d.stream.getTracks().forEach((t) => t.stop());
    }
    };
  }, []);

  // a picture kept for editing belongs to the chat it was picked in: New chat / another chat drops it
  const sessionId = useAppStore((s) => s.activeSessionId);
  const prevSessionRef = useRef(sessionId);
  useEffect(() => {
    const prev = prevSessionRef.current;
    prevSessionRef.current = sessionId;
    // null -> id is a first /image creating its chat, not a chat change
    if (prev !== sessionId && prev != null) setEditPics([]);
  }, [sessionId]);

  const toggleRecording = () => {
    if (voiceOn) { setVoiceOn(false); return; }
    if (startingRef.current) { cancelStartRef.current = true; return; }   // a click while starting cancels the start
    if (isRecording) { stopDictation(); return; }
    void startDictation();
  };

  // --- slash / @file menu ---
  const [menu, setMenu] = useState<{ kind: 'slash' | 'files'; items: CmdItem[]; sel: number; start: number } | null>(null);
  const customAgentsRef = useRef<any[]>([]);
  const menuReq = useRef(0);
  const menuRef = useRef<HTMLDivElement>(null);

  // keep the highlighted row in view when arrowing through a long list (the first row also reveals its group label)
  useEffect(() => {
    const box = menuRef.current;
    if (!box || !menu) return;
    if (menu.sel === 0) { box.scrollTop = 0; return; }
    box.querySelector<HTMLElement>('.cmd-item.sel')?.scrollIntoView({ block: 'nearest' });
  }, [menu?.sel, menu?.items]);

  const refreshMenu = async (value: string, caret: number) => {
    const tok = currentToken(value, caret);
    const req = ++menuReq.current;
    if (!tok || (tok.ch === '@' && appMode !== 'code')) {
      setMenu(null);
      return;
    }
    if (tok.ch === '/' && customAgentsRef.current.length === 0) {
      try {
        const r = await apiFetch('/custom-agents');
        const d = await r.json();
        customAgentsRef.current = Array.isArray(d) ? d : d.agents || [];
      } catch (_) {
        // the menu still works without custom agents
      }
    }
    const items = tok.ch === '@' ? await fileItems(tok.text) : await slashItems(tok.text, appMode, customAgentsRef.current);
    if (req !== menuReq.current) return;   // a newer keystroke superseded this lookup
    setMenu(items.length ? { kind: tok.ch === '@' ? 'files' : 'slash', items, sel: 0, start: tok.start } : null);
  };

  const pickMenuItem = (it: CmdItem) => {
    const ta = textareaRef.current;
    if (!menu || !ta) return;
    menuReq.current++;
    const tok = currentToken(inputVal, ta.selectionStart ?? inputVal.length);
    const tokLen = tok ? tok.text.length + 1 : 0;
    const before = inputVal.slice(0, menu.start);
    const after = inputVal.slice(menu.start + tokLen).replace(/^\s?/, '');
    setMenu(null);
    const put = (text: string) => {
      setInputVal(before + text + after);
      requestAnimationFrame(() => {
        ta.focus();
        const pos = before.length + text.length;
        ta.setSelectionRange(pos, pos);
      });
    };
    if (menu.kind === 'files') return put('@' + it.value + ' ');
    if (it.template) return put(TEMPLATES[it.name] || '');
    if (it.agent) {
      put('');
      setActiveCustomAgent(it.agent);
      showToast(`Active agent: ${it.agent.icon || ''} ${it.agent.name}`, 'info');
      return;
    }
    if (it.name === 'plan' || it.name === 'build') {
      put('');
      setAgentMode(it.name === 'plan' ? 'plan' : agentMode === 'plan' ? 'auto' : agentMode);
      return;
    }
    put('/' + it.name + ' ');
  };

  // Run a leading /command typed in the box. Returns true when it was handled here (nothing is sent as chat text).
  const runSlash = async (text: string): Promise<boolean> => {
    const m = text.match(/^\/([a-zA-Z0-9_-]+)(?:\s+([\s\S]*))?$/);
    if (!m) return false;
    const name = m[1].toLowerCase();
    const arg = (m[2] || '').trim();
    if (name === 'image' || name === 'video') {
      const err = await runMedia(name, arg);
      if (err) showToast(err, 'warn');
      else setInputVal('');
      return true;
    }
    if (name === 'compact') {
      setInputVal('');
      await compactConversation(arg);
      return true;
    }
    if (appMode === 'chat') return false;   // chat only knows /image, /video and /compact: anything else is the user's words
    if (name === 'plan' || name === 'build') {
      setAgentMode(name === 'plan' ? 'plan' : agentMode === 'plan' ? 'auto' : agentMode);
      setInputVal('');
      if (arg) await sendPrompt(arg, { displayContent: arg });
      return true;
    }
    if (name === 'init') {
      if (appMode !== 'code') {
        showToast('/init works in Code mode', 'warn');
        return true;
      }
      try {
        const prompt = await initPrompt(arg);
        if (agentMode === 'plan') setAgentMode('auto');   // /init writes a file: leave Plan mode, keep any other mode
        setInputVal('');
        await sendPrompt(prompt, { displayContent: arg ? `/init ${arg}` : '/init' });
      } catch (e: any) {
        showToast(e.message, 'error');
      }
      return true;
    }
    if (appMode === 'code' && isLibraryCommand(name)) {
      try {
        const prompt = await expandLibraryCommand(name, arg);
        setInputVal('');
        await sendPrompt(prompt, { displayContent: text });
      } catch (e: any) {
        showToast(`/${name}: ${e.message}`, 'error');
      }
      return true;
    }
    return false;   // /goal, skills and anything else go through as the user's words
  };

  // An image request that needs more than runSlash: Image mode (plain text becomes /image) or pictures to start from.
  // Returns true when it was handled here; false leaves the send to the normal path.
  const runImageSend = async (text: string): Promise<boolean> => {
    const typed = text.match(/^\/image(?:\s+([\s\S]*))?$/i);
    const asMode = !typed && !text.startsWith('/') && imageComposeMode();
    if (!typed && !asMode) return false;
    const hasPics = picAttachments(attachments).length > 0 || editPicsStore.snapshot().length > 0;
    if (!hasPics && (asMode ? attachments.length > 0 : true)) return false;   // chat attachments / no pictures: normal path
    const prompt = asMode ? text : (typed?.[1] || '').trim();
    const pics = await takeMediaPics(attachments);
    if (typeof pics === 'string') { showToast(pics, 'warn'); return true; }
    const err = await runMedia('image', prompt, { pics: pics || undefined });
    if (err) { showToast(err, 'warn'); return true; }
    setInputVal('');
    setArmed(null);
    if (pics) {
      // consumed as pictures: stop any description that was started for them
      setAttachments((prev) => {
        prev.filter((a) => pics.used.includes(a.id)).forEach((a) => a.abort?.());
        return prev.filter((a) => !pics.used.includes(a.id));
      });
    }
    return true;
  };

  const sendingRef = useRef(false);

  const handleSend = async () => {
    const typedText = inputVal.trim();
    if (armed && !typedText && attachments.length === 0) return;   // nothing to make yet
    const text = armed ? `/${armed} ${typedText}`.trim() : typedText;
    if (!text && attachments.length === 0) return;
    if (isStreaming || sendingRef.current) return;
    if (attachments.some((x) => x.uploading)) {
      showToast('Wait for the attachments to finish uploading.', 'warn');
      return;
    }
    // checked before anything is cleared: nothing you typed is lost when a send cannot go ahead
    if (appMode === 'code' && !useAppStore.getState().activeProjectId) {
      showToast('Please select a project first', 'error');
      return;
    }

    sendingRef.current = true;
    menuReq.current++;
    setMenu(null);
    // a recording still running (or being turned into text) must not write into the box once it is cleared
    if (dictCtlRef.current) dictCtlRef.current.discard = true;
    if (dictRef.current) stopDictation();
    try {
      if (await runImageSend(text)) { setArmed(null); return; }
      if (attachments.length > 0 && /^\/(image|video)(\s|$)/i.test(text)) {
        showToast('Remove the attachments to use /image or /video.', 'warn');
        return;
      }
      if (attachments.length === 0 && (await runSlash(text))) { setArmed(null); return; }

      let imageUrls: string[] = [];
      let fileNames = '';
      let prompt = text;
      // @file mentions in what you typed become the file's text (Code mode); attached text is never scanned
      const typed = appMode === 'code' ? await expandAtTags(text) : text;

      if (attachments.length > 0) {
        const sent = attachments;
        try {
          // what the model reads: the question plus each file's text and each image's description
          prompt = await buildPromptText(typed, sent);
        } catch (e: any) {
          showToast('Could not prepare the attachments: ' + (e?.message || 'error'), 'error');
          return;   // the text and the chips stay where they are
        }
        imageUrls = sent
          .filter((a) => a.isImage || a.type.startsWith('image/') || a.dataUrl || a.previewUrl)
          .map((a) => a.dataUrl || a.previewUrl)
          .filter(Boolean) as string[];
        fileNames = sent.map((a) => a.name).join(', ');
        setAttachments([]);
      } else {
        prompt = typed;
      }

      setInputVal('');
      setArmed(null);
      sendPrompt(prompt, { images: imageUrls, files: fileNames, displayContent: text });
    } finally {
      sendingRef.current = false;
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (menu && menu.items.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setMenu({ ...menu, sel: (menu.sel + 1) % menu.items.length }); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setMenu({ ...menu, sel: (menu.sel - 1 + menu.items.length) % menu.items.length }); return; }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pickMenuItem(menu.items[menu.sel]); return; }
      if (e.key === 'Escape') { e.preventDefault(); menuReq.current++; setMenu(null); return; }
    }
    if (e.key === 'Backspace' && armed && !inputVal) { e.preventDefault(); setArmed(null); return; }
    if (e.key === 'Enter' && !e.shiftKey) {
      if (e.nativeEvent.isComposing || e.keyCode === 229) return;   // confirming an IME composition
      e.preventDefault();
      handleSend();
    }
  };

  // Close dropdowns on outside click
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest('#claude-mic-split-wrap')) setShowVoiceMenu(false);
      if (!target.closest('#claude-mode-wrap')) setShowModeMenu(false);
      if (!target.closest('#effort-wrap')) setShowEffortMenu(false);
      if (!target.closest('#model-picker-wrap')) setShowModelDropdown(false);
    };
    window.addEventListener('click', handleClickOutside);
    const onEditPrompt = (e: any) => {
      if (!e.detail) return;
      if (e.detail.focusOnly) { textareaRef.current?.focus(); return; }
      if (typeof e.detail.text === 'string') {
        if (e.detail.ifEmpty && inputValRef.current.trim()) { textareaRef.current?.focus(); return; }   // keep the draft being written
        setInputVal(e.detail.text);
        if (textareaRef.current) {
          textareaRef.current.focus();
          textareaRef.current.selectionStart = textareaRef.current.selectionEnd = e.detail.text.length;
        }
      }
    };
    window.addEventListener('edit-user-prompt', onEditPrompt);
    return () => {
      window.removeEventListener('click', handleClickOutside);
      window.removeEventListener('edit-user-prompt', onEditPrompt);
    };
  }, []);

  useEffect(() => { if (showModeMenu) { setShowEffortMenu(false); setShowModelDropdown(false); setShowVoiceMenu(false); } }, [showModeMenu]);
  useEffect(() => { if (showEffortMenu) { setShowModeMenu(false); setShowModelDropdown(false); setShowVoiceMenu(false); } }, [showEffortMenu]);
  useEffect(() => { if (showModelDropdown) { setShowModeMenu(false); setShowEffortMenu(false); setShowVoiceMenu(false); } }, [showModelDropdown]);
  useEffect(() => { if (showVoiceMenu) { setShowModeMenu(false); setShowEffortMenu(false); setShowModelDropdown(false); } }, [showVoiceMenu]);

  useEffect(() => { if (appMode === 'code') void loadLibraryNames(); }, [appMode]);

  // 1-5 pick a mode while the mode menu is open
  useEffect(() => {
    if (!showModeMenu) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;   // typing, not a hotkey
      if (e.key === 'Escape') { setShowModeMenu(false); return; }
      const m = AGENT_MODES.find((x) => x.hotkey === e.key);
      if (m && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        setAgentMode(m.key);
        setShowModeMenu(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [showModeMenu]);

  const currentModeObj = AGENT_MODES.find((m) => m.key === agentMode) || AGENT_MODES[0];
  const currentModelDisplay = useMemo(() => {
    if (serverStatus?.cloud_main?.display) return serverStatus.cloud_main.display;
    if (selectedModel) {
      const match = availableModels.find((m) => m.id === selectedModel || m.display === selectedModel);
      return match?.display || selectedModel;
    }
    return availableModels[0]?.display || 'No model available';
  }, [serverStatus, selectedModel, availableModels]);

  const placeholderModel = currentModelDisplay === 'No model available' ? 'model' : currentModelDisplay;

  const effortDisplay = deepResearch
    ? `${reasoningEffort.charAt(0).toUpperCase() + reasoningEffort.slice(1)} · Deep`
    : reasoningEffort.charAt(0).toUpperCase() + reasoningEffort.slice(1);

  const contextStats = useMemo(() => {
    const nCtx = (serverStatus?.context?.n_ctx) || 32768;
    let totalToks = 0;
    // the latest reply with a server-reported prompt size already covers the system prompt, tools and all history:
    // add only its own completion and what came after it
    let anchor = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant' && (messages[i].promptTokens || 0) > 0) { anchor = i; break; }
    }
    if (anchor >= 0) totalToks = (messages[anchor].promptTokens || 0) + (messages[anchor].ntok || 0);
    messages.slice(anchor + 1).forEach((m) => {
      if (m.ntok && m.ntok > 0) {
        totalToks += m.ntok;
      } else {
        const textLen = (m.content || '').length + (m.thinking || '').length;
        totalToks += Math.round(textLen / 3.5);
      }
      if (m.acts && m.acts.length > 0) {
        totalToks += m.acts.length * 40;
      }
    });
    const pct = Math.min(100, Math.round((totalToks / Math.max(1, nCtx)) * 100));
    const strokeDash = 88;
    const offset = Math.max(0, strokeDash - (pct / 100) * strokeDash);
    const color = pct > 85 ? '#ef4444' : pct > 65 ? '#f59e0b' : '#38bdf8';
    const fmtK = (n: number) =>
      n >= 1000000
        ? `${(n / 1000000).toFixed(1).replace('.0', '')}M`
        : n >= 1000
        ? `${(n / 1000).toFixed(1).replace('.0', '')}k`
        : `${n}`;
    return { nCtx, totalToks, pct, offset, color, fmtK };
  }, [messages, serverStatus]);

  const groupedModels = useMemo(() => {
    const q = modelSearch.toLowerCase();
    const matches = (availableModels || []).filter((m) => {
      const matchName = (m.display || '').toLowerCase().includes(q) || (m.id || '').toLowerCase().includes(q);
      const matchProvider = (m.provider_name || m.provider || '').toLowerCase().includes(q);
      return matchName || matchProvider;
    });

    const groups: { [key: string]: { name: string; isLocal: boolean; items: typeof availableModels } } = {};

    matches.forEach((m) => {
      const isLocal = m.kind === 'local' || (!String(m.id).startsWith('cloud:') && m.kind !== 'cloud');
      const providerKey = isLocal ? 'local' : (m.provider_name || m.provider || 'Cloud');
      const groupDisplayName = isLocal ? 'Local' : (m.provider_name || m.provider || 'Cloud');

      if (!groups[providerKey]) {
        groups[providerKey] = {
          name: groupDisplayName,
          isLocal,
          items: [],
        };
      }
      groups[providerKey].items.push(m);
    });

    return Object.values(groups).sort((a, b) => {
      if (a.isLocal) return -1;
      if (b.isLocal) return 1;
      return a.name.localeCompare(b.name);
    });
  }, [availableModels, modelSearch]);

  return (
    <footer>
      <div id="f-inner">
        <GitStrip />
        <div className="claude-chat-composer composer" id="claude-chat-composer" style={{ position: 'relative' }}>
          {dragging && (
            <div
              aria-hidden="true"
              style={{ position: 'absolute', inset: 0, zIndex: 20, borderRadius: 16, border: '2px dashed var(--accent, #38bdf8)', background: 'rgba(56,189,248,0.1)', display: 'grid', placeItems: 'center', fontSize: 13, fontWeight: 600, color: 'var(--accent, #38bdf8)', pointerEvents: 'none' }}
            >
              Drop files to attach
            </div>
          )}
          {/* Attachments Preview Bar */}
          {attachments.length > 0 && (
            <div
              id="attach-preview-bar"
              className="attach-preview-bar"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                flexWrap: 'wrap',
                padding: '6px 12px',
                marginBottom: '4px',
              }}
            >
              {attachments.map((att) => (
                <div
                  key={att.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    padding: '4px 8px',
                    borderRadius: '8px',
                    background: 'var(--panel2, rgba(255,255,255,0.06))',
                    border: '1px solid var(--border)',
                    fontSize: '11px',
                    color: 'var(--text)',
                    cursor: 'pointer',
                    userSelect: 'none',
                  }}
                  title={`Preview ${att.name} in modal`}
                  onClick={() => handlePreviewAttachment(att)}
                >
                  {att.previewUrl || att.thumb ? (
                    <img src={att.previewUrl || att.thumb} alt="" style={{ width: '18px', height: '18px', borderRadius: '4px', objectFit: 'cover' }} />
                  ) : (
                    <span>📄</span>
                  )}
                  <span style={{ maxWidth: '140px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', textDecoration: 'underline', textUnderlineOffset: '2px' }}>
                    {att.uploading ? (att.progress ? `${att.progress} ` : 'Reading… ') : ''}{att.name}
                  </span>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      removeAttachment(att.id);
                    }}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--dim)', padding: '0 2px', fontSize: '11px' }}
                    title="Remove attachment"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Top Rounded Input Box (matching screenshot) */}
          {(armed || inputTokens.cmds.length > 0 || inputTokens.tags.length > 0) && (
            <div id="cmd-chip-bar" className="cmd-chip-bar" style={{ display: 'flex', alignItems: 'center' }}>
              {armed && (
                <div className="cmd-chip" title={armed === 'image' ? '/image: make a picture from a description' : '/video: make a short video clip from a description'}>
                  <span className="cmd-chip-icon">{armed === 'image' ? '🎨' : '🎬'}</span>
                  <span className="cmd-chip-name">&lt;{armed}&gt;</span>
                  <span className="cmd-chip-hint">{armed === 'image' ? 'Enter to make the image' : 'Enter to make the video'}</span>
                  <span className="cmd-chip-x" title="Remove command (or press Backspace on an empty box)" onClick={() => { setArmed(null); textareaRef.current?.focus(); }}>✕</span>
                </div>
              )}
              {/* /image and /video options (shape, size, length, start-from pictures) sit beside the pill */}
              {armed && <MediaOptions text={`/${armed} ${inputVal}`} attachments={attachments} />}
              {inputTokens.cmds.map((c) => (
                <div key={'c' + c} className="cmd-active-badge cmd" title="Internal command directive"><span className="badge-icon">⚡</span>/{c}<span className="badge-label">Directive</span></div>
              ))}
              {inputTokens.tags.map((t) => (
                <div key={'t' + t} className="cmd-active-badge tag" title="Project target file"><span className="badge-icon">📄</span>@{t}<span className="badge-label">File</span></div>
              ))}
            </div>
          )}
          {!armed && <MediaOptions text={inputVal} attachments={attachments} />}
          <div
            className="claude-input-pill-box"
            id="claude-input-pill-box"
            style={{
              display: 'flex',
              alignItems: 'flex-start',
              background: 'var(--panel, #121316)',
              border: '1.5px solid rgba(56, 189, 248, 0.45)',
              borderRadius: '24px',
              padding: '12px 18px',
              minHeight: '85px',
              height: '85px',
              boxSizing: 'border-box',
              position: 'relative',
              transition: 'border-color 0.15s, box-shadow 0.15s',
            }}
          >
            <div id="input-backdrop" ref={backdropRef} className="input-backdrop" aria-hidden="true" dangerouslySetInnerHTML={{ __html: inputTokens.html }} />
            {menu && (
              <div id="cmd-menu" ref={menuRef} role="listbox" style={{ display: 'block' }}>
                {menu.items.map((it, i) => (
                  <React.Fragment key={it.name + i}>
                    {it.category && it.category !== menu.items[i - 1]?.category && <div className="cmd-group-label">{CMD_GROUP_LABELS[it.category] || it.category}</div>}
                    <div className={`cmd-item ${i === menu.sel ? 'sel' : ''}`} role="option" aria-selected={i === menu.sel} onMouseDown={(e) => { e.preventDefault(); pickMenuItem(it); }}>
                      <span className="cmd-icon">{it.icon}</span>
                      <span className="cmd-name">{it.name}</span>
                      {it.desc && <span className="cmd-desc">{it.desc}</span>}
                    </div>
                  </React.Fragment>
                ))}
              </div>
            )}
            <textarea
              ref={textareaRef}
              id="input"
              rows={1}
              value={inputVal}
              onChange={(e) => {
                setInputVal(e.target.value);
                refreshMenu(e.target.value, e.target.selectionStart ?? e.target.value.length);
              }}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              onScroll={(e) => { if (backdropRef.current) backdropRef.current.scrollTop = e.currentTarget.scrollTop; }}
              placeholder={armed ? (armed === 'image' ? 'Describe the picture (subject, style, light…) — Enter to make it' : 'Describe the scene and what moves — Enter to make the video (takes a few minutes)') : appMode === 'code' ? 'Describe a coding task (e.g. "Find and fix bug in main.py", "Refactor the database queries")...' : `Message ${placeholderModel}... (Enter to send, Shift+Enter for newline)`}
              title="Type your prompt here. Press Enter to send, Shift+Enter for newline."
              style={{
                flex: 1,
                background: 'transparent',
                border: 'none',
                outline: 'none',
                fontSize: '13.5px',
                fontFamily: 'inherit',
                lineHeight: 1.5,
                color: 'var(--text, #f3f4f6)',
                padding: 0,
                margin: 0,
                height: '60px',
                resize: 'none',
              }}
            />
            <button
              type="button"
              className="claude-enter-btn"
              id="btn-input-enter"
              title={isStreaming ? 'Cancel generation' : 'Send (Enter)'}
              onClick={isStreaming ? abortGeneration : handleSend}
              style={{
                background: 'transparent',
                border: 'none',
                color: isStreaming ? '#ef4444' : inputVal.trim() ? '#38bdf8' : 'var(--dim, #64748b)',
                fontSize: '16px',
                cursor: 'pointer',
                padding: '4px 6px',
                alignSelf: 'center',
                marginLeft: '8px',
                transition: 'color 0.15s',
              }}
            >
              {isStreaming ? '⏹' : '⏎'}
            </button>
          </div>

          {/* Bottom Toolbar Row (matching screenshot) */}
          <div
            className="claude-input-bar-row select-none"
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '4px 6px 0 6px',
              fontSize: '12.5px',
            }}
          >
            {/* Left Controls: +   🎤 ▾   Auto ▾   🌐   Auto */}
            <div className="claude-bar-left" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              {/* Attach Button */}
              <button
                id="btn-attach"
                className="claude-tool-icon-btn comp-tool"
                type="button"
                title="Attach files"
                aria-label="Attach files"
                onClick={() => fileInputRef.current?.click()}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--dim)',
                  cursor: 'pointer',
                  padding: '4px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                  <line x1="12" y1="5" x2="12" y2="19" />
                  <line x1="5" y1="12" x2="19" y2="12" />
                </svg>
              </button>
              <input
                ref={fileInputRef}
                type="file"
                id="file-input"
                multiple
                onChange={handleFileChange}
                style={{ display: 'none' }}
              />
              {/* Split Mic Pill */}
              <div className="claude-mic-split-wrap" id="claude-mic-split-wrap" style={{ position: 'relative' }}>
                <div
                  className={`claude-mic-split-pill ${voiceState ? 'voice-live' : isRecording ? 'recording' : ''}`}
                  id="claude-mic-split-pill"
                  data-voice-state={voiceState || undefined}
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    background: isRecording ? 'rgba(239, 68, 68, 0.15)' : 'var(--panel2, rgba(255,255,255,0.06))',
                    border: isRecording ? '1px solid #ef4444' : '1px solid var(--border)',
                    borderRadius: '20px',
                    padding: '2px 6px',
                  }}
                >
                  <button
                    id="btn-mic"
                    className="claude-mic-main-btn comp-tool comp-mic-btn"
                    type="button"
                    title={
                      voiceOn
                        ? 'End the voice conversation'
                        : isRecording
                        ? 'Stop recording'
                        : voiceMode === 'voice'
                        ? 'Voice chat'
                        : 'Dictate (speech to text)'
                    }
                    aria-label={voiceMode === 'voice' ? 'Voice chat' : 'Dictate'}
                    onClick={() => (voiceMode === 'voice' && !voiceOn && !isRecording ? startVoiceChat() : toggleRecording())}
                    style={{
                      background: 'none',
                      border: 'none',
                      color: isRecording ? '#ef4444' : 'var(--dim)',
                      cursor: 'pointer',
                      padding: '2px 4px',
                      display: 'flex',
                      alignItems: 'center',
                    }}
                  >
                    {voiceMode === 'voice' ? (
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <line x1="4" y1="10" x2="4" y2="14" />
                        <line x1="8" y1="6" x2="8" y2="18" />
                        <line x1="12" y1="3" x2="12" y2="21" />
                        <line x1="16" y1="7" x2="16" y2="17" />
                        <line x1="20" y1="10" x2="20" y2="14" />
                      </svg>
                    ) : (
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
                        <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
                        <line x1="12" y1="19" x2="12" y2="22" />
                      </svg>
                    )}
                  </button>
                  <div className="claude-mic-divider" style={{ width: '1px', height: '12px', background: 'var(--border)', margin: '0 2px' }} />
                  <button
                    id="btn-mic-menu-toggle"
                    className="claude-mic-arrow-btn"
                    type="button"
                    title="Voice options"
                    onClick={(e) => {
                      e.stopPropagation();
                      setShowVoiceMenu(!showVoiceMenu);
                    }}
                    style={{
                      background: 'none',
                      border: 'none',
                      color: 'var(--dim)',
                      cursor: 'pointer',
                      padding: '2px 3px',
                      display: 'flex',
                      alignItems: 'center',
                    }}
                  >
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                      <path d="m6 9 6 6 6-6" />
                    </svg>
                  </button>
                </div>

                {voiceOn && (
                  <VoiceMode
                    lang={voiceLang}
                    noise={voiceNoise}
                    onState={setVoiceState}
                    onEnd={() => setVoiceOn(false)}
                  />
                )}

                {/* Voice Dropdown Menu (Matching Image 3) */}
                {showVoiceMenu && (
                  <div
                    className="claude-voice-menu"
                    id="claude-voice-menu"
                    role="menu"
                    style={{
                      display: 'block',
                      position: 'absolute',
                      bottom: '100%',
                      left: 0,
                      marginBottom: '8px',
                      background: 'var(--panel, #18181b)',
                      border: '1px solid var(--border-subtle, rgba(255, 255, 255, 0.12))',
                      borderRadius: '12px',
                      padding: '6px',
                      minWidth: '220px',
                      boxShadow: '0 12px 36px rgba(0,0,0,0.5)',
                      zIndex: 9999,
                      animation: 'claudePopIn 0.12s cubic-bezier(0.16, 1, 0.3, 1)',
                      transformOrigin: 'bottom left',
                    }}
                  >
                    <button
                      type="button"
                      className="claude-menu-item"
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '10px',
                        width: '100%',
                        padding: '7px 10px',
                        background: 'transparent',
                        border: 'none',
                        color: 'var(--text)',
                        cursor: 'pointer',
                        borderRadius: '6px',
                        textAlign: 'left',
                      }}
                      onClick={() => {
                        setVoiceMode('transcribe');
                        if (voiceOn) setVoiceOn(false);   // ends voice chat; the next mic click dictates
                        else toggleRecording();
                        setShowVoiceMenu(false);
                      }}
                    >
                      <span style={{ fontSize: '13px' }}>🎤</span>
                      <span style={{ flex: 1, textAlign: 'left', fontSize: '12.5px' }}>Dictate</span>
                      {voiceMode === 'transcribe' && <span style={{ color: '#ffffff', fontWeight: 600 }}>✓</span>}
                    </button>
                    <button
                      type="button"
                      className="claude-menu-item"
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '10px',
                        width: '100%',
                        padding: '7px 10px',
                        background: 'transparent',
                        border: 'none',
                        color: 'var(--text)',
                        cursor: 'pointer',
                        borderRadius: '6px',
                        textAlign: 'left',
                      }}
                      onClick={() => {
                        if (!voiceOn) startVoiceChat();
                        setShowVoiceMenu(false);
                      }}
                    >
                      <span style={{ fontSize: '13px' }}>🗣️</span>
                      <span style={{ flex: 1, textAlign: 'left', fontSize: '12.5px' }}>Voice chat</span>
                      {voiceMode === 'voice' && <span style={{ color: '#ffffff', fontWeight: 600 }}>✓</span>}
                    </button>
                    <div style={{ height: '1px', background: 'var(--border-subtle, rgba(255, 255, 255, 0.08))', margin: '5px 4px' }} />
                    <button
                      type="button"
                      className="claude-menu-item"
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        width: '100%',
                        padding: '7px 10px',
                        background: 'transparent',
                        border: 'none',
                        color: 'var(--text)',
                        cursor: 'pointer',
                        fontSize: '12.5px',
                        borderRadius: '6px',
                      }}
                      onClick={(e) => {
                        e.stopPropagation();
                        const langs = ['Auto', 'English', 'Bengali', 'Spanish'];
                        const next = langs[(langs.indexOf(voiceLang) + 1) % langs.length];
                        setVoiceLang(next);
                      }}
                    >
                      <span style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <span style={{ fontSize: '13px', color: '#38bdf8' }}>🌐</span>
                        <span>I speak</span>
                      </span>
                      <span style={{ color: '#38bdf8', fontWeight: 600, fontSize: '12px' }}>{voiceLang}</span>
                    </button>
                    <button
                      type="button"
                      className="claude-menu-item"
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        width: '100%',
                        padding: '7px 10px',
                        background: 'transparent',
                        border: 'none',
                        color: 'var(--text)',
                        cursor: 'pointer',
                        fontSize: '12.5px',
                        borderRadius: '6px',
                      }}
                      onClick={(e) => {
                        e.stopPropagation();
                        const modes = ['Standard', 'Crowd', 'Off'];
                        const next = modes[(modes.indexOf(voiceNoise) + 1) % modes.length];
                        setVoiceNoise(next);
                      }}
                    >
                      <span style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                        <span style={{ fontSize: '13px', color: '#94a3b8' }}>🎧</span>
                        <span>Noise reduction</span>
                      </span>
                      <span style={{ color: '#38bdf8', fontWeight: 600, fontSize: '12px' }}>{voiceNoise}</span>
                    </button>
                  </div>
                )}
              </div>

              {/* Mode Selector & Dropdown Menu */}
              <div className="claude-mode-wrap" id="claude-mode-wrap" style={{ position: 'relative', display: appMode === 'chat' ? 'none' : undefined }}>
                <button
                  id="btn-claude-mode"
                  className="claude-tool-text-btn"
                  type="button"
                  title="Mode / Permissions"
                  aria-haspopup="menu"
                  aria-expanded={showModeMenu}
                  onClick={(e) => {
                    e.stopPropagation();
                    setShowModeMenu(!showModeMenu);
                  }}
                  style={{
                    background: 'transparent',
                    border: 'none',
                    color: 'var(--dim)',
                    cursor: 'pointer',
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: '4px',
                    fontSize: '12px',
                    padding: '2px 4px',
                  }}
                >
                  <span id="claude-mode-text">{currentModeObj.label}</span>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <path d="m6 9 6 6 6-6" />
                  </svg>
                </button>

                {showModeMenu && (
                  <div
                    className="claude-mode-menu"
                    id="claude-mode-menu"
                    role="menu"
                    style={{
                      display: 'block',
                      position: 'absolute',
                      bottom: '100%',
                      left: 0,
                      marginBottom: '8px',
                      background: 'var(--panel2, #1c1c21)',
                      border: '1px solid var(--border)',
                      borderRadius: '10px',
                      padding: '6px',
                      minWidth: '220px',
                      boxShadow: '0 8px 30px rgba(0,0,0,0.5)',
                      zIndex: 9999,
                    }}
                  >
                    <div style={{ fontSize: '10.5px', color: 'var(--dim)', fontWeight: 700, padding: '4px 8px', textTransform: 'uppercase' }}>Mode</div>
                    {AGENT_MODES.map((m) => (
                      <button
                        key={m.key}
                        type="button"
                        className={`claude-menu-item ${agentMode === m.key ? 'active' : ''}`}
                        onClick={() => {
                          setAgentMode(m.key);
                          setShowModeMenu(false);
                        }}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: '8px',
                          width: '100%',
                          padding: '6px 8px',
                          background: agentMode === m.key ? 'rgba(56, 189, 248, 0.1)' : 'transparent',
                          border: 'none',
                          color: 'var(--text)',
                          cursor: 'pointer',
                          borderRadius: '6px',
                          textAlign: 'left',
                        }}
                      >
                        <span style={{ width: '12px', color: '#38bdf8' }}>{agentMode === m.key ? '✓' : ''}</span>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: '12px', fontWeight: 600 }}>{m.label}</div>
                          <div style={{ fontSize: '10px', color: 'var(--dim)' }}>{m.desc}</div>
                        </div>
                        <kbd style={{ fontSize: '10px', background: 'rgba(255,255,255,0.06)', padding: '1px 4px', borderRadius: '3px', color: 'var(--dim)' }}>{m.hotkey}</kbd>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Web Search & KB Mode Toggles (Chat mode only, default ON in agent & code mode) */}
              {appMode === 'chat' && (
                <>
                  <button
                    id="btn-web-toggle"
                    className={`claude-tool-icon-btn comp-tool comp-web-btn ${webSearchEnabled ? 'active' : ''}`}
                    type="button"
                    title={`Web search is ${webSearchEnabled ? 'ON' : 'OFF'}`}
                    aria-label="Toggle web search"
                    onClick={toggleWebSearch}
                    style={{
                      background: webSearchEnabled ? 'rgba(56, 189, 248, 0.15)' : 'transparent',
                      border: webSearchEnabled ? '1px solid #38bdf8' : '1px solid transparent',
                      borderRadius: '6px',
                      color: webSearchEnabled ? '#38bdf8' : 'var(--dim)',
                      cursor: 'pointer',
                      padding: '4px 6px',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                    }}
                  >
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                      <circle cx="12" cy="12" r="10" />
                      <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20" />
                      <path d="M2 12h20" />
                    </svg>
                  </button>

                  <button
                    id="btn-kb-toggle"
                    className={`claude-tool-icon-btn comp-tool comp-kb-btn ${kbMode !== 'off' ? 'active' : ''}`}
                    type="button"
                    data-mode={kbMode}
                    title={`Company knowledge base: ${kbMode.toUpperCase()}`}
                    aria-label={`Company knowledge base: ${kbMode.toUpperCase()}`}
                    onClick={() => {
                      const next = kbMode === 'auto' ? 'always' : kbMode === 'always' ? 'off' : 'auto';
                      setKbMode(next);
                    }}
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '4px',
                      background: 'transparent',
                      border: 'none',
                      color: kbMode === 'off' ? 'var(--dim)' : 'inherit',
                      opacity: kbMode === 'off' ? 0.5 : 0.85,
                      cursor: 'pointer',
                      padding: '2px 6px',
                    }}
                  >
                    <svg
                      width="15"
                      height="15"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      style={{ flexShrink: 0 }}
                      aria-hidden="true"
                    >
                      <ellipse cx="12" cy="5" rx="8" ry="3" />
                      <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" />
                      <path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" />
                    </svg>
                    <span className="comp-kb-tag" style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.3px', textTransform: 'uppercase' }}>
                      {kbMode}
                    </span>
                  </button>
                </>
              )}
            </div>

            {/* Right Controls: Model   Medium ▾   ○ */}
            <div className="claude-bar-right" style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              {/* Model Picker Button & Dropdown */}
              <div id="model-picker-wrap" style={{ position: 'relative' }}>
                <button
                  id="btn-claude-model"
                  className="claude-tool-text-btn"
                  type="button"
                  title="Choose Model"
                  onClick={(e) => {
                    e.stopPropagation();
                    setShowModelDropdown(!showModelDropdown);
                  }}
                  style={{
                    background: 'transparent',
                    border: 'none',
                    color: 'var(--dim)',
                    cursor: 'pointer',
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: '4px',
                    fontSize: '12px',
                    padding: '2px 4px',
                  }}
                >
                  <span id="claude-model-text" title={currentModelDisplay} style={{ display: "inline-block", maxWidth: "220px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", verticalAlign: "bottom" }}>{currentModelDisplay}</span>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <path d="m6 9 6 6 6-6" />
                  </svg>
                </button>

                {showModelDropdown && (
                  <div
                    id="model-picker-dropdown"
                    className="model-picker-dropdown"
                    style={{
                      display: 'block',
                      position: 'absolute',
                      bottom: '100%',
                      right: 0,
                      marginBottom: '8px',
                      width: '320px',
                      background: 'var(--panel2, #1c1c21)',
                      border: '1px solid var(--border)',
                      borderRadius: '10px',
                      padding: '8px',
                      boxShadow: '0 8px 30px rgba(0,0,0,0.5)',
                      zIndex: 9999,
                    }}
                  >
                    <input
                      type="search"
                      placeholder="Search models…"
                      value={modelSearch}
                      onChange={(e) => setModelSearch(e.target.value)}
                      autoFocus
                      style={{
                        width: '100%',
                        boxSizing: 'border-box',
                        padding: '6px 10px',
                        borderRadius: '6px',
                        border: '1px solid var(--border)',
                        background: 'var(--bg, #101014)',
                        color: 'inherit',
                        fontSize: '12px',
                        marginBottom: '6px',
                        outline: 'none',
                      }}
                    />
                    <div style={{ maxHeight: '260px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '4px' }}>
                      {groupedModels.length === 0 ? (
                        <div className="dim" style={{ fontSize: '11.5px', padding: '12px', textAlign: 'center' }}>
                          No matching models found.
                        </div>
                      ) : (
                        groupedModels.map((grp: any, gIdx: number) => (
                          <div key={grp.name} className="model-group-section">
                            {/* Provider Header / Separator */}
                            <div
                              style={{
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'space-between',
                                padding: '6px 8px 3px 8px',
                                borderTop: gIdx > 0 ? '1px solid var(--border)' : 'none',
                                marginTop: gIdx > 0 ? '6px' : '0',
                                marginBottom: '2px',
                              }}
                            >
                              <div style={{ display: 'flex', alignItems: 'center', gap: '5px' }}>
                                <span style={{ fontSize: '11px' }}>{grp.isLocal ? '🖥️' : '☁️'}</span>
                                <span
                                  style={{
                                    fontSize: '10px',
                                    fontWeight: 700,
                                    textTransform: 'uppercase',
                                    letterSpacing: '0.05em',
                                    color: grp.isLocal ? 'var(--blue, #38bdf8)' : 'var(--accent, #6366f1)',
                                  }}
                                >
                                  {grp.name}
                                </span>
                              </div>
                              <span style={{ fontSize: '9.5px', color: 'var(--dim)', opacity: 0.8 }}>
                                {grp.items.length} {grp.items.length === 1 ? 'model' : 'models'}
                              </span>
                            </div>

                            {/* Provider Models */}
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                              {grp.items.map((m: any) => {
                                const isCurrent = selectedModel === m.display || selectedModel === m.id || (serverStatus?.cloud_main && ('cloud:' + serverStatus.cloud_main.key === m.id || serverStatus.cloud_main.display === m.display));
                                return (
                                  <button
                                    key={m.id}
                                    type="button"
                                    className={`claude-menu-item ${isCurrent ? 'active' : ''}`}
                                    style={{
                                      display: 'flex',
                                      alignItems: 'center',
                                      justifyContent: 'space-between',
                                      padding: '6px 10px',
                                      borderRadius: '6px',
                                      background: isCurrent ? 'rgba(56,189,248,0.12)' : 'transparent',
                                      border: isCurrent ? '1px solid rgba(56,189,248,0.25)' : '1px solid transparent',
                                      color: 'inherit',
                                      cursor: 'pointer',
                                      fontSize: '11.5px',
                                      textAlign: 'left',
                                      transition: 'background 0.15s ease',
                                    }}
                                    onClick={() => {
                                      setSelectedModel(m.id || m.display);
                                      setShowModelDropdown(false);
                                    }}
                                  >
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', minWidth: 0 }}>
                                      <span style={{ fontWeight: isCurrent ? 700 : 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                        {m.display || m.id}
                                      </span>
                                      {m.currently_loaded && (
                                        <span style={{ fontSize: '9px', padding: '1px 5px', borderRadius: '4px', background: 'rgba(34,197,94,0.15)', color: '#22c55e', fontWeight: 600 }}>
                                          Active
                                        </span>
                                      )}
                                    </div>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
                                      {m.ctx ? (
                                        <span style={{ fontSize: '10px', color: 'var(--dim)', opacity: 0.7 }}>
                                          {m.ctx >= 1000 ? `${Math.round(m.ctx / 1000)}k` : m.ctx}
                                        </span>
                                      ) : null}
                                      {isCurrent && (
                                        <span style={{ fontSize: '12px', color: 'var(--blue, #38bdf8)', fontWeight: 700 }}>✓</span>
                                      )}
                                    </div>
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        ))
                      )}
                    </div>
                  </div>
                )}
              </div>

              {/* Reasoning Effort Dropdown */}
              <div className="effort-wrap" id="effort-wrap" style={{ position: 'relative' }}>
                <button
                  id="effort-btn"
                  className="claude-tool-text-btn effort-btn"
                  type="button"
                  aria-haspopup="menu"
                  aria-expanded={showEffortMenu}
                  title="Reasoning effort"
                  onClick={(e) => {
                    e.stopPropagation();
                    setShowEffortMenu(!showEffortMenu);
                  }}
                  style={{
                    background: 'transparent',
                    border: 'none',
                    color: 'var(--dim)',
                    cursor: 'pointer',
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: '4px',
                    fontSize: '12px',
                    padding: '2px 4px',
                  }}
                >
                  <span id="effort-label">{effortDisplay}</span>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <path d="m6 9 6 6 6-6" />
                  </svg>
                </button>

                {showEffortMenu && (
                  <div
                    className="effort-menu open"
                    id="effort-menu"
                    role="menu"
                    style={{
                      display: 'block',
                      position: 'absolute',
                      bottom: '100%',
                      right: 0,
                      marginBottom: '8px',
                      background: 'var(--panel, #18181b)',
                      border: '1px solid var(--border-subtle, rgba(255, 255, 255, 0.12))',
                      borderRadius: '12px',
                      padding: '6px',
                      minWidth: '170px',
                      boxShadow: '0 12px 36px rgba(0,0,0,0.5)',
                      zIndex: 9999,
                      animation: 'claudePopIn 0.12s cubic-bezier(0.16, 1, 0.3, 1)',
                      transformOrigin: 'bottom right',
                    }}
                  >
                    {(['none', 'low', 'medium', 'high', 'extra'] as ReasoningEffort[]).map((level) => (
                      <button
                        key={level}
                        type="button"
                        className={`effort-item ${reasoningEffort === level ? 'active' : ''}`}
                        role="menuitemradio"
                        data-level={level}
                        onClick={() => {
                          setReasoningEffort(level);
                          setShowEffortMenu(false);
                        }}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          width: '100%',
                          padding: '7px 12px',
                          background: 'transparent',
                          border: 'none',
                          color: 'var(--text)',
                          cursor: 'pointer',
                          borderRadius: '6px',
                          fontSize: '13px',
                          textAlign: 'left',
                        }}
                      >
                        <span style={{ textTransform: 'capitalize' }}>{level}</span>
                      </button>
                    ))}
                    <div style={{ height: '1px', background: 'var(--border-subtle, rgba(255, 255, 255, 0.08))', margin: '6px 4px' }} />
                    <div
                      className="effort-deep"
                      id="effort-deep"
                      role="menuitemcheckbox"
                      aria-checked={deepResearch}
                      title="Deep research"
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleDeepResearch();
                      }}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        width: '100%',
                        padding: '8px 12px',
                        background: 'transparent',
                        border: 'none',
                        color: 'var(--text)',
                        cursor: 'pointer',
                        fontSize: '13px',
                        borderRadius: '6px',
                        boxSizing: 'border-box',
                      }}
                    >
                      <span>Deep research</span>
                      <div
                        style={{
                          width: '36px',
                          height: '20px',
                          borderRadius: '10px',
                          background: deepResearch ? '#a78bfa' : 'rgba(255, 255, 255, 0.18)',
                          position: 'relative',
                          transition: 'background 0.2s ease',
                          cursor: 'pointer',
                          flexShrink: 0,
                        }}
                      >
                        <div
                          style={{
                            position: 'absolute',
                            top: '2px',
                            left: deepResearch ? '18px' : '2px',
                            width: '16px',
                            height: '16px',
                            borderRadius: '50%',
                            background: '#ffffff',
                            boxShadow: '0 1px 3px rgba(0,0,0,0.4)',
                            transition: 'left 0.2s ease',
                          }}
                        />
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Context Window Pie Chart (○) */}
              <div
                className="claude-ctx-wrap"
                id="claude-ctx-wrap"
                style={{ position: 'relative' }}
                onMouseEnter={() => setShowCtxTooltip(true)}
                onMouseLeave={() => setShowCtxTooltip(false)}
              >
                <button
                  id="btn-claude-ctx"
                  className="claude-tool-icon-btn claude-ctx-btn"
                  type="button"
                  title="Context usage"
                  aria-label="Context usage"
                  style={{
                    background: 'none',
                    border: 'none',
                    padding: 0,
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: 'var(--dim)',
                  }}
                >
                  <svg className="claude-ctx-svg" width="16" height="16" viewBox="0 0 36 36">
                    <circle
                      className="claude-ctx-bg"
                      cx="18"
                      cy="18"
                      r="14"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      opacity="0.25"
                    />
                    <circle
                      className="claude-ctx-fill"
                      id="claude-ctx-fill"
                      cx="18"
                      cy="18"
                      r="14"
                      fill="none"
                      stroke={contextStats.color}
                      strokeWidth="2.5"
                      strokeDasharray="88 88"
                      strokeDashoffset={contextStats.offset}
                      transform="rotate(-90 18 18)"
                      strokeLinecap="round"
                    />
                  </svg>
                </button>
                {showCtxTooltip && (
                  <div
                    className="claude-ctx-popup"
                    id="claude-ctx-popup"
                    role="tooltip"
                    style={{
                      display: 'block',
                      position: 'absolute',
                      bottom: '100%',
                      right: 0,
                      marginBottom: '6px',
                      background: 'var(--panel2, #1c1c21)',
                      border: '1px solid var(--border)',
                      borderRadius: '6px',
                      padding: '4px 8px',
                      fontSize: '11px',
                      whiteSpace: 'nowrap',
                      zIndex: 9999,
                    }}
                  >
                    <span>
                      {contextStats.fmtK(contextStats.totalToks)} / {contextStats.fmtK(contextStats.nCtx)} tokens ({contextStats.pct}%)
                    </span>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </footer>
  );
};
