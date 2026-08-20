"""A false-positive ticker here means the trading system could analyze or
trade the wrong instrument, so extraction correctness matters more than
coverage — these tests lean toward "did we avoid picking up garbage" as much
as "did we find the real ticker"."""
from pathlib import Path

from agentic_trading.research.obsidian_scanner import scan_vault


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_extracts_frontmatter_ticker(tmp_path):
    _write(tmp_path / "notes" / "Nebius.md", """---
ticker: NBIS
公司: Nebius Group
---
# Nebius
""")
    result = scan_vault(tmp_path)
    assert result.symbols == ["NBIS"]
    assert result.tickers[0].method == "frontmatter"


def test_extracts_filename_ticker_when_no_frontmatter_field(tmp_path):
    _write(tmp_path / "notes" / "01-IQVIA-IQV.md", "# IQVIA deep dive\nno frontmatter ticker field here\n")
    result = scan_vault(tmp_path)
    assert result.symbols == ["IQV"]
    assert result.tickers[0].method == "filename"


def test_does_not_pick_up_tags_as_tickers(tmp_path):
    # Tags mix real acronyms (CRO = Contract Research Organization, but ALSO a
    # real ticker for something unrelated) with the actual subject ticker.
    # Only the explicit ticker: field should be trusted, not the tag list.
    _write(tmp_path / "notes" / "01-IQVIA-IQV.md", """---
title: IQVIA deep dive
tags: [投资研究, CRO, 医疗数据, IQV, Serenity]
---
# IQVIA
""")
    result = scan_vault(tmp_path)
    assert result.symbols == ["IQV"]  # from filename, not "CRO" from tags


def test_index_and_summary_files_produce_no_ticker(tmp_path):
    _write(tmp_path / "notes" / "00-索引与研究方法.md", "# index\n")
    _write(tmp_path / "notes" / "06-美股市场情绪与仓位环境.md", "# market sentiment summary\n")
    result = scan_vault(tmp_path)
    assert result.symbols == []


def test_known_acronyms_excluded_from_filename_heuristic(tmp_path):
    _write(tmp_path / "notes" / "some-research-ETF.md", "# generic ETF note\n")
    result = scan_vault(tmp_path)
    assert result.symbols == []


def test_deduplicates_same_ticker_across_files(tmp_path):
    _write(tmp_path / "notes" / "a.md", "---\nticker: VHT\n---\n")
    _write(tmp_path / "notes" / "b.md", "---\nticker: VHT\n---\n")
    result = scan_vault(tmp_path)
    assert result.symbols == ["VHT"]
    assert len(result.tickers) == 2  # both occurrences still recorded, just deduped in .symbols


def test_restricts_to_specified_subfolders(tmp_path):
    _write(tmp_path / "included" / "a.md", "---\nticker: AAA\n---\n")
    _write(tmp_path / "excluded" / "b.md", "---\nticker: BBB\n---\n")
    result = scan_vault(tmp_path, subfolders=["included"])
    assert result.symbols == ["AAA"]


def test_missing_vault_returns_empty_not_an_error(tmp_path):
    result = scan_vault(tmp_path / "does-not-exist")
    assert result.symbols == []


def test_missing_subfolder_is_skipped_not_an_error(tmp_path):
    _write(tmp_path / "present" / "a.md", "---\nticker: AAA\n---\n")
    result = scan_vault(tmp_path, subfolders=["present", "absent"])
    assert result.symbols == ["AAA"]


def test_lowercase_frontmatter_ticker_is_uppercased(tmp_path):
    _write(tmp_path / "notes" / "a.md", "---\nticker: nbis\n---\n")
    result = scan_vault(tmp_path)
    assert result.symbols == ["NBIS"]


def test_non_markdown_files_are_ignored(tmp_path):
    _write(tmp_path / "notes" / "readme.txt", "ticker: XXX\n")
    result = scan_vault(tmp_path)
    assert result.symbols == []


# --- tickers: (plural array) + type: gating ---------------------------------

def test_extracts_multiple_tickers_from_plural_field(tmp_path):
    _write(tmp_path / "notes" / "N03.md", """---
title: AMD adds SpaceX position
type: news
tickers: [AMD, SPCX, NTNX, MRVL]
---
""")
    # type: news is an excluded type, so this should yield nothing — see next test
    # for the positive case with type: analysis.
    result = scan_vault(tmp_path)
    assert result.symbols == []


def test_plural_tickers_are_mentions_not_tradeable(tmp_path):
    # A Seeking Alpha analysis note naming BSX is "this article talked about
    # BSX", not "put BSX on the trading universe". Only ticker: (singular) or
    # a -XXXX.md filename can do that.
    _write(tmp_path / "notes" / "A04.md", """---
title: Boston Scientific deep dive
type: analysis
tickers: [BSX]
rating: strong buy
---
""")
    result = scan_vault(tmp_path)
    assert result.symbols == []
    assert result.mentions == ["BSX"]


