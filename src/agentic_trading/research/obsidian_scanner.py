"""Extracts candidate tickers from the user's Obsidian investment vault, so the
watchlist grows from their own research instead of a hand-maintained list.

Extraction is deliberately conservative — a false-positive ticker means the
system might analyze or trade the wrong instrument, which is worse than
missing a real one. Only two signals can put a name on the *tradeable*
universe, and only for notes whose declared `type:` (when present) marks
them as a deliberate write-up rather than a news blurb or a
summary/archive/ledger file — see _EXCLUDED_TYPES.

1. A `ticker:` (singular) field in YAML front matter — one instrument, one
   note. This is what the per-name write-ups actually use.
2. A filename ending in "-XXXX.md" where XXXX is 1-5 uppercase letters, which
   is the vault's naming convention for individual stock write-ups (e.g.
   "01-IQVIA-IQV.md" -> IQV).

`tickers:` (plural array) is recorded as a *mention* and is NOT tradeable.
Seeking Alpha Daily analysis notes stamp a `tickers:` list on every article;
those are names the article talked about, not names the user researched and
wants to trade. Letting that array into the universe previously dumped
dozens of names (including micro-cap biotechs) onto the paper account.

Tags are NOT used as a source either: a tag list like [投资研究, CRO, 医疗数据,
IQV] mixes real tickers with acronyms that are themselves valid tickers for
something else (CRO is Cronos Group, not "Contract Research Organization"
here) — too easy to pick up the wrong one.

Type filtering exists because of one specific vault folder: "Seeking Alpha
Daily" has per-article notes with a `tickers:` field on EVERY article,
including `type: news` blurbs like "AMD's 13F added SpaceX, Nutanix..." that
mention four tickers in passing — being named in someone else's 13F filing is
not a research judgment about that company. Only `type: analysis` articles
(a full write-up with an explicit rating) represent one. Notes with no `type:`
field at all (the older 投资研究/ETF/投资笔记 folders) are unaffected — they
predate this distinction and are trusted as before.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TYPE_FIELD_RE = re.compile(r'^type:\s*"?([A-Za-z-]+)"?\s*$', re.MULTILINE)
_TICKER_FIELD_RE = re.compile(r"^ticker:\s*([A-Za-z.]{1,6})\s*$", re.MULTILINE)
_TICKERS_FIELD_RE = re.compile(r"^tickers:\s*\[(.*?)\]\s*$", re.MULTILINE)
_FILENAME_TICKER_RE = re.compile(r"-([A-Z]{1,5})$")

# A syntactically valid US equity/ETF ticker: 1-5 letters, optionally with a
# 1-2 letter share-class/exchange suffix (BRK.B, POW.TO). This exists because
# the `tickers:` (plural) field is machine-populated upstream and has no
# guaranteed vocabulary — it has been observed to contain FX pairs (USDJPY),
# bare index/futures codes (VIX, SPX, RTY, CO1), and plain words that happen
# to be all-caps (ANTHRO, for "Anthropic" — a private company, not a ticker).
# This regex is a cheap first filter; it does not confirm the symbol is
# actually listed or Alpaca-tradable, only that it looks like one.
_VALID_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")


def _looks_like_a_ticker(candidate: str) -> bool:
    return bool(_VALID_TICKER_RE.match(candidate)) and candidate not in _NOT_TICKERS

# Values that are syntactically indistinguishable from a real ticker (short,
# all-caps) but aren't one. Two origins:
#   - common acronyms that could false-positive via the filename fallback
#     ("-XXXX.md")
#   - known index/futures root codes, observed coming through the `tickers:`
#     field alongside real tickers (VIX, SPX, RTY — a regex can't tell "index
#     code" from "3-letter equity ticker" by shape alone)
# This is a best-effort denylist, not a source of truth — the broker is: a
# symbol that slips past this and isn't actually tradable simply gets rejected
# at order time (handled already, see execution/broker.py) or fails to load
# any price history and is skipped (see signals/technical.py).
_NOT_TICKERS = {
    "ETF", "USD", "CEO", "CFO", "FAQ", "MOC", "AI",
    "VIX", "SPX", "RTY", "DJI", "NDX", "RUT", "DXY",
}

# Front-matter `type:` values that disqualify a note from contributing tickers
# even though it may have a ticker/tickers field. A note with no type field at
# all is never excluded by this — see module docstring.
_EXCLUDED_TYPES = {"news", "daily-brief", "sector-summary", "news-archive", "fetch-ledger"}


@dataclass
class VaultTicker:
    ticker: str
    source_file: str
    method: str  # "frontmatter" | "filename" | "mention"


@dataclass
class ScanResult:
    tickers: list[VaultTicker] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        """Deduplicated, sorted list of *tradeable* tickers (singular field or filename)."""
        return sorted({t.ticker for t in self.tickers if t.method != "mention"})

    @property
    def mentions(self) -> list[str]:
        """Names that appeared in a `tickers:` array — recorded, not tradeable."""
        return sorted({t.ticker for t in self.tickers if t.method == "mention"})


def _frontmatter_block(text: str) -> str | None:
    match = _FRONTMATTER_RE.match(text)
    return match.group(1) if match else None


def _extract_tickers_from_file(path: Path, text: str) -> list[tuple[str, str]]:
    """Returns [(ticker, method), ...] — a file can yield more than one ticker
    via a `tickers:` array (e.g. a 13F-filing note naming several companies)."""
    fm = _frontmatter_block(text)
    if fm is not None:
        type_match = _TYPE_FIELD_RE.search(fm)
        if type_match and type_match.group(1).lower() in _EXCLUDED_TYPES:
            return []

        ticker_match = _TICKER_FIELD_RE.search(fm)
        if ticker_match:
            candidate = ticker_match.group(1).upper()
            return [(candidate, "frontmatter")] if _looks_like_a_ticker(candidate) else []

        tickers_match = _TICKERS_FIELD_RE.search(fm)
        if tickers_match:
            raw = [t.strip().strip('"').strip("'").upper() for t in tickers_match.group(1).split(",") if t.strip()]
            valid = [t for t in raw if _looks_like_a_ticker(t)]
            dropped = [t for t in raw if t not in valid]
            if dropped:
                logger.info("Dropped non-ticker-looking values from %s: %s", path.name, dropped)
            # Mentions only — a plural list is "this article talked about",
            # not "the user wants this in the trading universe".
            return [(t, "mention") for t in valid]

    filename_match = _FILENAME_TICKER_RE.search(path.stem)
    if filename_match and _looks_like_a_ticker(filename_match.group(1)):
        return [(filename_match.group(1), "filename")]

    return []


def scan_vault(vault_dir: Path, subfolders: list[str] | None = None) -> ScanResult:
    """Walks the vault (or a subset of its top-level folders) for markdown notes
    and extracts candidate tickers per file. Never raises — a vault that
    doesn't exist or a folder that's missing just yields an empty result, since
    this is a nice-to-have data source, not a required one."""
    if not vault_dir.exists():
        logger.warning("Obsidian vault not found at %s", vault_dir)
        return ScanResult()

    roots = [vault_dir / f for f in subfolders] if subfolders else [vault_dir]
    result = ScanResult()

    for root in roots:
        if not root.exists():
            logger.warning("Vault subfolder not found: %s", root)
            continue
        for path in root.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Could not read %s", path)
                continue

            for ticker, method in _extract_tickers_from_file(path, text):
                result.tickers.append(VaultTicker(ticker=ticker, source_file=str(path), method=method))

    return result
