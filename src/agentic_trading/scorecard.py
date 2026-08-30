"""Shadow scoring of the LLM layer against the full decision history.

Pure observation: reads the journal read-only and grades how often the LLM
vetoed a qualifying quant signal, rescued a sub-threshold one, and simply
agreed with the quant score's direction. Post-hoc returns are attached for
context. Nothing here feeds back into trading decisions.

Usage:
    python -m agentic_trading.scorecard                  # default db + output path
    python -m agentic_trading.scorecard --db PATH --out PATH [--threshold X]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .config import load_settings
from .data.feed import YFinanceFeed
from .journal.logger import DEFAULT_DB_PATH

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "research"

# Fixed disclaimer required at the top of every generated report.
DISCLOSURE = "本报告是相关性观察，不是调参依据；两本书均处观察期，6 个月判读协议不变。"

# Discipline rule: any bucket smaller than this shows its count and nothing else.
MIN_BUCKET_SAMPLE = 5
# Confidence split for the agreement baseline.
HIGH_CONFIDENCE = 0.6
# Forward-return horizons in trading sessions past the first post-decision session.
HORIZONS = (5, 20)
# Fallback buy threshold, used only when config/risk.yaml cannot be read.
FALLBACK_THRESHOLD = 0.35

STANCE_SIGN = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}


@dataclass
class Case:
    """One historical decision row plus its (optional) post-hoc returns."""

    decided_at: datetime
    symbol: str
    quant_score: float | None
    llm_stance: str | None
    llm_confidence: float | None
    combined_score: float | None
    action: str
    ret_5d: float | None = None
    ret_20d: float | None = None
    price_error: str | None = None


# --- classification -----------------------------------------------------------

def classify(case: Case, threshold: float) -> str | None:
    """Returns 'veto', 'rescue', or None. Buckets are mutually exclusive."""
    if (
        case.quant_score is not None
        and case.quant_score >= threshold
        and case.llm_stance == "bearish"
        and case.action != "buy"
    ):
        return "veto"
    if (
        case.combined_score is not None
        and case.quant_score is not None
        and case.combined_score >= threshold
        and case.quant_score < threshold
    ):
        return "rescue"
    return None


def llm_signed_score(stance: str | None, confidence: float | None) -> float | None:
    """stance mapped to ±1/0 times confidence; None when either side is missing."""
    if stance not in STANCE_SIGN or confidence is None:
        return None
    return STANCE_SIGN[stance] * float(confidence)


def sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def agreement_stats(cases: list[Case]) -> dict[str, tuple[int, int]]:
    """(agreements, total) over all rated cases and by the confidence bands."""
    tallies: dict[str, list[int]] = {"all": [0, 0], "high": [0, 0], "low": [0, 0]}
    for case in cases:
        llm = llm_signed_score(case.llm_stance, case.llm_confidence)
        if llm is None or case.quant_score is None:
            continue
        band = "high" if float(case.llm_confidence) >= HIGH_CONFIDENCE else "low"
        tallies["all"][1] += 1
        tallies[band][1] += 1
        if sign(llm) == sign(case.quant_score):
            tallies["all"][0] += 1
            tallies[band][0] += 1
    return {key: tuple(counts) for key, counts in tallies.items()}


# --- post-hoc returns -----------------------------------------------------------

def forward_returns(close: pd.Series, decided_at: datetime, horizons=tuple(HORIZONS)):
    """Close-to-close returns ~h sessions after the decision.

    Baseline is the first session STRICTLY AFTER the decision's calendar day —
    the decision-time close is already embedded in the signal — so horizon h
    means the close h sessions after that baseline. Missing future sessions
    yield None rather than a shorter-window stand-in.
    """
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError("price history index is not a DatetimeIndex")
    idx = close.index
    if not idx.is_monotonic_increasing:
        raise ValueError("price history index is not sorted")
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    if decided_at.tzinfo is None:
        day = decided_at.date()
    else:
        day = decided_at.astimezone(timezone.utc).date()
    start = int(idx.searchsorted(pd.Timestamp(day), side="right"))
    if start >= len(close):
        return tuple(None for _ in horizons)
    base = float(close.iloc[start])
    out: list[float | None] = []
    for h in horizons:
        pos = start + h
        if pos >= len(close) or base <= 0:
            out.append(None)
        else:
            out.append(float(close.iloc[pos]) / base - 1.0)
    return tuple(out)


def _load_price_history(symbol: str) -> pd.DataFrame:
    """Daily bars through the live feed's standard interface (patched in tests)."""
    # drop_forming=False: this report looks at settled past sessions, and a
    # dropped final bar would shorten the newest windows for no reason.
    return YFinanceFeed(drop_forming=False).price_history(symbol, period="1y")


