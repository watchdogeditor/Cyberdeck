# Cyberdeck — Platform Portability Inventory

*Living inventory of every line of Windows-specific code in the
deck, with notes on what swaps when porting to RPi-Linux (the
eventual hardware-agnostic deployment target). The deck is
intentionally cross-platform in design but Windows-specific in
current production reality — this doc tracks the explicit
fence-posts so porting cost is known, not surprising.*

*Filed 2026-05-06 by netrunner. Pair with `cyberdeck-philosophy.md`
("hardware-agnostic by goal"), `cyberdeck-state.md` Filed gotchas
(many Windows landmines have entries there), and
`cyberdeck-spec.md` (which doesn't currently call out platform
boundaries explicitly — should reference this doc once it's
stable).*

**Update rule:** anytime new Windows-specific code lands, add it
here. A spawn site that uses `sys.platform`, a ctypes call into
`kernel32`/`user32`/`advapi32`, a path that assumes drive letters
or backslashes, an encoding that assumes cp1252, a symlink
fallback, a subprocess invocation that uses `wt`/`cmd.exe`/
`PowerShell` — all belong here.

---

## Status summary (as of 2026-05-06)

| Category | Status | Linux-ready? |
|---|---|---|
| Entry / bootstrap (launch.bat) | Windows-only | Needs `launch.sh` sibling |
| Subprocess pid-alive + kill | Cross-platform via `sys.platform` branches | ✅ |
| Clipboard write | Cross-platform via fan-out (Windows / macOS / Linux) | ✅ |
| File logger `latest.log` pointer | Cross-platform (symlink → hard-copy on Windows) | ✅ |
| Brake-hook path/command matching | Cross-platform (case-insensitive + backslash norm on Windows) | ✅ |
| Doctor stdout encoding | ASCII-only on all platforms (defensive for cp1252) | ✅ |
| Tool surface (PowerShell vs Bash) | Both registered; tools.toml is platform-neutral | ✅ logically; tooling availability differs |
| Heartbeat file (deck → mechanic) | Cross-platform (mtime-based; `pathlib`) | ✅ |
| LLM subprocess flags (claude code) | Cross-platform (claude binary handles per-OS) | ✅ |

**Net: most of the deck is platform-agnostic.** The Windows
fence-posts are concentrated in three areas: the entry script
(launch.bat), Win32 API access for clipboard + pid-watch, and
Windows-encoding awareness in stdout-facing code. Each is small,
isolated, and well-commented.

---

## Detailed inventory

### 1. Entry script — `launch.bat` (Windows-only, top-level)

**File:** `launch.bat` (~25 lines, batch script).

**What it does:**
- `wt --fullscreen python tui.py` — launch the deck in Windows
  Terminal fullscreen
- `timeout /t 1 /nobreak > NUL` — 1s sleep so the deck writes its
  log header before mechanic attaches
- `start "Cyberdeck Mechanic" /MIN python "%~dp0mechanic.py"` —
  spawn the mechanic supervisor in a separate minimized window

**Why Windows-only:** `wt` (Windows Terminal), `start` (cmd.exe
built-in), `timeout` (Windows command), `%~dp0` (batch script
syntax) all are Windows. Linux equivalent would use a different
terminal (alacritty / kitty / gnome-terminal) and bash backgrounding.

**Linux port plan:** add a sibling `launch.sh` script (~25 lines)
that does the same flow with the Linux equivalents:
```bash
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Launch deck in fullscreen terminal (Linux: depends on installed
# emulator; alacritty / kitty / wezterm all support a similar
# --fullscreen flag).
alacritty --class cyberdeck -e python "${SCRIPT_DIR}/tui.py" &
DECK_LAUNCH_PID=$!

sleep 1

# Launch mechanic in a separate (background) terminal.
alacritty --class cyberdeck-mechanic -e python "${SCRIPT_DIR}/mechanic.py" &
```

**Decisions deferred:** which Linux terminal to target (probably
none specifically — the netrunner picks at deploy time and edits
the script). For headless / wall-mount RPi mode, the launch
script would be different again — `tmux`-based or systemd-unit-
based with no terminal emulator.

**Filed in:** this doc.

---

### 2. Subprocess pid management — `mechanic.py`

**Function:** `_pid_alive_win` (Win32 API) + `_pid_alive_posix`
(`os.kill(pid, 0)` signal probe). Dispatch via
`sys.platform == "win32"` in `pid_alive()`.

**File:** `mechanic.py` lines ~62-135.

