// companion/oauthops.js — receives an OAuth sign-in redirect on this machine.
//
// Remote MCP servers (Gmail, ...) need a user OAuth token, and providers like
// Google only redirect to a loopback address or a public https site. The server
// (core/mcp_oauth.py) builds the authorization URL with a placeholder redirect_uri;
// here we bind 127.0.0.1:<port>, put our URI into the URL, open the system
// browser and hand the code back. Nothing is stored on this machine; the server
// exchanges the code (PKCE) and keeps the tokens in its keychain.

const http = require("http");
const { shell } = require("electron");

const PAGE = (msg) => `<!doctype html><meta charset="utf-8"><title>Local Agent</title>
<body style="font:15px system-ui;padding:40px">${msg}<br><br>You can close this tab and return to the app.</body>`;

function loopback({ auth_url, placeholder, state, port = 0, timeout_s = 300 }) {
  const u = (() => { try { return new URL(String(auth_url || "")); } catch (_) { return null; } })();
  if (!u || u.protocol !== "https:") throw new Error("sign-in URL must be https");
  if (!placeholder || !String(auth_url).includes(encodeURIComponent(placeholder))) {
    throw new Error("sign-in URL has no redirect placeholder");
  }
  if (!state) throw new Error("state required");
  const timeoutMs = Math.min(Math.max(Number(timeout_s) || 300, 30), 600) * 1000;

  return new Promise((resolve, reject) => {
    let done = false;
    let redirectUri = "";
    const server = http.createServer((req, res) => {
      const url = new URL(req.url, "http://127.0.0.1");
      if (url.pathname !== "/callback") { res.writeHead(404); res.end(); return; }
      // a stray request (another tab, a scanner) must not end the flow
      if (url.searchParams.get("state") !== state) {
        res.writeHead(400, { "Content-Type": "text/html; charset=utf-8" });
        res.end(PAGE("This sign-in link doesn't match the one the app started."));
        return;
      }
      const err = url.searchParams.get("error");
      const code = url.searchParams.get("code");
      res.writeHead(err || !code ? 400 : 200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(PAGE(err || !code ? "Sign-in was not completed." : "Signed in."));
      finish(err || !code ? { error: err || "no code returned" } : { code, state, redirect_uri: redirectUri });
    });
    const timer = setTimeout(() => finish({ error: "sign-in timed out" }), timeoutMs);
    function finish(result) {
      if (done) return;
      done = true;
      clearTimeout(timer);
      server.close();
      if (result.error) reject(new Error(result.error)); else resolve(result);
    }
    server.on("error", (e) => finish({ error: `cannot listen on 127.0.0.1:${port}: ${e.message}` }));
    server.listen(Number(port) || 0, "127.0.0.1", () => {
      redirectUri = `http://127.0.0.1:${server.address().port}/callback`;
      const target = String(auth_url).split(encodeURIComponent(placeholder)).join(encodeURIComponent(redirectUri));
      shell.openExternal(target).catch((e) => finish({ error: `could not open the browser: ${e.message}` }));
    });
  });
}

module.exports = { loopback };
