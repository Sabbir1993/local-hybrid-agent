"""core/sandbox/appcontainer.py - SPIKE (P5): one process per call in a Windows AppContainer.

Code-only spike per the Phase-4 re-scope: this module is NOT wired into
run_shell / run_python / the agent loop, and there is no config flag yet.
scripts/appcontainer_probe.py is the only intended consumer until the spike
proves its value on this machine. The measured fact it targets: the sandbox
tool is idle (usage.db: run_sandbox_python was never called once, so the WSL2
runner sits unexercised), while run_shell (n=541) and run_python (n=412)
execute directly on the host with only the ask-first card as protection.

What an AppContainer child gets:
- a derived low-integrity profile SID (pure formula below; a test pins the
  pure form against the native API on Windows),
- NO capabilities: no network (WFP denies the syscall - a negative-control
  test pins the 10013/WSAEACCES outcome),
- a normal process otherwise: real paths, real python.

The child touches only what an explicit DACL ACE grants (the scratch dir it
writes into, the interpreter dir it reads from); everything else fails with
ERROR_ACCESS_DENIED, and the caller sees a failed call, not a host leak.

Rollback: delete this file plus its probe and tests; nothing else changes.
"""
import hashlib
import os
import struct
import tempfile
import time
from pathlib import Path

try:
    import ctypes
    import ctypes.wintypes as wt
except Exception:                       # pragma: no cover - cpython always ships ctypes
    ctypes = None
    wt = None

# --- pure, platform-independent core ---------------------------------------

# SPECIFIC masks (not GENERIC_*): generic bits get auto-remapped at inherit
# time and the FILE-mapped result dropped FILE_READ_DATA (0x1), which breaks
# every image load with 0xC0000022.  Specific bits pass through unchanged.
ACCESS_MASKS = {"read": 0x001200A9,       # FILE_GENERIC_READ|FILE_EXECUTE
                "write": 0x001F01FF,      # FILE_ALL_ACCESS (read+write+exec)
                "all": 0x001F01FF}
ACE_DIR_INHERIT = 0x00000003             # CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE


def is_windows() -> bool:
    return os.name == "nt"


def profile_name_ok(name: str) -> bool:
    """CreateAppContainerProfile is picky: plain ascii, no spaces, <=64 chars."""
    return bool(name) and len(name) <= 64 and all(c.isalnum() or c in "._-" for c in name)


def derive_sid_string(name: str) -> str:
    """The pure AppContainer-SID derivation, platform independent.

    Pinned on this machine against the native DeriveAppContainerSidFromApp
    ContainerName: SHA-256 of the UTF-16LE name, truncated to 28 bytes,
    rendered as SEVEN little-endian dwords under S-1-15-2-.
    """
    if not profile_name_ok(name):
        return ""
    d = hashlib.sha256(name.encode("utf-16-le")).digest()[:28]
    groups = [int.from_bytes(d[i:i + 4], "little") for i in range(0, 28, 4)]
    return "S-1-15-2-" + "-".join(map(str, groups))


def quote_arg(s: str) -> str:
    """CommandLineToArgvW-compatible quoting for one argv element."""
    if s and not any(c in ' \t\n\v"' for c in s):
        return s
    out, slashes = '"', 0
    for c in s:
        if c == "\\":
            slashes += 1
        elif c == '"':
            out += "\\" * (2 * slashes + 1) + '"'
            slashes = 0
        else:
            out += "\\" * slashes + c
            slashes = 0
    out += "\\" * slashes
    return out + '"'


def command_line(argv) -> str:
    """An argv list joined into the CreateProcessW lpCommandLine string."""
    return " ".join(quote_arg(a) for a in argv)


def _sid_from_string(sid_str: str) -> bytes:
    """Binary SID bytes for an S-1-15-2-... string; ACE building needs raw bytes."""
    parts = sid_str.split("-")
    if (len(parts) != 11 or parts[0] != "S" or parts[1] != "1" or parts[2] != "15"
            or parts[3] != "2"
            or not all(p.isdigit() for p in parts[4:])):
        raise ValueError(f"not an appcontainer SID string: {sid_str!r}")
    subs = [2] + [int(g) for g in parts[4:]]
    return struct.pack("<BB", 1, len(subs)) + struct.pack(">Q", 15)[2:] \
        + b"".join(struct.pack("<I", s) for s in subs)