**Why platform-specific:** Windows has no `kill -0` equivalent;
process liveness check requires `OpenProcess` + `GetExitCodeProcess`.
POSIX uses signal 0 to probe without delivering a real signal.

**Implementation:** ctypes calls into `kernel32.OpenProcess`,
`kernel32.GetExitCodeProcess`, `kernel32.CloseHandle`. **Critical:**
explicit argtypes + restype are MANDATORY (see Filed gotchas →
"ctypes Windows-handle truncation"). Without explicit types, ctypes
defaults to `c_int` (32-bit) for HANDLE returns, truncating
pointer-sized handles on 64-bit Windows. Bug shipped 2026-05-04
in initial mechanic v0; surfaced + fixed 2026-05-06.

**Linux port:** the POSIX path already works. No new code needed
when the deck deploys to RPi-Linux.

**Filed in:** `cyberdeck-state.md` Filed gotchas → Async / subprocess
("ctypes Windows-handle truncation").

---

### 3. Subprocess kill — `mechanic.py` `kill_pid`

**File:** `mechanic.py` lines ~140-170.

**Why platform-specific:** `os.kill(pid, SIGTERM)` on Windows maps
to `TerminateProcess` (synchronous, immediate, no graceful
shutdown opportunity). On POSIX, SIGTERM is handlable; deck-side
finally-blocks can run. The mechanic uses a 2s grace period on
POSIX, escalating to SIGKILL if the process doesn't die.

**Linux port:** already correct. The platform branch handles
both cleanly.

**Filed in:** this doc.

---

### 4. Clipboard write — `clipboard.py`

**File:** `clipboard.py` (~200 lines).

**Why platform-specific:** every OS exposes the clipboard
differently. Windows = Win32 API via ctypes; macOS = `pbcopy`
subprocess; Linux = `wl-copy` (Wayland) or `xclip` (X11) subprocess.

**Implementation:**
- Windows path uses `OpenClipboard` / `EmptyClipboard` /
  `SetClipboardData` / `GlobalAlloc` / `GlobalLock` /
  `GlobalUnlock` / `CloseClipboard` from kernel32 + user32.
  All have explicit argtypes + restype (correct pattern post-
  2026-05-06 ctypes fix).
- Encoding: `utf-16-le` + null terminator for Windows
  `CF_UNICODETEXT`. The clipboard.py module docstring documents
  the cp1252 trap — `clip.exe` was tried first and silently
  mangled non-ASCII characters because of cp1252 default codec.

**Linux port:** already cross-platform. Picks up `wl-copy` first
(Wayland), falls back to `xclip -selection clipboard` (X11).
RPi running modern Linux usually has both available.

**Filed in:** `cyberdeck-state.md` Filed gotchas → Terminal/Textual
("clipboard.py / clip.exe / cp1252 encoding pitfalls").

---

### 5. File logger `latest.log` pointer — `logger.py`

**File:** `logger.py` lines ~220-240.

**Why platform-specific:** symlink creation on Windows requires
admin privilege (or developer mode), so the file logger falls back
to a hard-copy of the current log file when `symlink_to()` raises.
On POSIX, real symlinks work without privilege escalation.

**Implementation:** try `latest.symlink_to(self.path.name)`; on
`OSError` (insufficient privileges), copy the file content to
`latest.log` instead. Best-effort throughout — disk failures
degrade gracefully.

**Linux port:** symlink path already works. No changes.

**Filed in:** this doc.

---

### 6. Brake-hook path/command matching — `brake_hook.py`

**File:** `brake_hook.py` lines ~365-450.

**Why platform-specific:** Windows filesystems are case-insensitive
by default, and use backslash separators. The brake hook's
destructive-command + protected-path patterns need to match
regardless of how the construct typed the command. POSIX is
case-sensitive and uses forward slashes.

**Implementation:** `on_windows = sys.platform.startswith("win")`
gate. When True: lowercase the haystack, also test against a
backslash-to-forward-slash normalized version. When False: leave
case alone.

**Linux port:** already correct. The non-Windows branch is the
default behavior; case-sensitive matching just works.

**Filed in:** this doc.

---

### 7. Doctor stdout encoding — `doctor.py`

**File:** `doctor.py` (~280 lines, mostly stdout-facing).

**Why platform-aware:** Windows' default Python stdout encoding
when piped is `cp1252`, which can't render Unicode characters
like em-dashes, arrows, or box-drawing. If a netrunner runs
`python tui.py --doctor > diag.txt`, those characters trigger
`UnicodeEncodeError`. Even when stdout is the terminal directly,
older Windows console hosts (pre-Windows-11 conhost) used cp1252
unless explicitly told to use UTF-8.

