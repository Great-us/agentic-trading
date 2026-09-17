"""Live cockpit helpers: JSONL tail, shadow quant, schedule, progress.

Read-only. Never calls fetch_price_history() — parquet via cache_path() name
mapping into the book's data/cache, live last price via AlpacaFeed GET-only.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..data.market_data import cache_path
from ..decision.engine import decide
from ..live_events import events_path, next_scheduled_slot, read_tail
from ..signals.macro import RATIOS, VIX_TICKER, assess_regime
from ..signals.technical import DEFAULT_WEIGHTS, QuantSignal, compute_signal
from ..broker_read import BookBrokerReader, BrokerError, _load_credentials
from .registry import Book
from .views import connect_ro, logic_payload, position_snapshot

log = logging.getLogger(__name__)

MACRO_TICKERS = tuple(dict.fromkeys(
    [t for _, num, den, _ in RATIOS for t in (num, den)] + [VIX_TICKER]
))


class ParquetFeed:
    """Read-only parquet feed. Empty frame on a miss — never fetches."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self._memo: dict[str, pd.DataFrame] = {}

    def price_history(self, symbol: str, asof=None, period: str | None = None) -> pd.DataFrame:
        if symbol in self._memo:
            return self._memo[symbol]
        path = self.cache_dir / cache_path(symbol).name
        if not path.is_file():
            df = pd.DataFrame()
        else:
            try:
                df = pd.read_parquet(path)
            except Exception:
                log.exception("parquet read failed for %s", path)
                df = pd.DataFrame()
        self._memo[symbol] = df
        return df


def sse_pack(event: str, data) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def _clip(x: float) -> float:
    return float(max(-1.0, min(1.0, x)))


def _components(signal: QuantSignal, df: pd.DataFrame) -> dict[str, float]:
    last_price = signal.last_price
    sma20, sma50 = signal.sma20, signal.sma50
    trend = _clip((last_price - sma50) / sma50 * 5) if sma50 else 0.0
    cross = _clip((sma20 - sma50) / sma50 * 10) if sma50 else 0.0
    momentum = _clip(signal.momentum_20d_pct / 10)
    rsi = _clip((50 - signal.rsi14) / 50)
    macd = 0.0
    if not df.empty and last_price and "Close" in df.columns:
        close = df["Close"]
        ema_fast = close.ewm(span=12, adjust=False).mean()
        ema_slow = close.ewm(span=26, adjust=False).mean()
        hist = (ema_fast - ema_slow) - (ema_fast - ema_slow).ewm(span=9, adjust=False).mean()
        last_hist = float(hist.iloc[-1])
        macd = _clip((last_hist / last_price) * 100)
    return {
        "trend": round(trend, 4),
        "cross": round(cross, 4),
        "momentum": round(momentum, 4),
        "macd": round(macd, 4),
        "rsi": round(rsi, 4),
    }


def _cache_asof(cache_dir: Path, symbols: list[str]) -> str | None:
    latest: float | None = None
    for symbol in symbols:
        path = cache_dir / cache_path(symbol).name
        try:
            mtime = path.stat().st_mtime if path.is_file() else None
        except OSError:
            mtime = None
        if mtime is not None:
            latest = mtime if latest is None else max(latest, mtime)
    if latest is None:
        return None
    return datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()


def _alpaca_last_closes(book: Book, symbols: list[str]) -> tuple[dict[str, float], str | None]:
    try:
        key, secret, _base = _load_credentials(book.root)
    except BrokerError as exc:
        return {}, str(exc)
    try:
        from ..data.alpaca_feed import AlpacaFeed

        wanted = [s for s in symbols if not str(s).startswith("^")]
        feed = AlpacaFeed(key, secret, drop_forming=False)
        feed.prefetch(wanted)
        out: dict[str, float] = {}
        for symbol in wanted:
            df = feed._bars.get(symbol)
            if df is None or df.empty or "Close" not in df.columns:
                continue
            out[symbol] = round(float(df["Close"].iloc[-1]), 4)
        return out, None
    except Exception as exc:  # noqa: BLE001 — degrade honestly
        return {}, f"{type(exc).__name__}: {exc}"


_IN_CYCLE_STAGES = frozenset({
    "cycle_start", "regime", "symbol_enter", "quant",
    "llm_call_start", "llm_call_done", "llm_circuit_open",
    "decide", "sizing", "order_or_veto",
})


