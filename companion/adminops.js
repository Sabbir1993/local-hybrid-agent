// companion/adminops.js - the Administrator ("System") terminal: commands the PERSON types, run with Windows administrator rights.
//
// Why a separate process: the companion keeps running with the user's normal rights. Elevating it would elevate everything it does
// (the agent's file and shell tools included). Instead a small PowerShell helper is started elevated (Windows shows its UAC prompt),
// and only `shell.admin_run` requests reach it.
//
// What keeps it safe:
//  - it always asks: a native confirmation window of THIS app on THIS PC (never skippable by the web page or a model), then UAC
//  - the helper listens on a randomly named pipe that only the signed-in Windows user (and the elevated account) may open, and every
//    request must carry a one-time random token
//  - the helper script is passed on the command line (-EncodedCommand), never written to a folder another process could edit
//  - nothing is remembered: no "don't ask again"; it ends on stop, on idle (helper and companion both), or when the app quits
//  - secret-looking environment variables are removed before a command runs, as for the normal shell
//
// Windows only. The server (routes/projects/companion_endpoints.py) decides who may use it and audits every command.

const { spawn } = require("child_process");
const crypto = require("crypto");
const net = require("net");
const policy = require("./policy");

const MAX_OUTPUT_CHARS = 20000;
const START_WAIT_MS = 60000;           // UAC + helper start

let session = null;                    // { pipe, token, idleMs, timer }
// the real Windows tool, not whichever "whoami" comes first on PATH (Git for Windows ships a different one)
const WHOAMI = require("path").join(process.env.SystemRoot || "C:\\Windows", "System32", "whoami.exe");

// PowerShell source of the elevated helper. Values are injected only after strict validation (see buildHelperScript).
const HELPER_TEMPLATE = String.raw`
$ErrorActionPreference = 'Stop'
$Pipe = '@PIPE@'; $Token = '@TOKEN@'; $UserSid = '@SID@'; $IdleSec = @IDLE@
$secretRx = '(SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|PRIVATE|CREDENTIAL|SESSION_KEY|ACCESS_KEY|_KEY$|^KEY$)'
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$elevated = ([System.Security.Principal.WindowsPrincipal]$me).IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
$sec = New-Object System.IO.Pipes.PipeSecurity
foreach ($s in @((New-Object System.Security.Principal.SecurityIdentifier($UserSid)), $me.User)) {
  $sec.AddAccessRule((New-Object System.IO.Pipes.PipeAccessRule($s, 'ReadWrite', 'Allow')))
}
$srv = New-Object System.IO.Pipes.NamedPipeServerStream($Pipe, 'InOut', 1, 'Byte', 'Asynchronous', 65536, 65536, $sec)
$enc = New-Object System.Text.UTF8Encoding($false)
$last = Get-Date
function Reply($w, $o) { $w.WriteLine(($o | ConvertTo-Json -Compress -Depth 4)) }
while ($true) {
  $a = $srv.BeginWaitForConnection($null, $null)
  while (-not $a.AsyncWaitHandle.WaitOne(1000)) { if (((Get-Date) - $last).TotalSeconds -gt $IdleSec) { exit 0 } }
  $srv.EndWaitForConnection($a)
  $r = New-Object System.IO.StreamReader($srv, $enc); $w = New-Object System.IO.StreamWriter($srv, $enc); $w.AutoFlush = $true
  try {
    $req = ($r.ReadLine() | ConvertFrom-Json)
    if ($req.token -ne $Token) { Reply $w @{ ok = $false; error = 'bad token' } }
    elseif ($req.op -eq 'ping') { Reply $w @{ ok = $true; elevated = $elevated; user = $me.Name } }
    elseif ($req.op -eq 'exit') { Reply $w @{ ok = $true }; $srv.Disconnect(); exit 0 }
    elseif ($req.op -eq 'run') {
      $sh = [string]$req.shell
      $cmd = [string]$req.command
      $psi = New-Object System.Diagnostics.ProcessStartInfo
      if ($sh -eq 'cmd') { $psi.FileName = 'cmd.exe'; $psi.Arguments = '/d /s /c "' + $cmd + '"' }
      else {
        $psi.FileName = $(if ($sh -eq 'pwsh') { 'pwsh.exe' } else { 'powershell.exe' })
        $psi.Arguments = '-NoProfile -NonInteractive -EncodedCommand ' + [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
      }
      if ($req.cwd -and (Test-Path -LiteralPath $req.cwd)) { $psi.WorkingDirectory = [string]$req.cwd }
      $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
      $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
      foreach ($k in @($psi.EnvironmentVariables.Keys)) { if ($k -match $secretRx) { $psi.EnvironmentVariables.Remove($k) } }
      $p = [System.Diagnostics.Process]::Start($psi)
      $so = $p.StandardOutput.ReadToEndAsync(); $se = $p.StandardError.ReadToEndAsync()
      $ms = [Math]::Min([Math]::Max([int]$req.timeout, 1), 300) * 1000
      if ($p.WaitForExit($ms)) { $code = $p.ExitCode; $err = $se.Result }
      else { & taskkill /PID $p.Id /T /F | Out-Null; $code = 124; $err = "timed out after $($ms / 1000)s" }
      $out = $so.Result
      Reply $w @{ ok = $true; exit_code = $code; stdout = $out.Substring(0, [Math]::Min($out.Length, 20000)); stderr = ([string]$err).Substring(0, [Math]::Min(([string]$err).Length, 20000)) }
    }
    else { Reply $w @{ ok = $false; error = 'unknown op' } }
  } catch { try { Reply $w @{ ok = $false; error = $_.Exception.Message } } catch {} }
  $last = Get-Date
  try { $srv.Disconnect() } catch {}
}
`;

