#!/usr/bin/env python3
"""Point-and-click installer for the Linux server (Ubuntu desktop). Standard library only.

    python3 docs/install_gui.py

Opens a page in your browser at http://127.0.0.1:<port>/ and runs the install steps of
docs/INSTALL_LINUX.html when you click them. It is the same set of commands as the guide, nothing more.

How it stays safe:
  * listens on 127.0.0.1 only, and every request needs a random token printed at start-up;
  * runs only the fixed steps below. The page can pick model groups and folders, never a command;
  * root actions use pkexec, which shows Ubuntu's own password dialog. This program never sees that password;
  * the keyring and admin passwords typed on the Start step go to the server through stdin / its environment
    for that one launch. They are not written to any file or log;
  * writes no shell script: every command is started directly from Python.
Stop it with Ctrl+C in the terminal that started it, or the Quit button.
"""
import json
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
HOME = Path.home()
GROUPS = {"routers", "embedder", "whisper", "tts", "vision", "routing", "voice", "image"}
APT_PACKAGES = ("git curl wget unzip tmux python3 python3-venv python3-pip mesa-vulkan-drivers vulkan-tools pciutils "
                "linux-firmware gnome-keyring dbus-user-session linux-generic-hwe-24.04")
WHISPER_PACKAGES = "build-essential cmake libvulkan-dev glslc git"

TOKEN = secrets.token_urlsafe(24)
LOG: list = []                   # lines shown in the page
STATUS: dict = {}                # step id -> idle | running | ok | failed | warn
RUNNING = {"step": None, "proc": None, "cancel": False}
SERVER = {"proc": None}          # the application server started from the Start step

# id, title, what it does, parameters the page may send
STEPS = [
    ("check", "Check this machine", "Looks at the user, memory, disk, kernel and graphics card. Changes nothing.", []),
    ("packages", "Install system programs", "apt update, full upgrade, git, Python, Vulkan tools, keyring, newer kernel. Asks for your password in a window; can take 10+ minutes. Restart the machine afterwards if it says so.", []),
    ("gpu", "Check the graphics card", "Runs vulkaninfo. You should see the Intel Arc Pro B70, not only llvmpipe.", []),
    ("python", "Install the Python parts", "Creates .venv in the application folder and installs the dependencies.", []),
    ("binaries", "Download llama.cpp and the image program", "Fetches the Linux Vulkan builds into your home folder.", []),
    ("models", "Download models", "Only the small helpers by default (about 3.5 GB, mostly Whisper large-v3). Large chat models are not downloaded here: copy them into the models folder yourself. Files already present are skipped; interrupted downloads resume.", ["groups", "models_dir"]),
    ("whisper", "Build speech to text (optional)", "Installs build tools (password window) and builds whisper-server into ~/whisper-vulkan. About 10 minutes.", []),
    ("config", "Point the settings at this machine", "Rewrites model and program paths in config/app.json. Then check the file by hand.", ["models_dir"]),
    ("folders", "Create the data folder", "Makes the private folder for users' files (mode 700).", ["common_dir"]),
    ("keyring", "Check the password store", "Checks that the keyring Python will use is SecretService.", []),
    ("start", "Start the server", "Starts the application in the background and makes its databases private (mode 600). Needs the keyring password you choose.", ["keyring_password", "admin_user", "admin_password", "lan"]),
]
STEP_IDS = {s[0] for s in STEPS}


def log(line: str = "") -> None:
    with LOCK_LOG:
        LOG.append(line.rstrip("\n"))
        del LOG[:-4000]


LOCK_LOG = threading.Lock()