**Implementation:** doctor.py's output is **ASCII-only** on all
platforms — no smart quotes, no arrows, no em-dashes. Comments
explain the cp1252 reasoning. This is preventive; works fine on
Linux (UTF-8 default) and on macOS (UTF-8 default) without
loss of fidelity (ASCII is a subset of both).

**Linux port:** no change needed. ASCII-only output works
everywhere.

**Filed in:** this doc.

---

### 8. Tool surface — PowerShell vs Bash

**Files:** `construct.py` (DEFAULT_TOOLS), `brake_hook.py`
(SHELL_TOOLS gating), `tools.toml` (registered bins).

**Why platform-aware:** PowerShell is the Windows-native shell
constructs use; Bash is the POSIX-native shell. Constructs
running on Windows reach for PowerShell; on Linux they reach
for Bash. Brake hook's destructive-command patterns gate both
symmetrically (per the orientation doc's "Don't gate one shell
tool without the other" hard rule — the LLM will silently
pivot to the other shell if you only block one).

**Implementation:**
- `construct.py` `DEFAULT_TOOLS` includes both PowerShell and
  Bash by default. Claude Code's CLI accepts both as built-in
  tool names.
- `brake_hook.py` `SHELL_TOOLS = {"Bash", "PowerShell"}` — the
  same destructive-command + protected-path checks apply to both.
- `tools.toml` registers binaries by command name; `pwsh` /
  `bash` could both be registered if desired.

**Linux port:** PowerShell is available on Linux (`pwsh`), but
constructs spawned on Linux will more naturally use Bash. The
PowerShell tool stays registered but is rarely used. No code
change needed.