def attach_returns(cases: list[Case]) -> bool:
    """Fills ret_5d/ret_20d per case. Returns False only when EVERY distinct
    symbol failed to load (overall outage) — single failures degrade to n/a."""
    symbols = sorted({case.symbol for case in cases})
    closes: dict[str, pd.Series] = {}
    failed: set[str] = set()
    for symbol in symbols:
        try:
            frame = _load_price_history(symbol)
            if frame is None or frame.empty or "Close" not in frame.columns:
                raise ValueError(f"no usable price history for {symbol}")
            closes[symbol] = frame["Close"]
        except Exception:
            failed.add(symbol)
    for case in cases:
        if case.symbol in failed:
            case.price_error = "price fetch failed"
            continue
        try:
            case.ret_5d, case.ret_20d = forward_returns(closes[case.symbol], case.decided_at)
        except Exception:
            case.price_error = "return computation failed"
    return (not symbols) or len(failed) < len(symbols)


# --- inputs ---------------------------------------------------------------------

def _parse_ts(raw: str) -> datetime:
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Read-only handle: the scorecard must never migrate or write the journal."""
    return sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)


def load_cases(db_path: Path) -> list[Case]:
    conn = _connect_ro(db_path)
    try:
        rows = conn.execute(
            """SELECT c.timestamp, d.symbol, d.quant_score, d.llm_stance, d.llm_confidence,
                      d.combined_score, d.action
               FROM decisions d JOIN cycles c ON c.id = d.cycle_id
               ORDER BY c.id, d.id"""
        ).fetchall()
    finally:
        conn.close()
    return [
        Case(
            decided_at=_parse_ts(ts), symbol=symbol, quant_score=quant,
            llm_stance=stance, llm_confidence=conf, combined_score=combined, action=action,
        )
        for (ts, symbol, quant, stance, conf, combined, action) in rows
    ]


def resolve_threshold(cli_value: float | None) -> tuple[float, str]:
    """Buy threshold plus a human-readable provenance note for the report."""
    if cli_value is not None:
        return cli_value, "命令行 --threshold 覆盖"
    try:
        risk = load_settings(include_research=False).risk
        return float(risk.buy_threshold), "config/risk.yaml: buy_threshold"
    except Exception:
        return FALLBACK_THRESHOLD, f"默认值 {FALLBACK_THRESHOLD}（未能读取 config/risk.yaml）"


# --- rendering --------------------------------------------------------------------

def _fmt_score(value: float | None) -> str:
    return f"{value:+.2f}" if value is not None else "n/a"


def _fmt_ret(value: float | None) -> str:
    return f"{value:+.1%}" if value is not None else "n/a"


def _fmt_rate(agreements: int, total: int) -> str:
    return f"{agreements / total:.1%}" if total else "n/a"


def _small_sample_line(count: int) -> str:
    return f"样本数 {count}（样本不足，不解读）"


def _case_table_section(title: str, definition: str, section_cases: list[Case],
                        prices_ok: bool) -> list[str]:
    lines = [f"## {title}", "", f"- 判定口径：{definition}"]
    if len(section_cases) < MIN_BUCKET_SAMPLE:
        lines.append(f"- {_small_sample_line(len(section_cases))}")
        return lines + [""]
    lines.append(f"- 样本数：{len(section_cases)}")
    if not prices_ok:
        lines.append("- 价格数据不可用")
        return lines + [""]
    lines += [
        "",
        "| 决策时间 (UTC) | symbol | quant | combined | action | 5日 | 20日 |",
        "|---|---|---:|---:|---|---:|---:|",
    ]
    for case in sorted(section_cases, key=lambda c: c.decided_at):
        lines.append(
            f"| {case.decided_at:%Y-%m-%d %H:%M} | {case.symbol} "
            f"| {_fmt_score(case.quant_score)} | {_fmt_score(case.combined_score)} "
            f"| {case.action} | {_fmt_ret(case.ret_5d)} | {_fmt_ret(case.ret_20d)} |"
        )
    return lines + [""]


def _agreement_section(cases: list[Case]) -> list[str]:
    lines = [
        "## 桶三：一致性基线（LLM 与 quant 符号一致率）",
        "",
        "- 判定口径：LLM 分数 = stance（bullish +1 / neutral 0 / bearish −1）× confidence；"
        "与 quant_score 符号相同记为一次一致。",
    ]
    stats = agreement_stats(cases)
    agree, total = stats["all"]
    if total < MIN_BUCKET_SAMPLE:
        lines.append(f"- 总体：{_small_sample_line(total)}")
    else:
        lines.append(f"- 总体：{agree}/{total} 一致（{_fmt_rate(agree, total)}）")
    for key, label in (("high", f"confidence ≥ {HIGH_CONFIDENCE}"),
                       ("low", f"confidence < {HIGH_CONFIDENCE}")):
        band_agree, band_total = stats[key]
        if band_total < MIN_BUCKET_SAMPLE:
            lines.append(f"- {label}：{_small_sample_line(band_total)}")
        else:
            lines.append(f"- {label}：{band_agree}/{band_total} 一致（{_fmt_rate(band_agree, band_total)}）")
    return lines + [""]


def render_report(cases: list[Case], threshold: float, threshold_source: str,
                  db_path: Path, prices_ok: bool = True) -> str:
    lines = [
        f"# LLM 影子计分报告 — {date.today():%Y-%m-%d}",
        "",
        f"> {DISCLOSURE}",
        "",
        f"- 数据来源：`{db_path}` decisions 表全部历史（共 {len(cases)} 行）",
        f"- 买入阈值：{threshold:.2f}（来源：{threshold_source}）",
        "- 收益口径：以决策次日首个交易日收盘为基准，其后约 5 / 20 个交易日的收盘收益；"
        "单案例数据不足或抓取失败记 n/a。",
    ]
    if not prices_ok:
        lines.append("- **价格数据不可用**：所有案例的行情抓取均失败，以下各桶不含事后收益。")
    veto = [c for c in cases if classify(c, threshold) == "veto"]
    rescue = [c for c in cases if classify(c, threshold) == "rescue"]
    lines += ["", "---", ""]
    lines += _case_table_section(
        "桶一：LLM 否决（quant 过线但 LLM 看空，且最终未买入）",
        "quant_score ≥ 阈值 且 llm_stance = 'bearish' 且 action ≠ 'buy'",
        veto, prices_ok,
    )
    lines += _case_table_section(
        "桶二：LLM 补脚（combined 过线但 quant 未过线）",
        "combined_score ≥ 阈值 且 quant_score < 阈值",
        rescue, prices_ok,
    )
    lines += _agreement_section(cases)
    return "\n".join(lines) + "\n"


# --- entry point --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="journal database path")
    parser.add_argument("--out", type=Path, default=None,
                        help=f"output markdown path (default {DEFAULT_OUT_DIR}/scorecard-YYYY-MM-DD.md)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="override the buy threshold from config/risk.yaml")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not args.db.exists():
        print(f"No journal yet at {args.db}. Run `python -m agentic_trading.run` first.")
        return

    cases = load_cases(args.db)
    threshold, threshold_source = resolve_threshold(args.threshold)
    prices_ok = attach_returns(cases)
    report = render_report(cases, threshold, threshold_source, args.db, prices_ok=prices_ok)

    out_path = args.out or DEFAULT_OUT_DIR / f"scorecard-{date.today():%Y-%m-%d}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"已写入 {out_path}（{len(cases)} 条决策，阈值 {threshold:.2f}，来源：{threshold_source}）")


if __name__ == "__main__":
    main()
