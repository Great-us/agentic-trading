"""Verify (and repair) the `file.py:123` citations inside HANDOFF-*.md.

A handoff document is only as good as its line numbers, and line numbers rot
the moment anyone edits the file they point into. Rather than ask a reader to
trust them — or to re-verify 51 citations by hand — this script pins each
citation to a *piece of code* instead of to a number:

    --snapshot   read every citation, record the distinctive source line it
                 currently points at, and store that in handoff-anchors.json
    (default)    find each recorded line in today's source and report whether
                 the citation still lands on it, moved, or vanished
    --fix        rewrite the .md so every moved citation points at the right
                 line again

A citation that MOVED is harmless drift and --fix repairs it. A citation that
is MISSING means the code it described was changed or deleted — that is a real
signal that the handoff's prose needs a human, and no amount of renumbering
will fix it.

Usage:
    python check_handoff_anchors.py --snapshot          # once, to establish the baseline
    python check_handoff_anchors.py                     # verify; exit 1 on any MISSING
    python check_handoff_anchors.py --fix               # verify + renumber the doc
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ANCHOR_STORE = ROOT / "handoff-anchors.json"
DEFAULT_DOCS = ["HANDOFF-DASHBOARD.md"]

# Directories that must never be searched when resolving a bare basename.
SKIP_DIRS = {".venv", "node_modules", ".git", "tmp", "dist", "__pycache__",
             ".pytest_cache", "build", "site-packages"}

# Bare basenames whose glob is ambiguous. Everything else resolves by a unique
# repo-wide glob, so this map only grows when a genuine collision appears.
BASENAME_OVERRIDES = {
    "engine.py": "src/agentic_trading/decision/engine.py",   # not backtest/engine.py
    "trades.py": "src/agentic_trading/dashboard/trades.py",  # not backtest/trades.py
    "data.py": "src/agentic_trading/backtest/data.py",
}

# `path/to/file.ext:12` or `...:12-34`, as written inside backticks.
CITATION = re.compile(r"`([A-Za-z_][\w./-]*\.(?:py|ts|tsx|css|md|yaml|json|cmd)):(\d+)(?:-(\d+))?`")

# A line worth anchoring on: enough substance to be findable again.
MIN_ANCHOR_CHARS = 8


@dataclass
class Anchor:
    citation: str        # exactly as it appears in the doc, e.g. "run.py:1446-1451"
    path: str            # repo-relative resolved path
    start: int           # 1-indexed line recorded in the doc
    span: int            # end - start (0 for single-line citations)
    anchor_offset: int   # distance from `start` to the line we fingerprinted
    anchor_text: str     # that line, stripped


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def resolve(cited: str) -> Path | None:
    """Map a citation's path fragment onto a real file.

    Citations are written the way a human refers to code — sometimes
    `run.py`, sometimes `journal/logger.py`, sometimes `tests/conftest.py` —
    so try the literal path, then a suffix match, then a unique basename glob.
    """
    direct = ROOT / cited
    if direct.is_file():
        return direct

    override = BASENAME_OVERRIDES.get(Path(cited).name)
    if override and "/" not in cited:
        candidate = ROOT / override
        if candidate.is_file():
            return candidate

    matches = [
        p for p in ROOT.rglob(Path(cited).name)
        if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)
    ]
    if "/" in cited:  # narrow by the directory fragment the author supplied
        suffix = cited.replace("\\", "/")
        narrowed = [p for p in matches if p.as_posix().endswith(suffix)]
        if narrowed:
            matches = narrowed
    if len(matches) == 1:
        return matches[0]
    return None


def pick_anchor(lines: list[str], start: int, end: int) -> tuple[int, str] | None:
    """First substantive line in [start, end] — 1-indexed, inclusive."""
    for lineno in range(start, min(end, len(lines)) + 1):
        text = lines[lineno - 1].strip()
        if len(text) >= MIN_ANCHOR_CHARS and any(c.isalnum() for c in text):
            return lineno, text
    return None


def find_anchor(lines: list[str], text: str, near: int) -> tuple[int, int]:
    """Locate `text` in `lines`. Returns (lineno, n_matches); lineno 0 = absent.

    When a line appears more than once the closest one to where the doc said
    it was wins — renaming a helper should not make a citation jump across the
    file to an identical-looking line.
    """
    hits = [i + 1 for i, raw in enumerate(lines) if raw.strip() == text]
    if not hits:
        return 0, 0
    return min(hits, key=lambda n: abs(n - near)), len(hits)


def snapshot(docs: list[Path]) -> int:
    anchors: list[dict] = []
    unresolved: list[str] = []
    for doc in docs:
        content = doc.read_text(encoding="utf-8")
        seen: set[str] = set()
        for match in CITATION.finditer(content):
            cited, start_s, end_s = match.group(1), match.group(2), match.group(3)
            key = f"{cited}:{start_s}" + (f"-{end_s}" if end_s else "")
            if key in seen:
                continue
            seen.add(key)
            path = resolve(cited)
            if path is None:
                unresolved.append(key)
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            start = int(start_s)
            end = int(end_s) if end_s else start
            if start > len(lines):
                unresolved.append(f"{key} (file has only {len(lines)} lines)")
                continue
            picked = pick_anchor(lines, start, end)
            if picked is None:
                unresolved.append(f"{key} (no substantive line in range)")
                continue
            anchor_lineno, anchor_text = picked
            anchors.append(asdict(Anchor(
                citation=key,
                path=path.relative_to(ROOT).as_posix(),
                start=start,
                span=end - start,
                anchor_offset=anchor_lineno - start,
                anchor_text=anchor_text,
            )))

    ANCHOR_STORE.write_text(json.dumps(
        {"commit": git_commit(), "docs": [d.name for d in docs], "anchors": anchors},
        indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Recorded {len(anchors)} anchors from {len(docs)} doc(s) at commit {git_commit()}"
          f" -> {ANCHOR_STORE.name}")
    for item in unresolved:
        print(f"  UNRESOLVED  {item}")
    return 1 if unresolved else 0


def check(fix: bool) -> int:
    if not ANCHOR_STORE.is_file():
        print(f"No {ANCHOR_STORE.name}. Run: python {Path(__file__).name} --snapshot")
        return 2
    store = json.loads(ANCHOR_STORE.read_text(encoding="utf-8"))
    baseline, now = store.get("commit", "unknown"), git_commit()

    ok: list[str] = []
    moved: list[tuple[Anchor, int]] = []
    missing: list[Anchor] = []
    ambiguous: list[str] = []

    for raw in store["anchors"]:
        anc = Anchor(**raw)
        path = ROOT / anc.path
        if not path.is_file():
            missing.append(anc)
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        found, n_hits = find_anchor(lines, anc.anchor_text, anc.start + anc.anchor_offset)
        if found == 0:
            missing.append(anc)
            continue
        if n_hits > 1:
            ambiguous.append(f"{anc.citation} ({n_hits} identical lines; took the nearest)")
        new_start = found - anc.anchor_offset
        if new_start == anc.start:
            ok.append(anc.citation)
        else:
            moved.append((anc, new_start))

    print(f"handoff anchors — baseline {baseline}, working tree {now}")
    print(f"  OK       {len(ok)}")
    print(f"  MOVED    {len(moved)}")
    print(f"  MISSING  {len(missing)}")
    for note in ambiguous:
        print(f"  ambiguous: {note}")

    if moved:
        print("\nMOVED — the code is intact, the citation just points at the wrong line:")
        for anc, new_start in moved:
            new_cit = f"{anc.citation.rsplit(':', 1)[0]}:{new_start}"
            if anc.span:
                new_cit += f"-{new_start + anc.span}"
            print(f"  {anc.citation:38} -> {new_cit:38} {anc.anchor_text[:56]}")
    if missing:
        print("\nMISSING — the anchored code is gone; the PROSE needs a human, not a renumber:")
        for anc in missing:
            print(f"  {anc.citation:38} {anc.path}")
            print(f"    was: {anc.anchor_text[:78]}")

    if fix and moved:
        docs = [ROOT / name for name in store.get("docs", DEFAULT_DOCS)]
        edits = 0
        for doc in docs:
            if not doc.is_file():
                continue
            text = doc.read_text(encoding="utf-8")
            for anc, new_start in moved:
                new_cit = f"{anc.citation.rsplit(':', 1)[0]}:{new_start}"
                if anc.span:
                    new_cit += f"-{new_start + anc.span}"
                before = text
                text = text.replace(f"`{anc.citation}`", f"`{new_cit}`")
                if text != before:
                    edits += 1
            doc.write_text(text, encoding="utf-8")
        print(f"\n--fix: rewrote {edits} citation(s). Re-run --snapshot to re-baseline.")

    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", action="store_true",
                        help="record today's anchors as the baseline")
    parser.add_argument("--fix", action="store_true",
                        help="rewrite moved citations in the doc")
    parser.add_argument("docs", nargs="*", default=None,
                        help=f"markdown files to scan (default: {' '.join(DEFAULT_DOCS)})")
    args = parser.parse_args(argv)

    # Windows consoles default to a legacy codepage that mangles the em dashes
    # in the output below; same fix as run.py:104-118.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.snapshot:
        names = args.docs or DEFAULT_DOCS
        docs = [ROOT / n for n in names]
        for doc in docs:
            if not doc.is_file():
                print(f"No such doc: {doc}")
                return 2
        return snapshot(docs)
    return check(fix=args.fix)


if __name__ == "__main__":
    raise SystemExit(main())
