"""A folder on the user's machine, as one absolute path.

Why this exists: a folder stored with a leading separator before the drive letter is not absolute on Windows. Path.resolve()
then keeps it drive-less, and the server builds the file path from its own working folder, so a write lands in (or tries to
create) a folder under the server's install directory. Every place that turns a stored folder into a file path goes through
normalise_device_path, so the rule is the same at save time and at use time.
"""
import ntpath
import posixpath
import re

_DRIVE_RX = re.compile(r"^[A-Za-z]:[\\/]")
_SLASHED_DRIVE_RX = re.compile(r"^[\\/]+([A-Za-z]:[\\/].*)$")   # a slash before the drive: the drive is there, one separator too many


def normalise_device_path(value) -> str:
    """The folder as an absolute path on the user's machine, or ValueError.

    "D:/reports/weekly" and "/D:/reports/weekly" both become "D:\\reports\\weekly"; "/home/me/reports" stays POSIX.
    Refused: empty, relative ("reports"), drive-relative ("D:reports"), UNC shares, and any ".." part.
    """
    raw = str(value or "").strip().strip('"').strip()
    if not raw:
        raise ValueError("no folder is set")
    m = _SLASHED_DRIVE_RX.match(raw)
    if m:
        raw = m.group(1)
    if ".." in re.split(r"[\\/]", raw):
        raise ValueError("the folder path may not contain '..'")
    if _DRIVE_RX.match(raw):
        out = ntpath.normpath(raw)
        if not ntpath.isabs(out) or not ntpath.splitdrive(out)[0]:
            raise ValueError(f"not a full path: {value}")
        return out
    if raw.startswith("/") and not raw.startswith("//"):
        return posixpath.normpath(raw)
    raise ValueError(f"not a full path (it must start with a drive letter such as D:): {value}")
