// Sampling settings (port of static/js/sampling.js). They are edited on the Settings page, which saves them to
// localStorage ('sampling_config') and to the server; this module only reads them and builds request fields.

import { apiFetch } from './api';

/** A browser with no saved sampler settings (first visit, cleared storage, a new desktop profile) starts from the values the
 *  admin saved on the server (config/sampling_config.json) rather than the built-in defaults. Same rule as the old UI. */
export async function syncSamplingFromServer(): Promise<void> {
  try {
    if (localStorage.getItem('sampling_config')) return;
    const r = await apiFetch('/control/sampling', { background: true });
    if (!r.ok) return;
    const d = await r.json();
    if (d && typeof d === 'object' && !d.error && !localStorage.getItem('sampling_config')) {
      localStorage.setItem('sampling_config', JSON.stringify(d));
    }
  } catch (_) {
    // offline or storage blocked: the built-in defaults stay
  }
}

const DEFAULTS = {
  sysprompt: '', temp: 0.6, topp: 0.95, minp: 0.0, rep: 1.0,
  presence: 0.0, topk: 20, maxtok: -1,
  rlast: 64, freq: 0.0, seed: -1,
  drym: 0.6, dryb: 1.75, dryl: 4, dryn: 2048,
  dynr: 0.0, dyne: 1.0,
};

type SamplingConfig = typeof DEFAULTS;

function num(v: unknown, def: number, min: number, max: number): number {
  const n = parseFloat(String(v));
  return Number.isNaN(n) ? def : Math.min(max, Math.max(min, n));
}

function int(v: unknown, def: number, min: number, max: number): number {
  const n = parseInt(String(v), 10);
  return Number.isNaN(n) ? def : Math.min(max, Math.max(min, n));
}

export function getSamplingConfig(): SamplingConfig {
  let src: any = {};
  try {
    src = JSON.parse(localStorage.getItem('sampling_config') || '{}') || {};
  } catch (_) {
    src = {};
  }
  return {
    sysprompt: typeof src.sysprompt === 'string' ? src.sysprompt : DEFAULTS.sysprompt,
    temp: num(src.temp, DEFAULTS.temp, 0, 2),
    topp: num(src.topp, DEFAULTS.topp, 0, 1),
    minp: num(src.minp, DEFAULTS.minp, 0, 1),
    rep: num(src.rep, DEFAULTS.rep, 0, 3),
    presence: num(src.presence, DEFAULTS.presence, -2, 2),
    topk: int(src.topk, DEFAULTS.topk, 0, 1000),
    maxtok: int(src.maxtok, DEFAULTS.maxtok, -1, 1048576),
    rlast: int(src.rlast, DEFAULTS.rlast, -1, 8192),
    freq: num(src.freq, DEFAULTS.freq, -2, 2),
    seed: int(src.seed, DEFAULTS.seed, -1, 2147483647),
    drym: num(src.drym, DEFAULTS.drym, 0, 5),
    dryb: num(src.dryb, DEFAULTS.dryb, 1, 4),
    dryl: int(src.dryl, DEFAULTS.dryl, 0, 64),
    dryn: int(src.dryn, DEFAULTS.dryn, -1, 32768),
    dynr: num(src.dynr, DEFAULTS.dynr, 0, 2),
    dyne: num(src.dyne, DEFAULTS.dyne, 0.1, 5),
  };
}

/**
 * Request fields for /chat/run and /agent/run. With a custom agent active the temperature is left out so the
 * agent's own value applies (the server fills it in).
 */
export function samplingBody(opts: { customAgent: boolean }): Record<string, unknown> {
  const c = getSamplingConfig();
  return {
    ...(opts.customAgent ? {} : { temperature: c.temp }),
    max_tokens: c.maxtok <= 0 ? -1 : c.maxtok,
    top_p: c.topp,
    min_p: c.minp,
    repeat_penalty: c.rep,
    presence_penalty: c.presence,
    top_k: c.topk,
    repeat_last_n: c.rlast,
    frequency_penalty: c.freq,
    seed: c.seed,
    dry_multiplier: c.drym,
    dry_base: c.dryb,
    dry_allowed_length: c.dryl,
    dry_penalty_last_n: c.dryn,
    dynatemp_range: c.dynr,
    dynatemp_exponent: c.dyne,
    system_prompt: c.sysprompt.trim() || undefined,
  };
}
