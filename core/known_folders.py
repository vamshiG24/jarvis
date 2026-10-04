"""
Where the user's standard folders ACTUALLY are.

WHY THIS EXISTS
    Fifteen places in this app wrote `Path.home() / "Desktop"` (or Downloads,
    Documents, …) and trusted it. That is only true on an untouched install.
    Windows lets the user move any of these folders — Explorer → Properties →
    Location — and OneDrive's "Known Folder Move" does it automatically, so on
    a great many machines the real Desktop is `E:\\desktop` or
    `C:\\Users\\x\\OneDrive\\Desktop`, and `C:\\Users\\x\\Desktop` is an empty
    leftover or does not exist at all.

    The cost was not cosmetic. "List my downloads" listed the wrong folder,
    the model was told the wrong Desktop and asked for paths that did not
    exist, and file_controller's sandbox (home folder only) refused every
    file on a Desktop that had been moved to another drive.

HOW
    Windows: SHGetKnownFolderPath — the shell's own answer, which follows
    every kind of redirection — then the User Shell Folders registry key, then
    the default. Linux: xdg-user-dir / user-dirs.dirs (localized names such as
    ~/Schreibtisch). macOS: the defaults are always real.

    Resolved once and cached; nothing here can raise.
"""
from __future__ import annotations

import os
import platform
import subprocess
from functools import lru_cache
from pathlib import Path

_OS = platform.system()

# name → (Windows KNOWNFOLDERID, registry value name, XDG key, default subdir)
_FOLDERS: dict[str, tuple[str, str, str, str]] = {
    "desktop":   ("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "Desktop",
                  "DESKTOP", "Desktop"),
    "downloads": ("374DE290-123F-4565-9164-39C4925E467B",
                  "{374DE290-123F-4565-9164-39C4925E467B}", "DOWNLOAD", "Downloads"),
    "documents": ("FDD39AD0-238F-46AF-ADB4-6C85480369C7", "Personal",
                  "DOCUMENTS", "Documents"),
    "pictures":  ("33E28130-4E1E-4676-835A-98395C3BC3BB", "My Pictures",
                  "PICTURES", "Pictures"),
    "music":     ("4BD8D571-6D19-48D3-BE97-422220080E43", "My Music",
                  "MUSIC", "Music"),
    "videos":    ("18989B1D-99B5-455B-841C-AB7C74E4DDFC", "My Video",
                  "VIDEOS", "Videos"),
}

NAMES = tuple(_FOLDERS)


def _win_known_folder(guid: str) -> Path | None:
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        u = uuid.UUID(guid)
        fid = _GUID(u.fields[0], u.fields[1], u.fields[2],
                    (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        buf = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
            try:
                p = Path(buf.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(buf)
            if p.is_dir():
                return p
    except Exception:
        pass
    return None


def _win_registry(value_name: str) -> Path | None:
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            val, _t = winreg.QueryValueEx(k, value_name)
        p = Path(os.path.expandvars(val))
        if p.is_dir():
            return p
    except Exception:
        pass
    return None


def _xdg(key: str) -> Path | None:
    home = Path.home()
    env = os.environ.get(f"XDG_{key}_DIR", "")
    if env and Path(env).is_dir():
        return Path(env)
    try:
        out = subprocess.run(["xdg-user-dir", key], capture_output=True,
                             text=True, timeout=3)
        val = out.stdout.strip()
        if val and Path(val) != home and Path(val).is_dir():
            return Path(val)
    except Exception:
        pass
    try:
        cfg = home / ".config" / "user-dirs.dirs"
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"XDG_{key}_DIR"):
                val = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(home))
                if Path(val) != home and Path(val).is_dir():
                    return Path(val)
    except Exception:
        pass
    return None


@lru_cache(maxsize=None)
def get(name: str) -> Path:
    """The real location of a standard folder: desktop, downloads, documents,
    pictures, music or videos. Falls back to ~/<Default> as a last resort."""
    name = name.lower().strip()
    guid, reg, xdg, default = _FOLDERS[name]
    found = None
    if _OS == "Windows":
        found = _win_known_folder(guid) or _win_registry(reg)
    elif _OS == "Linux":
        found = _xdg(xdg)
    return found or (Path.home() / default)


def desktop() -> Path:
    return get("desktop")


def downloads() -> Path:
    return get("downloads")


def documents() -> Path:
    return get("documents")


def pictures() -> Path:
    return get("pictures")


def music() -> Path:
    return get("music")


def videos() -> Path:
    return get("videos")


def all_folders() -> dict[str, Path]:
    return {n: get(n) for n in NAMES}


def remap_default(p: Path) -> Path:
    """Map a path under a *default* location onto the real one.

    `C:\\Users\\x\\Desktop\\notes` → `E:\\desktop\\notes` when the Desktop has
    been moved. Models (and older code) build paths from the default layout;
    when that default does not exist but the real folder does, the intent is
    unambiguous. A path that exists as written is always left alone."""
    try:
        if p.exists():
            return p
        home = Path.home()
        for name, (_g, _r, _x, default) in _FOLDERS.items():
            base = home / default
            real = get(name)
            if real == base:
                continue
            try:
                rel = p.relative_to(base)
            except ValueError:
                # Case-insensitive match on Windows
                if _OS == "Windows" and str(p).lower().startswith(str(base).lower()):
                    rel = Path(str(p)[len(str(base)):].lstrip("\\/"))
                else:
                    continue
            return real / rel
    except Exception:
        pass
    return p