**Filed in:** orientation doc hard rules ("Don't gate one shell
tool without the other") + this doc.

---

### 9. Heartbeat file — deck writes, mechanic reads

**Files:** `tui.py` (heartbeat-write timer), `mechanic.py`
(`heartbeat_age_seconds` mtime-poll).

**Why cross-platform:** uses `pathlib.Path` for path operations
and `os.path.getmtime()` for staleness check. Both abstract
platform differences.

**Linux port:** already works.

**Filed in:** this doc.

---

### 10. LLM subprocess invocation — `claude` binary

**Files:** `construct.py`, `daemon.py`, `watchdog.py`, `advisor.py`,
`mechanic_triage.py`.

**Why cross-platform:** the `claude` binary itself handles per-OS
behavior. The deck just invokes it via `subprocess` with the
same flags everywhere.

**One Windows-specific gotcha** (filed as Hard Rule in CLAUDE.md):
**multi-line argv truncates at the first newline on Windows.**
Affects `--system-prompt`, `--append-system-prompt`,
`--mcp-config <text>`, and any other flag taking multi-line
content as an argv value. Fix: always use the `-file` variant
(`--system-prompt-file`, `--append-system-prompt-file`, etc.).
POSIX shells handle multi-line argv correctly, so this is
Windows-specific in symptom — but the file-based fix is
platform-agnostic and the right pattern even when deploying to
Linux.

**Linux port:** the claude binary works the same. The argv-newline
gotcha is irrelevant on Linux but the fix (file-based flags)
stays. Already canonical.

**Filed in:** CLAUDE.md Hard Rules + `cyberdeck-state.md` Filed
gotchas → Async / subprocess.

---

## Filed Windows-relevant gotchas (cross-reference)

These live in `cyberdeck-state.md` Filed gotchas → Async /
subprocess section. All are diagnosed + fixed; this doc lists them
for the porter's awareness.

1. **🚨 Multi-line argv truncates at first newline on Windows.**
   Use `-file` variants of every CLI flag taking multi-line
   content. Promoted to top-level Hard Rule in CLAUDE.md.
2. **`--bare` breaks Claude Max OAuth.** Don't use `--bare` for
   any subprocess that depends on OAuth/keychain auth (which is
   all of the deck's spawns — Claude Max is the netrunner's auth
   path). Use env-var belt instead.
3. **ctypes Windows-handle truncation.** ALWAYS set argtypes +
   restype on kernel32 / user32 / advapi32 calls. Without them,
   default `c_int` truncates pointer-sized HANDLE values on
   64-bit Windows.
4. **Async-task teardown isn't guaranteed to run before process
   exit.** Cleanup that needs to be durable across exit must run
   synchronously BEFORE `exit()`. Don't rely on async finally-
   blocks under cancellation. (Cross-platform issue, but surfaces
   most often on Windows where `start` and `wt` interact with
   process lifecycle differently.)
5. **`shift+space`, `ctrl+space`, `ctrl+i`, `ctrl+m`** rarely
   transmit distinctly in real terminals. Test on real terminal,
   not just pilot. (Terminal-specific, not Windows-specific, but
   often surfaces first on Windows because of conhost / Windows
   Terminal differences.)

---

## Linux/Pi porting checklist

When the deck targets RPi-Linux (or any non-Windows host), the
following work items become live:

### Required for first Linux launch

- [ ] **`launch.sh`** — sibling to launch.bat. Pick a default
      terminal (alacritty? kitty?) or skip terminal-launching
      for headless mode.
- [ ] **Heartbeat path default** — currently
      `<home>/.cyberdeck/heartbeat`. Already path-agnostic via
      pathlib; verify it works on Linux at deploy time.
- [ ] **mechanic.py launch convention** — same `launch.bat` flow
      adapted to bash. Should "just work" given the `_pid_alive_*`
      branch is already cross-platform.
- [ ] **Verify clipboard.py on the deploy distro** — wl-copy
      (Wayland) and xclip (X11) need to be installed. Doctor
      should add a check for at least one being available.
- [ ] **PATH / shell expectations** — ensure bash is in PATH
      (almost always true; doctor could verify).
- [ ] **Real-deck Linux verification** — every gotcha in the
      Filed gotchas list needs to be re-checked on Linux to
      confirm it doesn't surface differently. Especially:
      argv-newline truncation (shouldn't happen on Linux but
      we use file-based flags everywhere now anyway), terminal
      key-sequence transmission, async cleanup-before-exit.

### Optional / form-factor-dependent

- [ ] **Headless mode** — wall-mount RPi with no terminal: deck
      runs in a tmux session or as a systemd unit, mechanic
      runs as a sibling unit. UI is exposed via SSH or a
      separate display surface (TUI rendered to a small LCD?).
      Maintbot design doc has more on this.
- [ ] **Boot-time auto-launch** — systemd unit for the deck
      with mechanic as a dependent unit.
- [ ] **D1 substrate (local model)** — deferred, hardware-blocked.
      Per philosophy doc, this is the long-term direction:
      replace cloud-Claude with on-device 7B model. Affects
      every spawn-site's `claude_bin` and removes the "burns
      tokens" cost story.

### Non-blocking; can be added incrementally

- [ ] **Rich terminal feature detection** — let the deck pick
      between fancy and stripped-down rendering based on terminal
      capability advertisement (TERM env var, terminfo). Today
      the deck assumes Windows Terminal capabilities; on a real
      ARM Linux box with a less-capable terminal, some renders
      may break.
- [ ] **Plugin platform gates** — plugins.toml has a `requires.platforms` field;
      each plugin can declare which OSes it works on. Today the
      `screenshot` plugin marks itself `["windows", "linux", "darwin"]`
      via mss. Future plugins should follow this pattern.

---

## Update protocol

When NEW Windows-specific code lands:

1. Add an entry to "Detailed inventory" above with: file, lines,
   what it does, why platform-specific, Linux port plan.
2. If it's a gotcha (silent failure, intermittent, easy to
   re-introduce), file it in `cyberdeck-state.md` Filed gotchas
   AND cross-reference here.
3. If it changes the Linux porting story (new dep, new path
   issue), update the porting checklist.

When porting work begins (real-deck Linux deploy):

1. Walk this doc top to bottom. For each entry, verify the Linux
   path actually works on the target distro.
2. Fill out the porting checklist; tick items as they ship.
3. Update Filed gotchas with any Linux-specific landmines that
   surface (currently the deck has zero — because it has zero
   real-deck Linux exposure yet).

---

## Things deliberately NOT in this doc

- **Path separator handling** (`/` vs `\`). pathlib + os.path
  abstract this. Calling out every `Path.join` or string
  concatenation would be noise. The brake hook's
  backslash-normalization pattern is the only place where path
  separators are explicit, and it's listed in entry #6.
- **Line endings** (LF vs CRLF). git's `core.autocrlf` handles
  this at the source-control level. The few places we read /
  write text files use Python's universal-newlines default.
- **Environment variable case sensitivity.** Windows env vars
  are case-insensitive; POSIX is case-sensitive. The deck
  doesn't currently rely on case-sensitive env var lookup.
  If it ever does, this changes.
- **The CYBERDECK_HOME path.** Defaults to a directory next to
  the source tree; netrunner can override via env var. Path
  itself is platform-agnostic (just a directory name).

---

*Last updated: 2026-05-06 (filed). Update on every new Windows-
specific code addition.*
