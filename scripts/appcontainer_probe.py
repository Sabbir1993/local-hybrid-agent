"""scripts/appcontainer_probe.py - P5 spike probe: what does a Windows AppContainer
child actually do on this machine, and at what latency cost?

Exercises core/sandbox/appcontainer.py without touching the app in any way:
- a real python child runs inside a zero-capability container,
- with READ grants on the interpreter dirs + the workspace dir, WRITE on a
  one-off scratch dir (grants appended, never replaced),
- the network control (a TCP connect against a LIVE host listener) must fail
  with WFP's access-denied, proving the deny is real (not just "port closed"),
- an un-granted dir must refuse the child, proving grants are the only way in,
- a spawn-latency A/B: the same tiny python run on host vs container.

Also serves offline CI: runs except the windows-only module parts skip.

Usage: python scripts/appcontainer_probe.py
Exit code: the number of FAILED gauntlet cases.
"""
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.sandbox import appcontainer as ac

MARKER = "hello-from-appcontainer"
TINY = "print(1+1)"


def host_run(py, code, cwd) -> tuple:
    r = subprocess.run([py, "-c", code], capture_output=True, text=True, cwd=cwd,
                       timeout=60, creationflags=0x08000000)   # CREATE_NO_WINDOW
    return r.returncode, (r.stdout or ""), (r.stderr or "")


def main() -> int:
    if not ac.is_windows():
        print("appcontainer probe is windows-only; nothing to measure here")
        return 0
    ws = str(Path(__file__).resolve().parents[1])
    py = sys.executable
    interpreter_dirs = sorted({sys.prefix, sys.base_prefix})
    name = ac.new_profile_name("probe")
    scratch = Path(ac.scratch_dir("probe"))                  # granted for write
    denied = Path(ac.scratch_dir("probe-denied"))            # sibling, never granted
    cases = []                                 # (label, ok, note)
    try:
        try:
            ac.create_profile(name)                      # zero capabilities
            cases.append(("profile created (zero capabilities)", True,
                          ac.derive_sid_string(name)))
        except ac.AppContainerError as e:
            cases.append(("profile created (zero capabilities)", False, str(e)))
            return _report(cases)

        ac.grant_dirs(name,
                      read_dirs=[str(p) for p in interpreter_dirs] + [ws],
                      write_dirs=[str(scratch)])
        cases.append(("grants appended (interpreter read, workspace read, scratch write)",
                      True, "read:" + ";".join(interpreter_dirs + [ws])))

        r1 = ac.run([py, "-c", f"import sys;open('case1.txt','w').write(sys.platform);print('{MARKER}')"],
                    profile=name, log_dir=str(scratch), cwd=str(scratch),
                    env_extra={"OPENCODE_TEST": "1"})
        wrote = (scratch / "case1.txt").read_text(encoding="utf-8") \
            if (scratch / "case1.txt").exists() else "<missing>"
        cases.append(("python child runs in container + writes granted scratch",
                      r1["rc"] == 0 and MARKER in r1["stdout"] and wrote == "win32",
                      f"rc={r1['rc']} file={wrote!r} ms={r1['ms']}"))

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        src = (f"import socket;s=socket.socket();s.settimeout(4);"
               f"s.connect(('127.0.0.1',{port}));print('seen-by-host')")
        rc_h, out_h, _ = host_run(py, src, scratch)
        cases.append(("host child connects to a live listener", rc_h == 0 and "seen-by-host" in out_h,
                      f"rc={rc_h}"))
        r2 = ac.run([py, "-c", src], profile=name, log_dir=str(scratch), cwd=str(scratch))
        # On current Windows builds WFP DROPS the contained child's packets
        # (TimeoutError) instead of returning 10013; either outcome is deny,
        # as long as the child never printed the connect-success marker.
        cases.append(("container child blocked from the network (WFP)",
                      rc_h == 0 and r2["rc"] != 0 and "seen-by-host" not in r2["stdout"],
                      f"rc={r2['rc']} err={r2['stderr'][:70]!r}"))

        r3 = ac.run([py, "-c",
                     f"import os;names=os.listdir({ws!r});print(len(names))"],
                    profile=name, log_dir=str(scratch), cwd=str(scratch))
        # Read grant on the workspace DIR lets the child list it; note that
        # git-style READS of PRE-EXISTING FILES inside need file-level ACEs
        # (a dir ACE does not refresh inheritance on files created earlier),
        # which this spike does not attempt.
        cases.append(("python child lists the granted workspace dir",
                      r3["rc"] == 0 and r3["stdout"].strip().isdigit()
                      and int(r3["stdout"]) > 10,
                      f"rc={r3['rc']} out={r3['stdout'][:30]!r}"))

        r4_target = denied / "must-not-write.txt"
        r4 = ac.run([py, "-c", f"open({str(r4_target)!r},'w').write('leak')"],
                    profile=name, log_dir=str(scratch), cwd=str(scratch))
        cases.append(("un-granted dir denies the child",
                      not r4_target.exists() and r4["rc"] != 0,
                      f"rc={r4['rc']} wrote={r4_target.exists()} err={r4['stderr'][:60]!r}"))

        ms5 = {"host": [], "container": []}
        for _ in range(3):
            t0 = time.perf_counter()
            host_run(py, TINY, scratch)
            ms5["host"].append(int((time.perf_counter() - t0) * 1000))
            ms5["container"].append(
                ac.run([py, "-c", TINY], profile=name, log_dir=str(scratch),
                       cwd=str(scratch))["ms"])
        avg5 = {k: sum(v) // max(len(v), 1) for k, v in ms5.items()}
        cases.append(("python spawn latency host vs container", True,
                      f"host={avg5['host']}ms container={avg5['container']}ms (3-round avg)"))
        return _report(cases)
    finally:
        try:
            shutil.rmtree(scratch, ignore_errors=True)
            shutil.rmtree(denied, ignore_errors=True)
        finally:
            ac.delete_profile(name)


def _report(cases) -> int:
    failures = 0
    for label, ok, note in cases:
        print(f"{'OK  ' if ok else 'FAIL'} | {label}: {note}")
        failures += 0 if ok else 1
    print(f"\nprobe complete: {len(cases) - failures}/{len(cases)} cases OK")
    return failures


if __name__ == "__main__":
    sys.exit(main())
