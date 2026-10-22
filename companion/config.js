// companion/config.js
// Server URL: A770_SERVER_URL from the environment, or from a .env file next
// to the installed executable (admins can drop one in without rebuilding), or
// from companion/.env when running from source (see .env.example).
const path = require("path");
try {
  const dotenv = require("dotenv");
  dotenv.config({ path: path.join(path.dirname(process.execPath), ".env") });
  dotenv.config({ path: path.join(__dirname, ".env") });   // never overrides the above
} catch (_) {}

function parseServerUrl(raw) {
  if (!raw) return { url: null, error: "A770_SERVER_URL is not set" };
  let u;
  try {
    u = new URL(String(raw).trim());
  } catch (_) {
    return { url: null, error: `A770_SERVER_URL is not a valid URL: ${raw}` };
  }
  const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(u.hostname);
  if (u.protocol !== "https:" && !(u.protocol === "http:" && loopback)) {
    return { url: null, error: "A770_SERVER_URL must be https:// (http:// only for localhost)" };
  }
  return { url: u.origin, error: null };
}

const parsed = parseServerUrl(process.env.A770_SERVER_URL);

// Frontend URL: if A770_FRONTEND_URL is set, the companion loads that URL (e.g. http://localhost:5173).
// If unset, it defaults to SERVER_URL (e.g. http://127.0.0.1:8000).
const frontendRaw = process.env.A770_FRONTEND_URL ? String(process.env.A770_FRONTEND_URL).trim() : null;
const parsedFrontend = frontendRaw ? parseServerUrl(frontendRaw) : { url: null, error: null };

const START_URL = parsedFrontend.url || parsed.url;

module.exports = {
  SERVER_URL: parsed.url,
  SERVER_URL_ERROR: parsed.error,
  FRONTEND_URL: parsedFrontend.url,
  START_URL,
  parseServerUrl,
};