function buildHelperScript({ pipe, token, sid, idleSec }) {
  // nothing but these shapes may reach the script text
  if (!/^a770-admin-[A-Za-z0-9]{16,64}$/.test(pipe)) throw new Error("bad pipe name");
  if (!/^[A-Za-z0-9]{32,128}$/.test(token)) throw new Error("bad token");
  if (!/^S-1-\d+(?:-\d+)+$/.test(sid)) throw new Error("bad user id");
  const idle = Math.min(Math.max(Math.round(Number(idleSec) || 900), 60), 7200);
  return HELPER_TEMPLATE.replace("@PIPE@", pipe).replace("@TOKEN@", token).replace("@SID@", sid).replace("@IDLE@", String(idle));
}

function pipePath(name) {
  return process.platform === "win32" ? `\\\\.\\pipe\\${name}` : name;
}

// one request, one reply (newline-delimited JSON) over the helper's pipe
function request(pipe, payload, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ path: pipePath(pipe) });
    let buf = "";
    let done = false;
    const finish = (fn, v) => { if (done) return; done = true; clearTimeout(t); try { sock.destroy(); } catch (_) {} fn(v); };
    const t = setTimeout(() => finish(reject, new Error("the administrator terminal did not answer in time")), timeoutMs);
    sock.setEncoding("utf8");
    sock.on("connect", () => sock.write(JSON.stringify(payload) + "\n"));
    sock.on("data", (d) => {
      buf += d;
      const i = buf.indexOf("\n");
      if (i >= 0) {
        try { finish(resolve, JSON.parse(buf.slice(0, i))); } catch (e) { finish(reject, new Error("unreadable answer from the administrator terminal")); }
      }
    });
    sock.on("error", (e) => finish(reject, e));
    sock.on("close", () => finish(reject, new Error("the administrator terminal closed the connection")));
  });
}

function currentSid() {
  return new Promise((resolve, reject) => {
    let out = "";
    const c = spawn(WHOAMI, ["/user", "/fo", "csv", "/nh"], { windowsHide: true });
    c.stdout.on("data", (d) => { out += d; });
    c.on("error", reject);
    c.on("close", () => {
      const m = out.match(/"(S-1-[\d-]+)"/);
      m ? resolve(m[1]) : reject(new Error("could not read the Windows user id"));
    });
  });
}

