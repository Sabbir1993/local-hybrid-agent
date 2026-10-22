import React, { useEffect, useRef } from 'react';
import { sessionKey, useAppStore } from '../../stores/useAppStore';
import { VoiceEngine, type VoiceLang, type VoiceState } from '../../services/voice/engine';
import type { NoiseKey } from '../../services/voice/core';

// Hands-free voice chat. Mounted by the Composer only while voice mode is on: it starts the microphone loop on mount
// and releases the microphone on unmount (mic button, Esc, idle timeout or a failed start). It draws nothing itself.
const LANG_MAP: Record<string, VoiceLang> = { English: 'en', Bengali: 'bn' };   // anything else: let the server guess
const NOISE_MAP: Record<string, NoiseKey> = { Standard: 'standard', Crowd: 'crowd', Off: 'off' };

export const VoiceMode: React.FC<{
  lang: string;
  noise: string;
  onState: (s: VoiceState | null) => void;
  onEnd: () => void;
}> = ({ lang, noise, onState, onEnd }) => {
  const engineRef = useRef<VoiceEngine | null>(null);
  const cbRef = useRef({ onState, onEnd });
  cbRef.current = { onState, onEnd };
  const optRef = useRef({ lang: LANG_MAP[lang] || 'auto', noise: NOISE_MAP[noise] || 'standard' });
  optRef.current = { lang: LANG_MAP[lang] || 'auto', noise: NOISE_MAP[noise] || 'standard' };

  useEffect(() => {
    const st = () => useAppStore.getState();
    let bound = sessionKey(st().activeSessionId);
    const engine = new VoiceEngine(
      {
        send: (text) => { void st().sendPrompt(text, { voice: true }); },
        abort: () => st().abortGeneration(),
        isBusy: () => st().isStreaming,
        // a brand-new chat ('0') is still the conversation's own until its first message gives it an id
        isOwnSession: () => bound === '0' || sessionKey(st().activeSessionId) === bound,
        messageCount: () => st().messages.length,
        messageAt: (i) => st().messages[i],
        trimMessage: (id, content) => st().patchMessage(id, { content }),
        permissionPending: () => st().permRequest !== null,
        notify: (msg, bad) => st().showToast(msg, bad ? 'error' : 'info'),
      },
      {
        onState: (s) => { cbRef.current.onState(s === 'idle' ? null : s); },
        onLevel: (l) => {
          document.getElementById('claude-mic-split-pill')?.style.setProperty('--voice-level', l.toFixed(2));
        },
        onHeard: () => {},
        onEnded: () => cbRef.current.onEnd(),
      },
      optRef.current,
    );
    engineRef.current = engine;
    let cancelled = false;
    engine.start().then((ok) => {
      if (cancelled) engine.stop();          // ended while the microphone was still being opened
      else if (!ok) cbRef.current.onEnd();
    }).catch(() => {
      engine.stop();                         // a failed start must never leave the microphone open
      if (!cancelled) cbRef.current.onEnd();
    });
    // The conversation belongs to the chat it started in: opening another chat ends it (a brand-new chat may
    // get its id once, when its first message creates it).
    const unsub = useAppStore.subscribe((st) => {
      const k = sessionKey(st.activeSessionId);
      if (k === bound) return;
      if (bound === '0') { bound = k; return; }
      cbRef.current.onEnd();
    });
    // Esc that closed a menu or dialog is not meant for the call
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !e.defaultPrevented) cbRef.current.onEnd(); };
    document.addEventListener('keydown', onKey);
    return () => {
      unsub();
      cancelled = true;
      document.removeEventListener('keydown', onKey);
      engine.stop();
      engineRef.current = null;
      document.getElementById('claude-mic-split-pill')?.style.removeProperty('--voice-level');
      cbRef.current.onState(null);
    };
  }, []);

  // language / noise changes apply to the running conversation
  useEffect(() => {
    engineRef.current?.setOptions({ lang: LANG_MAP[lang] || 'auto', noise: NOISE_MAP[noise] || 'standard' });
  }, [lang, noise]);

  // no panel: the state is shown on the mic icon itself (colour and moving bars, see styles/voice.css)
  return null;
};