def run(argv, env=None, cwd=None, stdin_text=None, secret_env=False) -> int:
    """Run one command and stream its output to the log. The command line is printed, secrets never are."""
    log("$ " + " ".join(str(a) for a in argv))
    full_env = dict(os.environ, **(env or {}))
    try:
        p = subprocess.Popen([str(a) for a in argv], cwd=str(cwd or ROOT), env=full_env, text=True, bufsize=1,
                             stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except FileNotFoundError:
        log(f"  command not found: {argv[0]}")
        return 127
    RUNNING["proc"] = p
    if stdin_text is not None:
        try:
            p.stdin.write(stdin_text)
            p.stdin.close()
        except OSError:
            pass
    for line in p.stdout:
        log("  " + line)
    code = p.wait()
    RUNNING["proc"] = None
    if RUNNING["cancel"]:
        log("  stopped")
        return 130
    if code:
        log(f"  exit code {code}")
    return code


def capture(argv) -> str:
    try:
        return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def as_root(shell_command: str) -> int:
    """One graphical password prompt (pkexec) for a fixed command string."""
    if not shutil.which("pkexec"):
        log("pkexec not found: install it or run this one step from the terminal (see the guide).")
        return 127
    return run(["pkexec", "env", "DEBIAN_FRONTEND=noninteractive", "sh", "-c", shell_command])


def safe_path(value: str, default: str) -> Path:
    v = (value or default).strip()
    p = Path(v).expanduser()
    if not p.is_absolute() or ".." in p.parts:
        raise ValueError(f"not a usable folder: {v}")
    return p


def models_cmd(extra: list, models_dir: Path) -> list:
    return [PY, ROOT / "scripts" / "download_models.py", *extra, "--models-dir", models_dir]


# ---------------------------------------------------------------- steps: each returns "ok", "warn" or "failed"
def step_check(_p) -> str:
    bad = False
    log(f"User: {os.environ.get('USER', '?')}   Python: {platform.python_version()}   Kernel: {platform.release()}")
    if os.geteuid() == 0:
        log("PROBLEM: this is running as root. Start it as your normal user.")
        bad = True
    osr = Path("/etc/os-release").read_text(errors="ignore") if Path("/etc/os-release").exists() else ""
    name = next((l.split("=", 1)[1].strip('"\n') for l in osr.splitlines() if l.startswith("PRETTY_NAME=")), "unknown")
    log(f"System: {name}")
    if sys.version_info < (3, 10):
        log("PROBLEM: Python 3.10 or newer is needed.")
        bad = True
    try:
        mem_kb = int(next(l for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemTotal")).split()[1])
        log(f"Memory: {mem_kb / 1048576:.0f} GB")
    except (OSError, StopIteration, ValueError):
        pass
    free = shutil.disk_usage(HOME).free / 1e9
    log(f"Free disk in your home folder: {free:.0f} GB (about 60 GB needed)")
    if free < 60:
        log("WARNING: less than 60 GB free.")
    out = capture(["lspci", "-nnk"])
    block = [l for l in out.splitlines() if "VGA" in l or "Display" in l or "Kernel driver" in l]
    log("Graphics (lspci):" if block else "lspci not available yet (it is installed by the next step).")
    for l in block:
        log("  " + l.strip())
    if not shutil.which("pkexec"):
        log("WARNING: pkexec missing, the steps that need administrator rights cannot open a password window.")
    return "failed" if bad else "ok"


def step_packages(_p) -> str:
    code = as_root(f"apt-get update && apt-get full-upgrade -y && apt-get install -y {APT_PACKAGES}")
    if code == 0:
        log("Done. If a new kernel was installed, restart the machine now (top-right menu, Power Off / Log Out, Restart), "
            "then start this installer again and continue with the next step.")
    return "ok" if code == 0 else "failed"


def step_gpu(_p) -> str:
    if not shutil.which("vulkaninfo"):
        log("vulkaninfo missing: run 'Install system programs' first.")
        return "failed"
    out = capture(["vulkaninfo", "--summary"])
    names = [l.split("=", 1)[1].strip() for l in out.splitlines() if "deviceName" in l and "=" in l]
    log("Vulkan devices: " + (", ".join(names) if names else "none"))
    real = [n for n in names if "llvmpipe" not in n.lower()]
    if not real:
        log("PROBLEM: no real GPU. Check Resizable BAR in the BIOS, the kernel (needs 6.14+), and the guide's step 3.")
        return "failed"
    if not any("intel" in n.lower() or "arc" in n.lower() or "b70" in n.lower() for n in real):
        log("WARNING: the name does not look like an Intel Arc card. Check it is the card you expect.")
        return "warn"
    return "ok"


def step_python(_p) -> str:
    if not PY.exists() and run([sys.executable, "-m", "venv", ROOT / ".venv"]):
        log("venv failed: run 'Install system programs' first (it installs python3-venv).")
        return "failed"
    if run([PY, "-m", "pip", "install", "--upgrade", "pip"]):
        return "failed"
    if run([PY, "-m", "pip", "install", "--require-hashes", "-r", ROOT / "requirements.lock"]):
        log("The locked list failed (made on Windows). Using requirements.txt instead.")
        if run([PY, "-m", "pip", "install", "-r", ROOT / "requirements.txt"]):
            return "failed"
    return "ok"


def step_binaries(_p) -> str:
    if not PY.exists():
        log("Run 'Install the Python parts' first.")
        return "failed"
    code = run([PY, ROOT / "scripts" / "download_models.py", "--binaries", "--backend", "vulkan", "--bin-dir", HOME])
    return "ok" if code == 0 else "failed"


def step_models(p) -> str:
    if not PY.exists():
        log("Run 'Install the Python parts' first.")
        return "failed"
    groups = [g for g in p.get("groups", []) if g in GROUPS]
    if not groups:
        log("Tick at least one group.")
        return "failed"
    code = run(models_cmd(groups, safe_path(p.get("models_dir"), "~/Models")))
    return "ok" if code == 0 else "failed"


def step_whisper(_p) -> str:
    if as_root(f"apt-get install -y {WHISPER_PACKAGES}"):
        return "failed"
    src = HOME / "whisper.cpp"
    if not src.exists() and run(["git", "clone", "https://github.com/ggml-org/whisper.cpp", src]):
        return "failed"
    if run(["cmake", "-S", src, "-B", src / "build", "-DGGML_VULKAN=1", "-DBUILD_SHARED_LIBS=OFF"]):
        return "failed"
    if run(["cmake", "--build", src / "build", "-j", "--config", "Release"]):
        return "failed"
    dest = HOME / "whisper-vulkan"
    shutil.copytree(src / "build" / "bin", dest, dirs_exist_ok=True)
    log(f"Copied the programs to {dest}")
    return "ok" if (dest / "whisper-server").exists() else "failed"


def step_config(p) -> str:
    if not PY.exists():
        log("Run 'Install the Python parts' first.")
        return "failed"
    code = run([PY, ROOT / "scripts" / "download_models.py", "--binaries", "--backend", "vulkan", "--bin-dir", HOME,
                "--models-dir", safe_path(p.get("models_dir"), "~/Models"), "--write-config"])
    cfg = ROOT / "config" / "app.json"
    left = [f"{i}: {l.strip()}" for i, l in enumerate(cfg.read_text(encoding="utf-8").splitlines(), 1) if "E:" in l]
    if left:
        log("Windows paths still in config/app.json (use 'Open settings file' and change them):")
        for l in left:
            log("  " + l)
    return "ok" if code == 0 and not left else ("warn" if code == 0 else "failed")


def step_folders(p) -> str:
    d = safe_path(p.get("common_dir"), "~/common")
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    log(f"Created {d} (only your user can read it)")
    return "ok"


def step_keyring(_p) -> str:
    if not PY.exists():
        log("Run 'Install the Python parts' first.")
        return "failed"
    out = capture(["dbus-run-session", "--", PY, "-c", "import keyring; print(keyring.get_keyring())"])
    log(out.strip() or "no answer: is dbus-run-session installed?")
    if "SecretService" in out:
        return "ok"
    log("PROBLEM: expected SecretService. Install gnome-keyring (system programs step). Never install keyrings.alt.")
    return "failed"


def step_start(p) -> str:
    if not PY.exists():
        log("Run 'Install the Python parts' first.")
        return "failed"
    if SERVER["proc"] and SERVER["proc"].poll() is None:
        log("The server is already running (use Stop server first).")
        return "warn"
    pw = p.get("keyring_password") or ""
    if len(pw) < 8:
        log("Choose a keyring password of at least 8 characters and keep it in your password manager.")
        return "failed"
    port = 8000
    lan = bool(p.get("lan"))
    args = [str(PY), "server_manager.py", "--port", str(port)] + (["--host", "0.0.0.0"] if lan else [])
    env = dict(os.environ)
    if p.get("admin_user") and p.get("admin_password"):
        env["A770_BOOTSTRAP_USERNAME"], env["A770_BOOTSTRAP_PASSWORD"] = p["admin_user"], p["admin_password"]
    (ROOT / "logs").mkdir(exist_ok=True)
    out = open(ROOT / "logs" / "server_gui.log", "ab")
    # the daemon reads the password from stdin; the server runs inside the same D-Bus session
    inner = "gnome-keyring-daemon --unlock --components=secrets >/dev/null && exec \"$@\""
    log("$ dbus-run-session -- sh -c 'gnome-keyring-daemon --unlock ... && exec " + " ".join(args) + "'   (password hidden)")
    proc = subprocess.Popen(["dbus-run-session", "--", "sh", "-c", inner, "sh", *args], cwd=str(ROOT), env=env, text=True,
                            stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        proc.stdin.write(pw)
        proc.stdin.close()
    except OSError:
        pass
    SERVER["proc"] = proc
    for _ in range(60):
        time.sleep(1)
        if proc.poll() is not None:
            log(f"The server stopped (code {proc.returncode}). See logs/server_gui.log")
            return "failed"
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            break
        except OSError:
            continue
    else:
        log("The server is still starting. Look at logs/server_gui.log, then use 'Open the app'.")
        return "warn"
    log(f"The server is running: http://127.0.0.1:{port}  (log: logs/server_gui.log)")
    locked = 0
    for f in ROOT.glob("*.db*"):      # the application does not set file permissions: keep the databases private
        try:
            f.chmod(0o600)
            locked += 1
        except OSError:
            pass
    log(f"Made {locked} database file(s) private (mode 600). Roles and permissions were created on this first start.")
    return "ok"


STEP_FUNCS = {s[0]: globals()["step_" + s[0]] for s in STEPS}


def run_job(step: str, params: dict) -> None:
    RUNNING["cancel"] = False
    STATUS[step] = "running"
    log(f"\n=== {dict((s[0], s[1]) for s in STEPS)[step]} ===")
    try:
        result = STEP_FUNCS[step](params)
    except Exception as e:  # keep the page alive whatever happens
        log(f"error: {e}")
        result = "failed"
    if RUNNING["cancel"]:
        result = "failed"
    STATUS[step] = result
    log({"ok": "-> done", "warn": "-> done, with a warning above", "failed": "-> FAILED, read the lines above"}[result])
    RUNNING["step"] = None


# ---------------------------------------------------------------- web page
PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Install the AI server</title><style>
:root{--pine:#0e6b5c;--ink:#14212b;--mut:#566673;--line:#cdd8db;--bg:#eaeff0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 "Segoe UI",system-ui,sans-serif}
header{background:#0d1a21;color:#dcebe6;padding:22px 24px}header h1{margin:0 auto;max-width:1100px;font:600 26px Georgia,serif}
header p{margin:6px auto 0;max-width:1100px;color:#b4cbc5}
.wrap{max-width:1100px;margin:0 auto;padding:20px 24px;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;align-items:start}
.step{background:#f8fafa;border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin-bottom:12px}
.step h2{margin:0;font:600 17px Georgia,serif;display:flex;gap:10px;align-items:center}
.step p{margin:4px 0 8px;color:var(--mut);font-size:14px}.n{color:var(--pine)}
.badge{margin-left:auto;font:600 12px "Segoe UI",sans-serif;padding:1px 9px;border-radius:99px;background:#e5ecee;color:var(--mut)}
.badge.ok{background:#d8eee9;color:#0a4d42}.badge.failed{background:#f8e0db;color:#a63a2b}.badge.warn{background:#fbefd4;color:#7a4d05}.badge.running{background:#e6eef1;color:#2d5666}
button{font:600 14px "Segoe UI",sans-serif;background:var(--pine);color:#fff;border:0;border-radius:8px;padding:7px 16px;cursor:pointer}
button:disabled{background:#9fb4b0;cursor:not-allowed}button.sec{background:#e5ecee;color:var(--ink)}
button:focus-visible,input:focus-visible{outline:3px solid #b7740a;outline-offset:2px}
label{display:block;font-size:14px;margin:6px 0}input[type=text],input[type=password]{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;font:14px monospace}
.chk{display:inline-block;margin-right:14px}
#logbox{position:sticky;top:12px;background:#0d1a21;color:#dcebe6;border-radius:12px;padding:12px 14px;height:calc(100vh - 40px);overflow:auto;font:13px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.bar{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
@media(max-width:860px){.wrap{grid-template-columns:1fr}#logbox{position:static;height:340px}}
</style></head><body>
<header><h1>Install the AI server</h1><p>Click the steps from top to bottom. The same commands as in INSTALL_LINUX.html, run for you. Lines on the right show what is happening.</p></header>
<div class="wrap"><div id="steps"></div><div><div id="logbox" role="log" aria-live="polite"></div>
<div class="bar"><button class="sec" id="stop">Cancel current step</button><button class="sec" id="srvstop">Stop server</button>
<button class="sec" id="oc">Open settings file</button><button class="sec" id="oa">Open the app</button><button class="sec" id="quit">Quit installer</button></div></div></div>
<script>
const TOKEN="__TOKEN__";let state={steps:[],status:{},running:null},since=0;
const H={"X-Token":TOKEN,"Content-Type":"application/json"};
const post=(u,b)=>fetch(u,{method:"POST",headers:H,body:JSON.stringify(b||{})}).then(r=>r.json());
function field(id,html){return html}
function form(s){
 if(s==="models")return `<div>`+[["routers","Laya router (CPU)",1],["embedder","Embedder, 140 MB",1],["whisper","Whisper large-v3, 3.1 GB",1],["tts","Spoken replies, 165 MB",1],["vision","Image reader, 3.2 GB",0],["routing","Executor Ornith 9B, 5.8 GB",0],["image","Image maker, 6.7 GB",0],["voice","Whisper turbo + large-v3 + voices, 3.8 GB",0]].map(g=>`<label class="chk"><input type="checkbox" data-g="${g[0]}" ${g[2]?"checked":""}> ${g[1]}</label>`).join("")+`</div><label>Models folder<input type="text" id="models_dir" value="~/Models"></label>`;
 if(s==="config")return `<label>Models folder (same as above)<input type="text" id="models_dir_c" value="~/Models"></label>`;
 if(s==="folders")return `<label>Data folder<input type="text" id="common_dir" value="~/common"></label>`;
 if(s==="start")return `<label>Keyring password (new; keep it in your password manager)<input type="password" id="kp" autocomplete="new-password"></label>
 <label>Admin user name (first start only, leave empty if it already exists)<input type="text" id="au"></label>
 <label>Admin password (first start only)<input type="password" id="ap" autocomplete="new-password"></label>
 <label><input type="checkbox" id="lan"> Let other computers on the network reach it</label>`;
 return "";}
function params(s){
 if(s==="models")return{groups:[...document.querySelectorAll("[data-g]:checked")].map(e=>e.dataset.g),models_dir:document.getElementById("models_dir").value};
 if(s==="config")return{models_dir:document.getElementById("models_dir_c").value};
 if(s==="folders")return{common_dir:document.getElementById("common_dir").value};
 if(s==="start")return{keyring_password:kp.value,admin_user:au.value,admin_password:ap.value,lan:lan.checked};
 return{};}
function draw(){
 const box=document.getElementById("steps");
 if(box.dataset.built!==String(state.steps.length)){
  box.dataset.built=state.steps.length;
  box.innerHTML=state.steps.map((s,i)=>`<section class="step" id="s_${s.id}"><h2><span class="n">${i+1}</span>${s.title}<span class="badge" id="b_${s.id}">not run</span></h2><p>${s.help}</p>${form(s.id)}<button data-run="${s.id}">Run this step</button></section>`).join("");
  box.querySelectorAll("[data-run]").forEach(b=>b.onclick=async()=>{const r=await post("/api/run",{step:b.dataset.run,params:params(b.dataset.run)});if(r.error)alert(r.error)});}
 state.steps.forEach(s=>{const st=state.status[s.id]||"idle",b=document.getElementById("b_"+s.id);
  b.className="badge "+st;b.textContent={idle:"not run",running:"running...",ok:"done",warn:"done, check log",failed:"failed"}[st];});
 document.querySelectorAll("[data-run]").forEach(b=>b.disabled=!!state.running);}
async function tick(){
 try{const r=await fetch("/api/state?since="+since,{headers:H}).then(r=>r.json());
  state=r;draw();if(r.lines.length){const lb=document.getElementById("logbox"),near=lb.scrollHeight-lb.scrollTop-lb.clientHeight<60;
   lb.textContent+=r.lines.join("\n")+"\n";if(near)lb.scrollTop=lb.scrollHeight;}since=r.next;}catch(e){}
 setTimeout(tick,800);}
document.getElementById("stop").onclick=()=>post("/api/cancel");
document.getElementById("srvstop").onclick=()=>post("/api/action",{action:"stop_server"});
document.getElementById("oc").onclick=()=>post("/api/action",{action:"open_config"});
document.getElementById("oa").onclick=()=>post("/api/action",{action:"open_app"});
document.getElementById("quit").onclick=()=>{post("/api/action",{action:"quit"});document.body.innerHTML="<p style='padding:30px'>Installer closed. You can close this tab.</p>"};
tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "installer"

    def log_message(self, *a):  # keep the terminal quiet
        pass

    def _ok_host(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _authed(self) -> bool:
        return self._ok_host() and secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def _send(self, code: int, body, ctype="application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path)
        if not self._ok_host():
            return self._send(403, {"error": "forbidden"})
        if path.path == "/" and path.query == "t=" + TOKEN:
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html")
        if path.path == "/api/state" and self._authed():
            since = int(dict(q.split("=") for q in path.query.split("&") if "=" in q).get("since", "0") or 0)
            with LOCK_LOG:
                lines, nxt = LOG[since:], len(LOG)
            return self._send(200, {"steps": [{"id": s[0], "title": s[1], "help": s[2]} for s in STEPS],
                                    "status": STATUS, "running": RUNNING["step"], "lines": lines, "next": nxt})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 20000)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad request"})
        path = urlparse(self.path).path
        if path == "/api/run":
            step = body.get("step")
            if step not in STEP_IDS:
                return self._send(400, {"error": "unknown step"})
            if RUNNING["step"]:
                return self._send(409, {"error": "another step is still running"})
            RUNNING["step"] = step
            threading.Thread(target=run_job, args=(step, body.get("params") or {}), daemon=True).start()
            return self._send(200, {"ok": True})
        if path == "/api/cancel":
            RUNNING["cancel"] = True
            p = RUNNING["proc"]
            if p and p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
            return self._send(200, {"ok": True})
        if path == "/api/action":
            act = body.get("action")
            if act == "open_config":
                subprocess.Popen(["xdg-open", str(ROOT / "config" / "app.json")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif act == "open_app":
                webbrowser.open("http://127.0.0.1:8000/")
            elif act == "stop_server":
                p = SERVER["proc"]
                if p and p.poll() is None:
                    os.killpg(p.pid, signal.SIGTERM)
                    log("Server stopped.")
                else:
                    log("The server was not started from here.")
            elif act == "quit":
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})


def main() -> int:
    if os.name != "posix" or sys.platform == "darwin":
        print("This installer is for the Linux server (Ubuntu).")
        return 1
    if os.geteuid() == 0:
        print("Run this as your normal user, not root.")
        return 1
    httpd = None
    for port in range(8765, 8785):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        print("No free port between 8765 and 8784.")
        return 1
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?t={TOKEN}"
    print(f"Installer page: {url}\nPress Ctrl+C here to quit.")
    log(f"Application folder: {ROOT}")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
