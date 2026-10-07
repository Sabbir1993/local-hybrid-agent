// Flat config for the vanilla-JS control center (static/js/*). Rules are sized
// to the actual codebase: it is browser-global-scoped, not ES modules, so every
// file shares one implicit namespace via `window.*` exports and bare globals.
// The lint goaP is catching real defects (undefined names, lost bindings) at
// zero-not-yet cost to the app: no rewrite, no build step, plain node.
import { defineConfig, globalIgnores } from "eslint/config";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));

// The app is browser-global-scoped: `window.X = ...` in one file is used as a
// bare `X(...)` in others. Enumerate those exports from source so no-undef can
// tell a real typo from an intentional cross-file binding, and so the list can
// never go stale - adding a global needs no config edit, removing one makes
// its uses immediately undefined.
const appGlobals = {};
// Files are IIFE-wrapped/global-scoped: a `function X` or `const X =` at ANY
// indent is still a window global (`function` at top level, or a declaration
// inside an IIFE that is assigned). eslint's per-file view counts them as
// undefined in sibling files, so enumerate every declaration/assignment name.
const declRx = /(?:^|[\s;(,{])(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*=|function\s+([A-Za-z_$][\w$]*)\s*\(/g;
for (const f of fs.readdirSync(path.join(ROOT, "static/js"))) {
  if (!f.endsWith(".js")) continue;
  const src = fs.readFileSync(path.join(ROOT, "static/js", f), "utf8");
  for (const m of src.matchAll(/window\.(\w+)\s*=/g)) {
    appGlobals[m[1]] = "readonly";
  }
  for (const m of src.matchAll(declRx)) {
    const g = m[1] || m[2];
    if (g) {
      appGlobals[g] = "readonly";
    }
  }
}

export default defineConfig([
  globalIgnores([
    "static/vendor/**",
    "companion/**",
    "node_modules/**",
  ]),
  {
    files: ["static/js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: {
        // browser + the globals the app itself installs (window.X = ... in one
        // file is used bare in others - see scripts/gen_js_globals.py)
        window: "readonly",
        document: "readonly",
        navigator: "readonly",
        location: "readonly",
        history: "readonly",
        console: "readonly",
        setTimeout: "readonly",
        setInterval: "readonly",
        clearTimeout: "readonly",
        clearInterval: "readonly",
        fetch: "readonly",
        WebSocket: "readonly",
        EventSource: "readonly",
        Blob: "readonly",
        FileReader: "readonly",
        AudioContext: "readonly",
        FormData: "readonly",
        URL: "readonly",
        URLSearchParams: "readonly",
        AbortController: "readonly",
        customElements: "readonly",
        CSS: "readonly",
        matchMedia: "readonly",
        requestAnimationFrame: "readonly",
        cancelAnimationFrame: "readonly",
        structuredClone: "readonly",
        crypto: "readonly",
        TextEncoder: "readonly",
        TextDecoder: "readonly",
        Image: "readonly",
        Worker: "readonly",
        localStorage: "readonly",
        sessionStorage: "readonly",
        origin: "readonly",
        isSecureContext: "readonly",
        DeviceOrientationEvent: "readonly",
        DeviceMotionEvent: "readonly",
        Notification: "readonly",
        SpeechRecognition: "readonly",
        webkitSpeechRecognition: "readonly",
        getSelection: "readonly",
        confirm: "readonly",
        prompt: "readonly",
        performance: "readonly",
        Event: "readonly",
        CustomEvent: "readonly",
        MutationObserver: "readonly",
        IntersectionObserver: "readonly",
        Headers: "readonly",
        Request: "readonly",
        MediaRecorder: "readonly",
        module: "readonly",  // a few files double as node-runnable tests
        // vendored libs (static/vendor/*.js) expose globals
        mermaid: "readonly",
        XLSX: "readonly",
        marked: "readonly",
        DOMPurify: "readonly",
        // shared app namespace: window.* exports, computed from source above
        ...appGlobals,
      },
    },
    rules: {
      // the point of the gate: a bare name nobody defined is a bug, except the
      // cross-file window.* exports the app relies on - those are enumerated
      // and asserted in tests/test_js_globals.py so the list cannot rot.
      "no-undef": "error",
      // deliberately OFF: one file's top-level `let X` is another file's input,
      // so "used in this file only" is the wrong question in a global-scope app.
      "no-undef-init": "error",
      "no-compare-neg-zero": "error",
      "no-constant-binary-expression": "error",
      "no-dupe-else-if": "error",
      "no-duplicate-imports": "error",
      "no-import-assign": "error",
      "no-loss-of-precision": "error",
      "no-promise-executor-return": "error",
      "no-self-compare": "error",
      "no-unreachable-loop": "error",
      "no-unused-private-class-members": "error",
      "valid-typeof": "error",
    },
  },
]);
