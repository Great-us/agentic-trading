"""Check that the shared engine has not silently drifted between the books.

P1 (this directory), Trading-P2, Trading-P3 and Trading-P4 share ~11k lines by design
(independent directories so the OS cycle locks never collide), which means
every engine bugfix must land in every book that carries the file. This script
makes any divergence visible; run it weekly or before trusting a backtest from
any side.

Usage:
    python check_p2_sync.py            # exit 0 = in sync, 1 = drift found

What is compared, across every book whose directory exists (missing books are
skipped with a note):

- src/agentic_trading/**/*.py — line endings normalized (\\r\\n / \\r -> \\n)
  before comparison, so a CRLF checkout never false-alarms. A file owned by
  more than one book must be identical in all of them; a file owned by a
  single book must be on that book's expected-only list or it is drift.
- config/risk.yaml — byte-for-byte, deliberately WITHOUT line-ending
  normalization: the frozen risk contract must be identical, endings included.

Expected-only per book: P1 may solely own dashboard/, P2 may solely own
p2_roster.py, P3 may solely own dashboard/ and p3/, and P4 may solely own
dashboard/ and p4/. Everything outside src/ and config/risk.yaml (data/,
logs/, research/, other config) is supposed to differ between the books.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

IGNORE = {"__pycache__"}


@dataclass(frozen=True)
class Book:
    name: str
    root: Path
    # Files this book may own exclusively — never count as drift:
    expected_only_prefixes: tuple[str, ...] = ()
    expected_only_files: frozenset[str] = frozenset()


BOOKS = (
    Book("P1", HERE, expected_only_prefixes=("dashboard/",)),
    Book("P2", HERE.parent / "Trading-P2", expected_only_files=frozenset({"p2_roster.py"})),
    Book("P3", HERE.parent / "Trading-P3", expected_only_prefixes=("dashboard/", "p3/")),
    Book("P4", HERE.parent / "Trading-P4", expected_only_prefixes=("dashboard/", "p4/")),
)


def walk(root: Path) -> dict[str, Path]:
    return {
        p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*.py"))
        if not (set(p.parts) & IGNORE)
    }


def normalized_source(path: Path) -> bytes:
    """.py comparison ignores line-ending style (CRLF vs LF checkout)."""
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def is_expected_only(book: Book, name: str) -> bool:
    return name in book.expected_only_files or name.startswith(book.expected_only_prefixes)


def main() -> int:
    books: list[Book] = []
    for book in BOOKS:
        if book.root.is_dir():
            books.append(book)
        else:
            print(f"  note: {book.name} not found at {book.root} - skipped")
    if len(books) < 2:
        print("Fewer than two books present - nothing to compare.")
        return 0

    trees = {b.name: walk(b.root / "src" / "agentic_trading") for b in books}
    drift: list[str] = []

    all_names = sorted(set().union(*trees.values()))
    for name in all_names:
        owners = [b for b in books if name in trees[b.name]]
        if len(owners) == 1:
            book = owners[0]
            if is_expected_only(book, name):
                print(f"  expected ({book.name} only)  : {name}")
            else:
                drift.append(f"  only in {book.name}  : {name}")
        else:
            variants = {normalized_source(trees[b.name][name]) for b in owners}
            if len(variants) > 1:
                drift.append(
                    f"  DIFFERS      : {name} ({' vs '.join(b.name for b in owners)})"
                )

    # config/risk.yaml is the frozen risk contract: byte-exact across books,
    # line endings included. Report a mismatch, never normalize it away.
    risk_bytes: dict[str, bytes] = {}
    for b in books:
        path = b.root / "config" / "risk.yaml"
        if path.is_file():
            risk_bytes[b.name] = path.read_bytes()
        else:
            drift.append(f"  risk.yaml missing in {b.name}")
    if len(set(risk_bytes.values())) > 1:
        drift.append(
            f"  DIFFERS      : config/risk.yaml ({' vs '.join(risk_bytes)})"
            " - byte compare, check line endings"
        )

    if drift:
        print(f"DRIFT: {len(drift)} finding(s) across {', '.join(b.name for b in books)}")
        print("\n".join(drift))
        print("\nEvery shared-file change must be applied to all books that carry it")
        print("(or ported via git: git -C Trading-P2/-P3 diff/apply against this baseline).")
        return 1
    shared = sum(1 for n in all_names if sum(n in trees[b.name] for b in books) > 1)
    print(
        f"In sync: {shared} shared files identical across "
        f"{', '.join(b.name for b in books)}; config/risk.yaml byte-identical."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