def analyzed_progress(conn: sqlite3.Connection, watchlist: list[str],
                      events: list[dict]) -> dict:
    """In-flight progress from JSONL; llm_escalations is a cheap fallback.

    Do not require `cycle_start` to still be in the tail: a 14-name deep cycle
    emits more than 80 lines before cycle_end, and the 80-line slice used by
    build_meta would otherwise flip running=False mid-cycle.
    """
    total = len(watchlist)
    last_start_i = last_end_i = None
    for i, ev in enumerate(events):
        st = ev.get("stage")
        if st == "cycle_start":
            last_start_i = i
        elif st == "cycle_end":
            last_end_i = i
    if last_start_i is not None:
        running = last_end_i is None or last_start_i > last_end_i
        window = events[last_start_i:] if running else []
    else:
        last = events[-1] if events else None
        running = bool(last) and last.get("stage") in _IN_CYCLE_STAGES
        window = events if running else []

    current_symbol = None
    llm_syms: set[str] = set()
    cycle = None
    n_from_start = None
    stage = window[-1].get("stage") if window else (events[-1].get("stage") if events else None)
    for ev in window:
        st = ev.get("stage")
        if ev.get("cycle") is not None:
            cycle = ev.get("cycle")
        if st == "cycle_start" and ev.get("n_symbols") is not None:
            n_from_start = ev.get("n_symbols")
        if st == "symbol_enter" and ev.get("symbol"):
            current_symbol = ev.get("symbol")
        if st in ("llm_call_start", "llm_call_done") and ev.get("symbol"):
            llm_syms.add(str(ev["symbol"]))
    analyzed = len(llm_syms)
    if not running:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM llm_escalations WHERE last_escalated_at >= ?",
                (cutoff,),
            ).fetchone()
            escalated = int(row[0]) if row else 0
        except sqlite3.Error:
            escalated = 0
        if analyzed == 0:
            analyzed = escalated
    try:
        total = int(n_from_start) if n_from_start else total
    except (TypeError, ValueError):
        pass
    return {
        "running": running,
        "analyzed": analyzed,
        "total": total,
        "current_symbol": current_symbol if running else None,
        "stage": stage,
        "cycle": cycle,
    }


def build_meta(book: Book, reader: BookBrokerReader | None = None) -> dict:
    path = events_path(book.root)
    events = read_tail(path)
    logic = logic_payload(book.config_dir)
    watchlist = logic.get("watchlist") or []
    progress = {
        "running": False, "analyzed": 0, "total": len(watchlist),
        "current_symbol": None, "stage": None, "cycle": None,
    }
    if book.db_path.exists():
        conn = connect_ro(book.db_path)
        try:
            progress = analyzed_progress(conn, watchlist, events)
        finally:
            conn.close()
    clock, clock_reason = (None, "no reader")
    if reader is not None:
        clock, clock_reason = reader.clock()
    if book.kind == "llm_book":
        from ..progress import next_job_p3, read_progress
        card = read_progress(book.root / "data" / "progress.json")
        last = (card or {}).get("round", {}).get("name") or "evening"
        status = (card or {}).get("round", {}).get("status") or "ok"
        job = next_job_p3(last_round=last, status=status)
        schedule = {
            "next": job.get("when"),
            "kind": job.get("slot"),
            "seconds": None,
            "next_deep": job.get("when"),
            "next_fast": None,
            "instruction": job.get("instruction"),
        }
        pipeline = [["交班", job.get("instruction") or ""],
                    ["执行", "reconciler + flatten 不过 LLM"]]
    else:
        schedule = next_scheduled_slot()
        pipeline = logic.get("pipeline") or []
    return {
        "schedule": schedule,
        "clock": clock,
        "clock_reason": clock_reason,
        "progress": progress,
        "pipeline": pipeline,
    }


