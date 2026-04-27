# screenshot — capture the screen as a PNG

Cross-platform screen capture using the [mss](https://pypi.org/project/mss/)
library. Multi-monitor aware (default captures the entire virtual
desktop). One invocation = one capture; no daemon process, no state
between calls.

## Invocation

```
python <plugin_dir>/run.py [output_path] [monitor_index]
```

Both arguments optional. `<plugin_dir>` is the absolute path to this
plugin's folder; the deck provides it in your system-prompt addendum
at spawn time.

- `output_path` (default: `screenshot-YYYYMMDD-HHMMSS.png` in cwd):
  where to write the PNG. Relative paths resolve against cwd. Parent
  directories are created if missing.
- `monitor_index` (default: `0`): which monitor to capture. `0` is
  the "all monitors" virtual rectangle (covers every display).
  `1`, `2`, ... are individual monitors in mss's enumeration order.

## Output

- Stdout (on success): the absolute path of the written PNG, one line.
- Stderr (on failure): `ERROR: <reason>` and a non-zero exit code.

## Examples

Quickest possible capture, default location:

```bash
python /path/to/plugins/screenshot/run.py
# → C:/Users/.../screenshot-20260427-153045.png
```

Capture to a specific path:

```bash
python /path/to/plugins/screenshot/run.py /tmp/myshot.png
```

Capture only the primary monitor:

```bash
python /path/to/plugins/screenshot/run.py shot.png 1
```

## Use cases

- Daemon asks you to "show me what error message is on the screen" —
  invoke this, return the path on stdout, the daemon reads it back.
- Netrunner wants a visual record of the deck's current state — same.
- Verifying a UI change ran end-to-end — capture before + after.

## Dependencies

- Python: `mss` ≥ 9.0 — `pip install mss`. Pure Python, ~200KB.
- OS: Windows, Linux (X11 or Wayland with appropriate libs),
  macOS. The plugin will refuse to load on unrecognized platforms.

## Notes

- File size for a typical 4K display is ~3-8 MB depending on
  content. Don't run this in a tight loop unless you mean it.
- mss respects display scaling; the captured pixel dimensions may
  differ from the logical screen size on high-DPI displays.
- This plugin is stateless. Future variants (e.g., `screenshot-region`
  for selective capture, `screenshot-window` for window-targeted
  capture) would land as separate plugins, not modes of this one.
