"""
runs.py — per-launch workspace primitive.

A "run" is a per-deck-launch working directory at
`<home>/runs/run-<timestamp>-<id>/`. It exists as an OUTPUT
DIRECTORY CONVENTION communicated to constructs via the deck
addendum + daemon system prompt — constructs cwd at `<home>` and
write outputs to absolute paths under the run dir.

Distinct from `Fleet.run_id` (a separate deck-launch identifier
used in NDJSON log envelopes + read by mechanic.py). That stays
as-is.

See `Design Files/in-flight/cyberdeck-per-run-workspaces-design.md`
for the design history. This module is the smallest piece that
survived the iteration: a Run dataclass + a mint helper.
"""
from __future__ import annotations

import datetime as _dt
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Run-id format: short hex slug for same-second uniqueness. 4 hex
# chars = 65k collision space within a single second; the timestamp
# component (YYYY-MM-DD-HHMMSS) carries the chronological signal.
_RUN_ID_HEX = 4


@dataclass(frozen=True)
class Run:
    """One per-launch workspace.

    Fields:
      run_id:    4-hex slug uniquely identifying this run. Distinct
                 from Fleet.run_id (deck-launch id used in NDJSON).
      timestamp: Birth time (local-time datetime). Used for the
                 folder name and any future sort ordering.
      dir_path:  Absolute path to the run's directory. Created at
                 mint time; persists across the deck's lifetime.
      origin:    "deck" today; reserved for future shapes (a future
                 morgue record might attribute differently). Surfaces
                 in the bus event payload for log readers.
      goal:      Optional goal text. None today; reserved.
    """
    run_id: str
    timestamp: _dt.datetime
    dir_path: Path
    origin: str = "deck"
    goal: Optional[str] = None


def runs_root(home: Path) -> Path:
    """Return `<home>/runs/`. Doesn't create — mint_run does that."""
    return Path(home) / "runs"


def _mint_run_id() -> str:
    """4 hex chars from a CSPRNG. Hex (not full uuid4) to keep the
    folder name short — the timestamp prefix carries identity, the
    suffix just disambiguates within-second mints."""
    return secrets.token_hex(_RUN_ID_HEX // 2)


def _format_timestamp(dt: _dt.datetime) -> str:
    """YYYY-MM-DD-HHMMSS for use in folder names. Local time, not
    UTC — the netrunner browses these in their own timezone, so the
    folder name should match wall-clock impressions."""
    return dt.strftime("%Y-%m-%d-%H%M%S")


def mint_run(
    home: Path,
    origin: str = "deck",
    goal: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
) -> Run:
    """Create a fresh run dir and return the Run object.

    Idempotent against same-second collisions: if the dir already
    exists for some reason (mint twice in the same second with the
    same hex draw), we re-mint the hex until we find a free slot.
    Practically this loop runs once.

    `home` is the deck home dir (cyberdeck-home). The runs root is
    `<home>/runs/`, created on demand.

    `now` is injectable for testing; defaults to datetime.now().
    """
    if now is None:
        now = _dt.datetime.now()

    runs_dir = runs_root(home)
    runs_dir.mkdir(parents=True, exist_ok=True)

    timestamp_str = _format_timestamp(now)
    # Re-mint hex on collision. Practically never happens; the loop
    # bounds at 64k attempts before raising — that bound only ever
    # trips on a filesystem-level error masking real failures.
    for _ in range(64_000):
        run_id = _mint_run_id()
        dir_name = f"run-{timestamp_str}-{run_id}"
        dir_path = runs_dir / dir_name
        try:
            dir_path.mkdir(parents=False, exist_ok=False)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError(
            f"runs.mint_run: exhausted hex space at {timestamp_str}; "
            f"check {runs_dir} for filesystem corruption"
        )

    return Run(
        run_id=run_id,
        timestamp=now,
        dir_path=dir_path,
        origin=origin,
        goal=goal,
    )