def _ace_bytes(sid_binary: bytes, mask: int, flags: int = ACE_DIR_INHERIT) -> bytes:
    """Raw ACCESS_ALLOWED_ACE bytes: type 0,inherit flags, mask, SID.

    The AceSize field counts the header plus the mask plus the SID (8+n), and
    must match the buffer passed to AddAce exactly or the API rejects the ACE
    with ERROR_INVALID_PARAMETER.
    """
    return struct.pack("<BBH", 0, flags, 8 + len(sid_binary)) \
        + struct.pack("<I", mask) + sid_binary


class AppContainerError(Exception):
    """Every failure of this spike surfaces with its context, never tracebacked up."""


# --- win32 binding; everything below is inert on non-Windows platforms ------

if ctypes is not None and os.name == "nt":
    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 131081   # 0x20009: value + proc-input flag (0x20000)
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    CREATE_NO_WINDOW = 0x08000000
    HANDLE_FLAG_INHERIT = 0x00000001
    STARTF_USESTDHANDLES = 0x00000100
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_ALWAYS = 4
    DACL_SECURITY_INFORMATION = 0x00000004
    UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
    OWNER_SECURITY_INFORMATION = 0x00000001
    SE_FILE_OBJECT = 1
    ACL_REVISION_DS = 4
    ACL_SIZE_INFORMATION_CLASS = 2
    WAIT_TIMEOUT = 0x00000102

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
            ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
            ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
            ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
            ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
            ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
            ("lpReserved2", wt.LPVOID),
            ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
            ("hStdError", wt.HANDLE)]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW),
                    ("lpAttributeList", ctypes.c_void_p)]

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [("AppContainerSid", ctypes.c_void_p),
                    ("CapabilityCount", wt.DWORD),
                    ("Capabilities", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                    ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [("AceCount", wt.DWORD), ("AclBytesInUse", wt.DWORD),
                    ("AclBytesFree", wt.DWORD)]

    _u = ctypes.WinDLL("userenv", use_last_error=True)
    _a = ctypes.WinDLL("advapi32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32", use_last_error=True)

    _u.CreateAppContainerProfile.argtypes = (
        wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR,
        ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p))
    _u.CreateAppContainerProfile.restype = ctypes.HRESULT
    _u.DeleteAppContainerProfile.argtypes = (wt.LPCWSTR,)
    _u.DeleteAppContainerProfile.restype = ctypes.HRESULT
    _u.DeriveAppContainerSidFromAppContainerName.argtypes = (
        wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
    _u.DeriveAppContainerSidFromAppContainerName.restype = ctypes.HRESULT

    _a.ConvertStringSidToSidW.argtypes = (wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
    _a.ConvertStringSidToSidW.restype = wt.BOOL
    _a.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
    _a.ConvertSidToStringSidW.restype = wt.BOOL
    _a.FreeSid.argtypes = (ctypes.c_void_p,)
    _a.FreeSid.restype = ctypes.c_void_p
    _a.GetNamedSecurityInfoW.argtypes = (
        wt.LPWSTR, wt.DWORD, wt.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p))
    _a.GetNamedSecurityInfoW.restype = wt.DWORD
    _a.SetNamedSecurityInfoW.argtypes = (
        wt.LPWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p)
    _a.SetNamedSecurityInfoW.restype = wt.DWORD
    _a.InitializeAcl.argtypes = (ctypes.c_void_p, wt.DWORD, wt.DWORD)
    _a.InitializeAcl.restype = wt.BOOL
    _a.GetAclInformation.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD)
    _a.GetAclInformation.restype = wt.BOOL
    _a.GetAce.argtypes = (ctypes.c_void_p, wt.DWORD, ctypes.POINTER(ctypes.c_void_p))
    _a.GetAce.restype = wt.BOOL
    _a.AddAce.argtypes = (ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD)
    _a.AddAce.restype = wt.BOOL

    _k.CreateProcessW.argtypes = (
        wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL,
        wt.DWORD, ctypes.c_void_p, wt.LPWSTR,
        ctypes.c_void_p, ctypes.c_void_p)
    _k.CreateProcessW.restype = wt.BOOL
    _k.InitializeProcThreadAttributeList.argtypes = (
        ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t))
    _k.InitializeProcThreadAttributeList.restype = wt.BOOL
    _k.UpdateProcThreadAttribute.argtypes = (
        ctypes.c_void_p, wt.DWORD, wt.DWORD,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
    _k.UpdateProcThreadAttribute.restype = wt.BOOL
    _k.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
    _k.DeleteProcThreadAttributeList.restype = None
    _k.SetHandleInformation.argtypes = (wt.HANDLE, wt.DWORD, wt.DWORD)
    _k.SetHandleInformation.restype = wt.BOOL
    _k.CreateFileW.argtypes = (wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                               wt.DWORD, wt.DWORD, wt.HANDLE)
    _k.CreateFileW.restype = wt.HANDLE
    _k.WaitForSingleObject.argtypes = (wt.HANDLE, wt.DWORD)
    _k.WaitForSingleObject.restype = wt.DWORD
    _k.GetExitCodeProcess.argtypes = (wt.HANDLE, ctypes.POINTER(wt.DWORD))
    _k.GetExitCodeProcess.restype = wt.BOOL
    _k.TerminateProcess.argtypes = (wt.HANDLE, wt.UINT)
    _k.TerminateProcess.restype = wt.BOOL
    _k.CloseHandle.argtypes = (wt.HANDLE,)
    _k.CloseHandle.restype = wt.BOOL
    _k.LocalFree.argtypes = (wt.HLOCAL,)
    _k.LocalFree.restype = ctypes.c_void_p
else:
    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = None


def _hr_fail(what: str, hr: int) -> None:
    raise AppContainerError(f"{what} failed: HRESULT={hr & 0xFFFFFFFF:#010x}")


def _rc_fail(what: str, rc: int) -> None:
    raise AppContainerError(f"{what} failed: rc={rc & 0xFFFFFFFF:#010x}")


def native_sid(name: str) -> str:
    """The authoritative OS derivation; tests pin the pure form against it."""
    if not _win():
        raise AppContainerError("appcontainer SIDs are windows-only")
    psid = ctypes.c_void_p()
    hr = _u.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(psid))
    if hr != 0:
        _hr_fail("DeriveAppContainerSid", hr)
    s = ctypes.c_wchar_p()
    if not _a.ConvertSidToStringSidW(psid, ctypes.byref(s)):
        _a.FreeSid(psid)
        raise AppContainerError("ConvertSidToStringSidW failed")
    text = s.value
    _k.LocalFree(s)
    _a.FreeSid(psid)
    return text


