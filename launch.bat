@echo off
title Cyberdeck Launcher

REM Launch the deck in a fullscreen Windows Terminal window. `wt`
REM returns once the new window is created — the deck (python
REM tui.py) starts up inside that window.
wt --fullscreen python tui.py

REM Brief delay so the deck comes up and writes its log header
REM before the Mechanic supervisor goes looking. Mechanic
REM discovers the deck PID by reading that header, so the deck
REM has to be at least past header-write first. 1 second is
REM plenty (header lands in DeckLogger.__init__, very early).
REM Boot-loop scenario (deck crashes inside the first second):
REM mechanic spawns, sees a stale log + dead deck pid, exits
REM cleanly. No orphan subprocesses to clean up because no
REM constructs were spawned.
timeout /t 1 /nobreak > NUL

REM Spawn the Mechanic supervisor in its own minimized window.
REM It tails the file logger's NDJSON stream for live claude
REM subprocess pids and kills them on detected deck death
REM (Task Manager kill, OOM, blue screen, etc.). Sibling process,
REM lives until cleanup finishes. Design:
REM `Design Files/cyberdeck-maintbot-design.md`.
start "Cyberdeck Mechanic" /MIN python "%~dp0mechanic.py"