def test_news_type_blocks_extraction_even_with_tickers_field(tmp_path):
    _write(tmp_path / "notes" / "N01.md", """---
title: Broadcom VMware vulnerability
type: news
tickers: [AVGO]
---
""")
    result = scan_vault(tmp_path)
    assert result.symbols == []


def test_news_type_blocks_filename_fallback_too(tmp_path):
    # Even though the filename would match the -XXXX pattern, an excluded type
    # should suppress extraction entirely, not fall back to guessing from the name.
    _write(tmp_path / "notes" / "some-note-AVGO.md", """---
type: news
---
""")
    result = scan_vault(tmp_path)
    assert result.symbols == []


def test_sector_summary_and_archive_types_are_excluded(tmp_path):
    _write(tmp_path / "notes" / "a.md", "---\ntype: sector-summary\ntickers: [XLV]\n---\n")
    _write(tmp_path / "notes" / "b.md", "---\ntype: news-archive\ntickers: [BSX]\n---\n")
    _write(tmp_path / "notes" / "c.md", "---\ntype: fetch-ledger\ntickers: [BLTE]\n---\n")
    _write(tmp_path / "notes" / "d.md", "---\ntype: daily-brief\ntickers: [SPY]\n---\n")
    result = scan_vault(tmp_path)
    assert result.symbols == []


def test_notes_without_a_type_field_are_never_excluded(tmp_path):
    # Pre-existing folders (投资研究/ETF/投资笔记) don't use type: at all and
    # must keep working exactly as before this change.
    _write(tmp_path / "notes" / "a.md", "---\nticker: NBIS\n---\n")
    result = scan_vault(tmp_path)
    assert result.symbols == ["NBIS"]


def test_rejects_non_ticker_values_seen_in_the_real_vault(tmp_path):
    # The `tickers:` (plural) field is machine-populated and has actually
    # contained an FX pair, bare index/futures codes, and a plain word that
    # happens to be all-caps — this is a regression test against exactly that.
    _write(tmp_path / "notes" / "a.md",
          "---\ntype: analysis\ntickers: [USDJPY, VIX, SPX, RTY, CO1, SP500, ANTHRO, BSX]\n---\n")
    result = scan_vault(tmp_path)
    assert result.symbols == []
    assert result.mentions == ["BSX"]  # only the one real-looking ticker is even recorded


def test_share_class_suffix_is_recorded_as_a_mention(tmp_path):
    _write(tmp_path / "notes" / "a.md", "---\ntype: analysis\ntickers: [BRK.B]\n---\n")
    result = scan_vault(tmp_path)
    assert result.symbols == []
    assert result.mentions == ["BRK.B"]


def test_singular_ticker_is_tradeable_even_when_a_plural_list_is_also_present(tmp_path):
    _write(tmp_path / "notes" / "01-IQVIA-IQV.md", """---
ticker: IQV
tickers: [IQV, ACET, BSX]
---
""")
    result = scan_vault(tmp_path)
    assert result.symbols == ["IQV"]
    assert "ACET" not in result.symbols


def test_rejects_ticker_with_digits(tmp_path):
    _write(tmp_path / "notes" / "a.md", "---\ntype: analysis\ntickers: [CO1, SP500]\n---\n")
    result = scan_vault(tmp_path)
    assert result.symbols == []


def test_rejects_overlong_value_from_singular_field_too(tmp_path):
    # ticker: (singular) is capped at 6 chars by its own regex already, but
    # confirm the shared validity check also rejects a 6-letter FX-pair-shaped
    # value rather than silently accepting anything under that length.
    _write(tmp_path / "notes" / "a.md", "---\nticker: USDJPY\n---\n")
    result = scan_vault(tmp_path)
    assert result.symbols == []


def test_one_file_contributing_multiple_mentions_all_recorded(tmp_path):
    _write(tmp_path / "notes" / "N03.md", "---\ntype: analysis\ntickers: [AMD, SPCX, NTNX, MRVL]\n---\n")
    result = scan_vault(tmp_path)
    assert result.symbols == []
    assert result.mentions == ["AMD", "MRVL", "NTNX", "SPCX"]
    assert all(t.source_file.endswith("N03.md") for t in result.tickers)


def test_unreadable_file_does_not_abort_the_scan(tmp_path, monkeypatch):
    good = tmp_path / "notes" / "a.md"
    _write(good, "---\nticker: AAA\n---\n")
    bad = tmp_path / "notes" / "b.md"
    _write(bad, "---\nticker: BBB\n---\n")

    real_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self == bad:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad encoding")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    result = scan_vault(tmp_path)
    assert result.symbols == ["AAA"]