def create_profile(name: str) -> str:
    """Create the container profile (idempotent if it already exists). Zero
    capabilities means: no internet, no private network, no devices."""
    if not _win():
        raise AppContainerError("appcontainer profiles are windows-only")
    psid = ctypes.c_void_p()
    hr = _u.CreateAppContainerProfile(
        name, "OpenCode isolation spike", "agent sandbox isolation spike (P5)",
        None, 0, ctypes.byref(psid))
    if hr != 0 and (hr & 0xFFFFFFFF) == 0x800700B7:   # ERROR_ALREADY_EXISTS: fine
        return derive_sid_string(name)
    if hr != 0:
        _hr_fail("CreateAppContainerProfile", hr)
    _a.FreeSid(psid)
    return derive_sid_string(name)


def delete_profile(name: str) -> bool:
    if not _win():
        return True
    return _u.DeleteAppContainerProfile(name) == 0


def _win() -> bool:
    return ctypes is not None and os.name == "nt"


# --- grants: APPEND one ACE per directory, never replace the DACL -----------

def _dacl_aces(p_dacl) -> list:
    info = ACL_SIZE_INFORMATION()
    if not _a.GetAclInformation(p_dacl, ctypes.byref(info),
                                ctypes.sizeof(info), ACL_SIZE_INFORMATION_CLASS):
        raise AppContainerError("GetAclInformation failed")
    aces = []
    for i in range(info.AceCount):
        p_ace = ctypes.c_void_p()
        if not _a.GetAce(p_dacl, i, ctypes.byref(p_ace)):
            raise AppContainerError(f"GetAce({i}) failed")
        acq = ctypes.cast(p_ace, ctypes.POINTER(ctypes.c_ubyte * 4)).contents
        ace_len = acq[2] | (acq[3] << 8)          # ACCESS_ALLOWED_ACE.AceSize (uint16 LE)
        aces.append(bytes(bytearray(
            ctypes.cast(p_ace, ctypes.POINTER(ctypes.c_ubyte * ace_len)).contents)))
    return aces


