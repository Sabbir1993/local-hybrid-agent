"""Native folder-picker dialog helper (tkinter / PowerShell fallback)."""

import os
import subprocess
import sys


def _ask_directory_native(initial_dir: str = "") -> str:
    init_dir = (initial_dir or "").strip()
    if not init_dir or not os.path.isdir(init_dir):
        if os.path.isdir("E:\\AI"):
            init_dir = "E:\\AI"
        else:
            init_dir = os.path.expanduser("~")

    try:
        py_script = f"""
import sys, os
try:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    root.lift()
    root.focus_force()
    res = filedialog.askdirectory(title='Select Local Workspace Directory', initialdir={repr(init_dir)}, mustexist=False)
    root.destroy()
    if res:
        print(os.path.normpath(res))
except Exception:
    sys.exit(1)
"""
        proc = subprocess.run(
            [sys.executable, "-c", py_script],
            capture_output=True,
            text=True,
            timeout=120
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return os.path.normpath(proc.stdout.strip())
    except Exception as e:
        print(f"[server_manager] tkinter subprocess failed ({e}), trying PowerShell fallback", file=sys.stderr)

    if os.name != "nt":  # WinForms fallback is Windows-only; headless Linux types the path
        return ""
    try:
        escaped_init = init_dir.replace("'", "''")
        ps_cmd = (
            "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$f.Description = 'Select Local Workspace Directory'; "
            "$f.ShowNewFolderButton = $true; "
            f"$f.SelectedPath = '{escaped_init}'; "
            "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Write-Output $f.SelectedPath }"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=120
        )
        if res.returncode == 0 and res.stdout.strip():
            return os.path.normpath(res.stdout.strip())
    except Exception as e:
        print(f"[server_manager] PowerShell folder dialog failed: {e}", file=sys.stderr)

    return ""