function launchElevated(script) {
  return new Promise((resolve, reject) => {
    const b64 = Buffer.from(script, "utf16le").toString("base64");
    const cmd = `Start-Process -FilePath powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','${b64}'`;
    let err = "";
    const c = spawn("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", cmd], { windowsHide: true });
    c.stderr.on("data", (d) => { err += d; });
    c.on("error", reject);
    c.on("close", (code) => (code === 0 ? resolve() : reject(new Error(/cancel/i.test(err) ? "Windows administrator permission was declined" : `could not start the administrator helper: ${err.trim().slice(0, 160)}`))));
  });
}

function armIdle() {
  if (!session) return;
  clearTimeout(session.timer);
  session.timer = setTimeout(() => { stop().catch(() => {}); }, session.idleMs);
}

async function start(p) {
  if (process.platform !== "win32") throw new Error("the administrator terminal is available on Windows only");
  if (session) {
    try { const r = await request(session.pipe, { token: session.token, op: "ping" }, 5000); if (r && r.ok && r.elevated) { armIdle(); return { ok: true, user: r.user, reused: true }; } } catch (_) {}
    session = null;
  }
  // 1. always asked, on this PC, by this app: no remembered choice, and the web page cannot answer for the person
  await policy.confirmAction(
    "allow administrator terminal?",
    "Allow the terminal to run commands as Windows ADMINISTRATOR on this PC?",
    "Only commands you type in the terminal's System mode will run with these rights. The AI agent's own tools stay as they are.\n" +
    "It switches off when you go back to Project, close the panel, or after a while idle. Windows will ask for permission next.",
    null, false);
  const idleMs = Math.min(Math.max(Number(p && p.idle_min) || 15, 1), 120) * 60 * 1000;
  const pipe = "a770-admin-" + crypto.randomBytes(12).toString("hex");
  const token = crypto.randomBytes(24).toString("hex");
  const script = buildHelperScript({ pipe, token, sid: await currentSid(), idleSec: idleMs / 1000 + 60 });
  // 2. Windows UAC
  await launchElevated(script);
  // 3. wait until the helper answers and really is elevated
  const t0 = Date.now();
  let hello = null;
  while (Date.now() - t0 < START_WAIT_MS && !hello) {
    try { hello = await request(pipe, { token, op: "ping" }, 3000); } catch (_) { await new Promise((r) => setTimeout(r, 500)); }
  }
  if (!hello || !hello.ok) throw new Error("the administrator helper did not start");
  if (!hello.elevated) {
    try { await request(pipe, { token, op: "exit" }, 3000); } catch (_) {}
    throw new Error("Windows did not grant administrator rights (the account is not an administrator)");
  }
  session = { pipe, token, idleMs, timer: null };
  armIdle();
  return { ok: true, user: hello.user };
}

async function run(p) {
  if (!session) throw new Error("the administrator terminal is not running - switch to System mode and confirm again");
  armIdle();
  const r = await request(session.pipe, {
    token: session.token, op: "run", command: String(p.command || ""), cwd: p.cwd || "",
    timeout: Math.min(Math.max(Number(p.timeout) || 60, 1), 300), shell: String(p.shell || "powershell").toLowerCase(),
  }, (Math.min(Math.max(Number(p.timeout) || 60, 1), 300) + 15) * 1000);
  if (!r || !r.ok) throw new Error((r && r.error) || "the administrator command failed");
  const cap = (s) => String(s || "").slice(0, MAX_OUTPUT_CHARS);
  return { exit_code: typeof r.exit_code === "number" ? r.exit_code : 0, stdout: cap(r.stdout), stderr: cap(r.stderr) };
}

async function stop() {
  const s = session;
  session = null;
  if (!s) return { ok: true, stopped: false };
  clearTimeout(s.timer);
  try { await request(s.pipe, { token: s.token, op: "exit" }, 5000); } catch (_) { /* it also ends by itself when idle */ }
  return { ok: true, stopped: true };
}

function isActive() { return !!session; }

module.exports = { start, run, stop, isActive, _buildHelperScript: buildHelperScript, _request: request };