FILES_TRAVERSE_MASK = 0x00000020     # FILE_TRAVERSE on a directory object


def _self_sid_bin() -> bytes:
    """The CALLING user's SID (the low-integrity child's user is the same); an
    object the user does not own is one we must not try to DACL."""
    tok = ctypes.c_void_p()
    if not _a.OpenProcessToken(_k.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):
        return b""
    try:
        need = ctypes.c_ulong(0)
        # TokenInformationClass 1 = TokenUser; size query first
        _a.GetTokenInformation(tok, 1, None, 0, ctypes.byref(need))
        buf = ctypes.create_string_buffer(need.value)
        if not _a.GetTokenInformation(tok, 1, buf, need.value, ctypes.byref(need)):
            return b""
        # TOKEN_USER = { PSID user; DWORD attributes; } - the PSID is the
        # first pointer in the buffer already copied out of the call.
        p_sid = ctypes.c_void_p.from_buffer_copy(buf[:8])
        n = _a.GetLengthSid(p_sid)
        return ctypes.string_at(p_sid.value, n)
    finally:
        _k.CloseHandle(tok)


def _append_dir_ace(path: Path, sid_binary: bytes, mask: int, flags: int = ACE_DIR_INHERIT) -> None:
    p_sd, p_dacl = ctypes.c_void_p(), ctypes.c_void_p()
    rc = _a.GetNamedSecurityInfoW(
        str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
        None, None, ctypes.byref(p_dacl), None, ctypes.byref(p_sd))
    if rc != 0:
        _rc_fail("GetNamedSecurityInfoW", rc)
    try:
        keep = _dacl_aces(p_dacl)
        for ace in keep:                          # idempotency: skip if our SID present
            if len(ace) >= 8 and ace[4:4 + len(sid_binary)] == sid_binary:
                stored = int.from_bytes(ace[4:8], "little")
                if stored & mask == mask:
                    return
        new_ace = _ace_bytes(sid_binary, mask, flags=flags)
        size = 8 + sum(len(a) for a in keep) + len(new_ace)   # 8-byte ACL header
        buf = ctypes.create_string_buffer(size)
        if not _a.InitializeAcl(buf, size, ACL_REVISION_DS):
            raise AppContainerError("InitializeAcl failed")
        for i, ace in enumerate(keep + [new_ace]):
            if not _a.AddAce(buf, ACL_REVISION_DS, i, ace, len(ace)):
                raise AppContainerError(f"AddAce({i}) failed")
        rc = _a.SetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | UNPROTECTED_DACL_SECURITY_INFORMATION,
            None, None, buf, None)
        if rc != 0:
            _rc_fail("SetNamedSecurityInfoW", rc)
    finally:
        if p_sd:
            _k.LocalFree(p_sd)


def _traverse_chain(path: Path):
    """Ancestors up to (and including) the volume root; a lowbox token needs
    FILE_TRAVERSE on every one of them to reach a deeper granted dir."""
    out = []
    cur = path.resolve()
    root = Path(cur.anchor)
    while cur != root:
        cur = cur.parent
        out.append(cur)
    return out


