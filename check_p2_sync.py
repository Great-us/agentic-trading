"""Check that Trading-P2's source tree has not silently drifted from Trading.

The two books share ~11k lines by design (independent directories so the OS
cycle locks never collide), which means every engine bugfix must land in both.
This script makes any divergence visible; run it weekly or before trusting a
backtest from either side.

Usage:
    python check_p2_sync.py            # exit 0 = in sync, 1 = drift found

Only src/agentic_trading is compared — config/, data/, logs/ and research/
are supposed to differ between the books.
"""
from __future__ import annotations

import filecmp
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAIN_SRC = HERE / "src" / "agentic_trading"
P2_ROOT = Path(os_p2_root()) if (os_p2_root := globals().get("P2_ROOT_OVERRIDE", None)) else HERE.parent / "Trading-P2"
P2_SRC = P2_ROOT / "src" / "agentic_trading"

IGNORE = {"__pycache__"}


def walk(root: Path) -> dict[str, Path]:
    return {
        p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*.py"))
        if not (set(p.parts) & IGNORE)
    }


def main() -> int:
    if not P2_SRC.is_dir():
        print(f"P2 source tree not found at {P2_SRC}")
        return 1
    main_files, p2_files = walk(MAIN_SRC), walk(P2_SRC)
    # Intentional asymmetry between the books — never counts as drift:
    EXPECTED_ONLY_MAIN_PREFIX = ("dashboard/",)          # read-only dashboard lives here
    EXPECTED_ONLY_P2 = {"p2_roster.py"}                  # the rotation roster generator
    drift: list[str] = []

    for name in sorted(set(main_files) - set(p2_files)):
        if name.startswith(EXPECTED_ONLY_MAIN_PREFIX):
            print(f"  expected (main only): {name}")
        else:
            drift.append(f"  only in MAIN : {name}")
    for name in sorted(set(p2_files) - set(main_files)):
        if name in EXPECTED_ONLY_P2:
            print(f"  expected (P2 only)  : {name}")
        else:
            drift.append(f"  only in P2   : {name}")
    for name in sorted(set(main_files) & set(p2_files)):
        if not filecmp.cmp(main_files[name], p2_files[name], shallow=False):
            drift.append(f"  DIFFERS      : {name}")

    if drift:
        print(f"DRIFT: {len(drift)} finding(s) between {MAIN_SRC} and {P2_SRC}")
        print("\n".join(drift))
        print("\nEvery shared-file change must be applied to both books")
        print("(or ported via git: git -C Trading-P2 diff/apply against this baseline).")
        return 1
    print(f"In sync: {len(main_files)} files identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