def shadow_signals(book: Book, reader: BookBrokerReader | None = None) -> dict:
    if book.kind == "llm_book":
        holdings: list[dict] = []
        reasons: list[str] = []
        if reader is not None:
            live, live_reason = reader.positions()
            if live is None:
                reasons.append(live_reason or "broker positions unavailable")
            else:
                holdings = [{**pos, "symbol": str(pos.get("symbol") or "").upper(),
                             "source": "broker"} for pos in live]
        return {
            "regime": {"label": "n/a", "score": 0.0, "vix": None,
                       "components": {}, "notes": []},
            "buy_threshold": None,
            "min_quant_score_to_consider": None,
            "weights": {},
            "signals": [],
            "holdings": holdings,
            "degraded": bool(reasons),
            "degraded_reasons": reasons,
            "cache_asof": None,
            "live_prices_ok": False,
            "note": "三号盘不跑 watchlist 影子量化；看交班卡与券商持仓。",
        }
    logic = logic_payload(book.config_dir)
    watchlist = [str(s).upper() for s in (logic.get("watchlist") or [])]
    risk = logic.get("risk") or {}
    buy_threshold = float(risk.get("buy_threshold") or 0.35)
    sell_threshold = float(risk.get("sell_threshold") or -0.25)
    min_quant = float(risk.get("min_quant_score_to_consider") or 0.15)
    penalty = float(risk.get("risk_off_score_penalty") or 0.0)
    cache_dir = book.root / "data" / "cache"
    feed = ParquetFeed(cache_dir)
    reasons: list[str] = []

    regime = assess_regime(feed=feed)
    if regime.label == "unknown":
        reasons.append("regime unknown — macro parquet missing or too short")

    held: set[str] = set()
    holdings: list[dict] = []
    if reader is not None:
        live, live_reason = reader.positions()
        if live is None:
            reasons.append(live_reason or "broker positions unavailable")
            if book.db_path.exists():
                conn = connect_ro(book.db_path)
                try:
                    for row in position_snapshot(conn):
                        symbol = str(row.get("symbol") or "").upper()
                        held.add(symbol)
                        holdings.append({**row, "symbol": symbol, "source": "journal"})
                finally:
                    conn.close()
        else:
            for pos in live:
                symbol = str(pos.get("symbol") or "").upper()
                held.add(symbol)
                holdings.append({**pos, "symbol": symbol, "source": "broker"})

    prices, price_reason = _alpaca_last_closes(book, watchlist)
    if price_reason:
        reasons.append(price_reason)

    rows: list[dict] = []
    for symbol in watchlist:
        df = feed.price_history(symbol)
        signal = compute_signal(symbol, df)
        if signal is None:
            rows.append({
                "symbol": symbol,
                "score": None,
                "combined": None,
                "action": None,
                "gap_to_buy": None,
                "rsi14": None,
                "extended": None,
                "last_price": None,
                "live_price": prices.get(symbol),
                "components": None,
                "held": symbol in held,
                "skip": "insufficient parquet history",
            })
            continue
        decision = decide(
            signal=signal, verdict=None, has_open_position=symbol in held,
            min_quant_score_to_consider=min_quant, regime=regime,
            risk_off_score_penalty=penalty, buy_threshold=buy_threshold,
            sell_threshold=sell_threshold,
        )
        combined = decision.combined_score
        rows.append({
            "symbol": symbol,
            "score": round(signal.score, 4),
            "combined": round(combined, 4),
            "action": decision.action.value,
            "gap_to_buy": round(buy_threshold - combined, 4),
            "rsi14": round(signal.rsi14, 2),
            "extended": bool(signal.extended),
            "last_price": round(signal.last_price, 4),
            "live_price": prices.get(symbol),
            "components": _components(signal, df),
            "held": symbol in held,
            "skip": None,
        })

    return {
        "regime": {
            "label": regime.label,
            "score": round(regime.score, 4),
            "vix": regime.vix,
            "components": {k: round(v, 4) for k, v in (regime.components or {}).items()},
            "notes": list(regime.notes or []),
        },
        "buy_threshold": buy_threshold,
        "min_quant_score_to_consider": min_quant,
        "weights": dict(DEFAULT_WEIGHTS),
        "signals": rows,
        "holdings": holdings,
        "degraded": bool(reasons),
        "degraded_reasons": reasons,
        "cache_asof": _cache_asof(cache_dir, watchlist + list(MACRO_TICKERS)),
        "live_prices_ok": price_reason is None and bool(prices),
        "note": "影子计算：直接读 parquet（cache_path 文件名），compute_signal + "
                "assess_regime(feed=…) + decide(verdict=None)。不调用 fetch_price_history。",
    }