def _owner_sid(path: Path) -> bytes:
    """The object's owner SID, raw bytes; empty on failure."""
    p_owner = ctypes.c_void_p()
    p_sd = ctypes.c_void_p()
    rc = _a.GetNamedSecurityInfoW(
        str(path), SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION,
        ctypes.byref(p_owner), None, None, None, ctypes.byref(p_sd))
    if rc != 0 or not p_owner.value:
        if p_sd.value:
            _k.LocalFree(p_sd)
        return b""
    n = _a.GetLengthSid(p_owner)
    out = ctypes.string_at(p_owner.value, n)
    _k.LocalFree(p_sd)
    return out


def grant_dirs(profile: str, read_dirs=(), write_dirs=()) -> list:
    """READ on each read_dir, WRITE (implies read) on each write_dir.

    Only directories the probe or test itself created are ever granted; the
    DACL change appends one ACE and keeps every ACE that was already there
    (idempotent: a second call with the same mask is a no-op).  Ancestors get
    a FILE_TRAVERSE ACE aswell, but ONLY when the caller is the OWNER of the
    ancestor - a non-owned ancestor (C:\\, C:\\Users, System32...) is skipped
    with a note instead: the lowbox token's Everyone/Users groups cover those
    anyway, and trying to Set a DACL we cannot own wastes ~minutes.
    Returns the list of (ancestor, reason) skips for the caller to log.
    """
    if not _win():
        raise AppContainerError("grants are windows-only")
    sid = derive_sid_string(profile)
    sid_bin = _sid_from_string(sid)
    seen = set()
    denied = []
    for d in list(read_dirs) + list(write_dirs):
        for a in _traverse_chain(Path(d)):
            if str(a).lower() not in seen:
                seen.add(str(a).lower())
                owner = _owner_sid(a)
                if not owner or owner != _self_sid_bin():
                    # TRAVERSE on the ancestor dir itself ONLY - flags=0 so
                    # the ACE never inherits; with an inherit flag the DACL
                    # Set would re-propagate across the whole subtree.
                    denied.append((str(a), "not owned by the caller"))
                    continue
                try:
                    _append_dir_ace(a, sid_bin, FILES_TRAVERSE_MASK, flags=0)
                except AppContainerError as e:
                    denied.append((str(a), str(e)))
    for d in read_dirs:
        _append_dir_ace(Path(d), sid_bin, ACCESS_MASKS["read"])
    for d in write_dirs:
        _append_dir_ace(Path(d), sid_bin, ACCESS_MASKS["all"])
    return denied


# --- the launcher -----------------------------------------------------------

def _env_block(env_extra) -> ctypes.Array:
    env = dict(os.environ.items())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    text = "\0".join(f"{k}={v}" for k, v in env.items()) + "\0\0"
    return ctypes.create_string_buffer(text.encode("utf-16-le"))


