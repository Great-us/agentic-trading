"""Loads config/dashboard.yaml — the read-only list of books the dashboard shows.

Each entry points at a book's repository root; everything else (journal path,
config dir, roster archive) is derived from it. The dashboard owns no trading
configuration — this file only says where to look.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY_PATH = ROOT / "config" / "dashboard.yaml"


@dataclass(frozen=True)
class Book:
    id: str
    display_name: str
    kind: str  # "fixed_pool" | "rs_rotation"
    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "data" / "journal.db"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def rosters_dir(self) -> Path:
        return self.root / "research" / "p2" / "rosters"


def load_books(path: Path | str | None = None) -> list[Book]:
    registry_path = Path(path) if path else DEFAULT_REGISTRY_PATH
    if not registry_path.exists():
        raise FileNotFoundError(f"dashboard registry not found: {registry_path}")
    with open(registry_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    books: list[Book] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw.get("books") or []):
        book_id = str(entry.get("id") or "").strip()
        root = Path(str(entry.get("root") or "")).expanduser()
        if not book_id or book_id in seen:
            raise ValueError(f"dashboard.yaml: books[{i}] has a missing/duplicate id {book_id!r}")
        if not str(entry.get("root") or "").strip() or not root.is_dir():
            raise ValueError(f"dashboard.yaml: book {book_id!r} root does not exist: {root}")
        if not (root / "data").is_dir():
            raise ValueError(f"dashboard.yaml: book {book_id!r} has no data/ under {root}")
        seen.add(book_id)
        books.append(
            Book(
                id=book_id,
                display_name=str(entry.get("display_name") or book_id),
                kind=str(entry.get("kind") or "fixed_pool"),
                root=root,
            )
        )
    if not books:
        raise ValueError("dashboard.yaml: no books configured")
    return books


def get_book(books: list[Book], book_id: str) -> Book | None:
    for book in books:
        if book.id == book_id:
            return book
    return None
