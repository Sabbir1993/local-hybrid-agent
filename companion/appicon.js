// Which icon the companion shows (tray, window). Kept apart from main.js so it can be tested without Electron.
//
// A packaged app does not contain companion/build/ (electron-builder keeps its build resources out of the asar),
// so the icons are shipped as extraResources and read from process.resourcesPath. If nothing loads, a built-in
// image is used: a tray created from an empty image is an invisible entry on Windows, which looks like the app
// never started.
const path = require("path");

// 32x32 teal square, generated once; only used when no icon file can be loaded
const FALLBACK_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAOklEQVR4nO3OMQ0AIAwAsDngJOFCNE4QOFywHT36N8bcWSnaBNY9XwkICAgICAgICAgICAj0C1QpDzy4XXcp0QzEZgAAAABJRU5ErkJggg==";

function iconCandidates({ platform, resourcesPath, dirname }) {
  const names = platform === "win32" ? ["icon.ico", "icon.png"] : ["icon.png"];
  const dirs = [];
  if (resourcesPath) dirs.push(resourcesPath);          // installed app: shipped by extraResources
  dirs.push(path.join(dirname, "build"));               // running from source
  const out = [];
  for (const d of dirs) for (const n of names) out.push(path.join(d, n));
  out.push(path.join(dirname, "..", "static", "logo", "logo.jpg"));   // source checkout only
  return out;
}

function loadAppIcon({ nativeImage, fs, platform = process.platform, resourcesPath = process.resourcesPath, dirname = __dirname, warn = console.warn }) {
  for (const p of iconCandidates({ platform, resourcesPath, dirname })) {
    try {
      if (!fs.existsSync(p)) continue;
      const img = nativeImage.createFromPath(p);
      if (img && !img.isEmpty()) return { image: img, path: p };
    } catch (_) { /* try the next one */ }
  }
  warn("[icon] no icon file could be loaded; using the built-in image");
  return { image: nativeImage.createFromDataURL("data:image/png;base64," + FALLBACK_PNG_BASE64), path: null };
}

module.exports = { iconCandidates, loadAppIcon, FALLBACK_PNG_BASE64 };
