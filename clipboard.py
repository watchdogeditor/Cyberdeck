"""Cross-platform clipboard write — stdlib only.

Used by the deck's `y` / `Y` copy keybinds to sidestep the Ctrl+C-as-copy
issue at the UX layer. The netrunner can yank the focused widget's
content into the OS clipboard without dropping a SIGINT into every
child claude subprocess on the way (per the 2026-04-30 Ctrl+C autopsy
in cyberdeck-state.md → Filed gotchas).

No third-party dependency. Each platform talks to its native
clipboard surface:

  Windows: ctypes against the Win32 clipboard API (CF_UNICODETEXT).
           clip.exe was tried first, twice — once with `text=True`
           (failed: cp1252 default codec choked on arrows / em-dashes
           / Cyrillic in watchdog content, writer thread exploded,
           main thread sat on stdin until the timeout fired). Once
           with explicit UTF-16-LE-with-BOM bytes (failed: clip.exe
           preserves the BOM in the clipboard contents — the leading
           U+FEFF breaks strict JSON parsers downstream). ctypes
           sidesteps both: direct control over CF_UNICODETEXT bytes,
           no encoding round-trip, no BOM injection. Round-trip
           verified via PowerShell Get-Clipboard. Same role as
           pbcopy/xclip — platform-native clipboard utility, just
           via the OS API rather than a CLI wrapper.
  macOS:   `pbcopy` (subprocess; UTF-8 stdin native).
  Linux:   `wl-copy` (Wayland) or `xclip -selection clipboard` (X11).
           UTF-8 stdin native on both.

Best-effort throughout — if the API call fails on Windows or the
utility is missing on Linux, `copy()` returns False with a
human-readable reason. Never raises.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Optional


def copy(text: str) -> tuple[bool, Optional[str]]:
    """Write `text` to the OS clipboard. Returns (success, error_msg).

    On success: (True, None).
    On failure: (False, "human-readable reason") — caller toasts the
    reason so the netrunner knows whether to install xclip, etc.

    Empty string is a valid copy; the caller decides whether to
    short-circuit before calling.
    """
    try:
        if sys.platform == "win32":
            return _copy_windows(text)
        if sys.platform == "darwin":
            return _copy_macos(text)
        return _copy_linux(text)
    except Exception as exc:
        return False, f"clipboard error: {exc}"


def _copy_windows(text: str) -> tuple[bool, Optional[str]]:
    """Set the Windows clipboard via the Win32 API directly.

    See module docstring for why we don't use clip.exe. Standard
    CF_UNICODETEXT pattern: open clipboard, empty it, alloc a
    moveable global buffer, copy UTF-16-LE bytes (NO BOM — that's
    the point), transfer ownership to the system via SetClipboardData.
    """
    import ctypes
    from ctypes import wintypes

    # CF_UNICODETEXT is the format every modern Windows app reads
    # first when pulling text — handles Unicode natively, no codepage
    # juggling. GMEM_MOVEABLE is required for SetClipboardData per
    # MSDN.
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Function signatures — declaring them lets ctypes do correct
    # 64-bit pointer-vs-int handling. Without restype set, ctypes
    # default-returns int which truncates HANDLE values on 64-bit
    # Windows.
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    # OpenClipboard with hwnd=0 ties the clipboard to the current
    # task without needing a window handle. Failure means another
    # process holds the clipboard right now (rare, transient).
    if not user32.OpenClipboard(0):
        return False, "OpenClipboard failed (clipboard busy)"

    h_mem = None
    try:
        if not user32.EmptyClipboard():
            return False, "EmptyClipboard failed"

        # UTF-16-LE with a 2-byte null terminator is the
        # CF_UNICODETEXT contract. NO BOM — the BOM only exists for
        # disambiguating byte streams; CF_UNICODETEXT is always
        # UTF-16-LE by definition. Adding one bakes a U+FEFF char
        # into the clipboard text (the bug clip.exe gave us).
        data = text.encode("utf-16-le") + b"\x00\x00"

        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            return False, "GlobalAlloc failed"

        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            return False, "GlobalLock failed"
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(h_mem)

        # SetClipboardData transfers ownership of h_mem to the
        # system on success — we MUST NOT GlobalFree it after.
        # On failure, ownership stays with us (freed in finally).
        if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            return False, "SetClipboardData failed"
        h_mem = None  # ownership transferred
        return True, None
    except Exception as exc:
        return False, f"win32 clipboard error: {exc}"
    finally:
        # If h_mem is still set, we own it and must free it.
        # Otherwise SetClipboardData took ownership.
        if h_mem is not None:
            kernel32.GlobalFree(h_mem)
        user32.CloseClipboard()


def _copy_macos(text: str) -> tuple[bool, Optional[str]]:
    # pbcopy reads UTF-8 stdin natively (macOS default everywhere).
    proc = subprocess.run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        return False, f"pbcopy exit {proc.returncode}: {stderr}"
    return True, None


def _copy_linux(text: str) -> tuple[bool, Optional[str]]:
    # Try Wayland first (modern desktops), then X11. Either may be
    # missing on a minimal install or a headless box. If both are
    # gone, surface a hint about which package to install. Both
    # utilities take UTF-8 bytes; same encoding contract as macOS.
    data = text.encode("utf-8")
    if shutil.which("wl-copy") is not None:
        proc = subprocess.run(
            ["wl-copy"],
            input=data,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return True, None
        # wl-copy fell over; try xclip as fallback before giving up.

    if shutil.which("xclip") is not None:
        proc = subprocess.run(
            ["xclip", "-selection", "clipboard"],
            input=data,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return True, None
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        return False, f"xclip exit {proc.returncode}: {stderr}"

    return False, "no clipboard utility found (install xclip or wl-clipboard)"