def run(argv, *, profile: str, log_dir: str, cwd: str = None,
        timeout_s: float = 60, env_extra: dict = None) -> dict:
    """Spawn one process in the AppContainer of `profile`.

    The caller has created the profile and granted every directory the child
    needs (grant_dirs); the log_dir writes come from inherited file handles,
    whose access was decided by the parent process, not by the child.
    Returns {"rc", "stdout", "stderr", "timed_out", "ms"}.
    """
    if not _win():
        raise AppContainerError("appcontainer launch is windows-only")
    sid_ptr = ctypes.c_void_p()
    if not _a.ConvertStringSidToSidW(derive_sid_string(profile), ctypes.byref(sid_ptr)):
        raise AppContainerError("ConvertStringSidToSidW failed")

    log_out = Path(log_dir) / "appcontainer.out.log"
    log_err = Path(log_dir) / "appcontainer.err.log"
    for stale in (log_out, log_err):          # TRUNCATE: drop the previous spawn's bytes
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    stdhandle = _k.CreateFileW("NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                               None, OPEN_ALWAYS, 0, None)
    # Log handles are opened inheritable by US (full user token) so the access
    # decision was made before the child exists; the contained child writes to
    # the inherited handles without ever needing a directory grant of its own.
    rw = GENERIC_READ | GENERIC_WRITE
    out = _k.CreateFileW(str(log_out), rw, FILE_SHARE_READ, None, OPEN_ALWAYS, 0, None)
    err = _k.CreateFileW(str(log_err), rw, FILE_SHARE_READ, None, OPEN_ALWAYS, 0, None)
    for h in (stdhandle, out, err):
        if h and h != -1:
            _k.SetHandleInformation(h, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

    sc = SECURITY_CAPABILITIES(AppContainerSid=sid_ptr.value, CapabilityCount=0,
                               Capabilities=None)
    need = ctypes.c_size_t(0)
    # the size query is DOCUMENTED to return FALSE with ERROR_INSUFFICIENT_BUFFER
    # (122) and fill `need`; treating FALSE as fatal here is the beginner's bug
    if (not _k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(need))
            and ctypes.get_last_error() != 122) or need.value == 0:
        raise AppContainerError(f"attribute list size query failed: {ctypes.get_last_error()}")
    attr_buf = ctypes.create_string_buffer(need.value)
    if not _k.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(need)):
        raise AppContainerError(f"InitializeProcThreadAttributeList failed: "
                                f"{ctypes.get_last_error()}")

    created = False
    reaped = False
    pi = PROCESS_INFORMATION()
    try:
        if not _k.UpdateProcThreadAttribute(
                attr_buf, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                ctypes.byref(sc), ctypes.sizeof(SECURITY_CAPABILITIES), None, None):
            raise AppContainerError(f"UpdateProcThreadAttribute failed: "
                                    f"{ctypes.get_last_error()}")
        siex = STARTUPINFOEXW()
        siex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        siex.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        siex.StartupInfo.hStdInput = stdhandle
        siex.StartupInfo.hStdOutput = out
        siex.StartupInfo.hStdError = err
        siex.lpAttributeList = ctypes.cast(attr_buf, ctypes.c_void_p).value
        env = _env_block(env_extra)
        t0 = time.perf_counter()
        started = _k.CreateProcessW(
            None, command_line(argv), None, None,
            True, EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
            env, cwd, ctypes.byref(siex), ctypes.byref(pi))
        if not started:
            raise AppContainerError(f"CreateProcessW failed: error={ctypes.get_last_error()}")
        created = True
        timed_out = _k.WaitForSingleObject(pi.hProcess, int(timeout_s * 1000)) == WAIT_TIMEOUT
        if timed_out:
            # the container child gets no runaway grants either: kill it, and
            # surface the timeout to the caller exactly as run_shell would
            _k.TerminateProcess(pi.hProcess, 1337)
            _k.WaitForSingleObject(pi.hProcess, 5000)
        code = wt.DWORD(0)
        if not _k.GetExitCodeProcess(pi.hProcess, ctypes.byref(code)):
            raise AppContainerError("GetExitCodeProcess failed")
        reaped = True
    finally:
        if created and not reaped:
            try:
                _k.TerminateProcess(pi.hProcess, 1337)
                _k.WaitForSingleObject(pi.hProcess, 5000)
            except Exception:
                pass
        if created:
            _k.CloseHandle(pi.hProcess)
            _k.CloseHandle(pi.hThread)
        _k.DeleteProcThreadAttributeList(attr_buf)
        for h in (stdhandle, out, err):
            if h and h != -1:
                _k.CloseHandle(h)

    stdout = log_out.read_bytes().decode("utf-8", errors="replace")
    stderr = log_err.read_bytes().decode("utf-8", errors="replace")
    return {"rc": code.value, "stdout": stdout, "stderr": stderr,
            "timed_out": timed_out, "ms": int((time.perf_counter() - t0) * 1000)}


def scratch_dir(prefix: str) -> str:
    """A fresh directory under the caller's temp root the caller must grant."""
    return tempfile.mkdtemp(prefix=f"appcontainer_{prefix}_")


def new_profile_name(prefix: str) -> str:
    """Path-unique profile names keep repeated probe runs idempotent."""
    stem = hashlib.sha256(os.urandom(8)).hexdigest()[:10]
    return f"opencode.ac.{prefix}.{stem}".replace(".", "_")
