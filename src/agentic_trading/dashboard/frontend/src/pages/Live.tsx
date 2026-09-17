import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api, LiveEvent, LiveMeta, LiveSignalsPayload, pct, shortTime, usd,
} from "../api";

const STEP_COLORS = [
  "var(--accent)",
  "var(--green)",
  "var(--red)",
  "var(--amber)",
  "var(--accent)",
  "var(--green)",
  "var(--amber)",
  "var(--green)",
];

const STAGE_STEP: Record<string, number> = {
  cycle_start: 0,
  regime: 0,
  symbol_enter: 1,
  quant: 1,
  llm_call_start: 3,
  llm_call_done: 3,
  llm_circuit_open: 3,
  decide: 4,
  sizing: 5,
  order_or_veto: 6,
  cycle_end: 7,
};

const DEFAULT_PIPELINE: [string, string][] = [
  ["0 · 市场体制", ""],
  ["1 · 量化信号", ""],
  ["2 · 风控优先", ""],
  ["3 · LLM 分析", ""],
  ["4 · 决策合成", ""],
  ["5 · 风险 sizing", ""],
  ["6 · 执行窗口", ""],
  ["7 · 保护性止损", ""],
];

export default function Live({ bookId }: { bookId: string }) {
  const [params] = useSearchParams();
  const wall = params.has("wall");
  const [signals, setSignals] = useState<LiveSignalsPayload | null>(null);
  const [meta, setMeta] = useState<LiveMeta | null>(null);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      api.liveSignals(bookId).then((d) => { if (!cancelled) setSignals(d); })
        .catch((e) => { if (!cancelled) setError(String(e)); });
    };
    load();
    const id = window.setInterval(load, 20_000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [bookId]);

  useEffect(() => {
    const src = new EventSource(api.liveStreamUrl(bookId));
    const onReplay = (ev: MessageEvent) => {
      try {
        const rows = JSON.parse(ev.data) as LiveEvent[];
        if (Array.isArray(rows)) setEvents(rows.slice(-40));
      } catch { /* ignore */ }
    };
    const onMeta = (ev: MessageEvent) => {
      try { setMeta(JSON.parse(ev.data) as LiveMeta); } catch { /* ignore */ }
    };
    const onMsg = (ev: MessageEvent) => {
      try {
        const row = JSON.parse(ev.data) as LiveEvent;
        setEvents((prev) => [...prev.slice(-79), row]);
      } catch { /* ignore */ }
    };
    src.addEventListener("replay", onReplay as EventListener);
    src.addEventListener("meta", onMeta as EventListener);
    src.addEventListener("message", onMsg as EventListener);
    src.onerror = () => { /* EventSource reconnects */ };
    return () => src.close();
  }, [bookId]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const pipeline = meta?.pipeline?.length ? meta.pipeline : DEFAULT_PIPELINE;
  const running = Boolean(meta?.progress.running);
  const lastEvent = events.length ? events[events.length - 1] : null;
  const activeStep = running
    ? STAGE_STEP[String(lastEvent?.stage || meta?.progress.stage || "cycle_start")] ?? 0
    : 1;
  const currentSymbol = meta?.progress.current_symbol
    || (lastEvent && running ? String(lastEvent.symbol || "") : "")
    || null;

  const countdown = useMemo(() => {
    const sec = meta?.schedule.seconds;
    if (sec == null) return "—";
    return fmtDuration(sec);
  }, [meta, now]);

  return (
    <div className={wall ? "live-page wall" : "live-page"}>
      {wall && (
        <div className="live-wall-brand">
          Agentic Trading · {bookId.toUpperCase()} 实况
          <Link to={`/${bookId}/live`} className="live-wall-exit">退出大屏</Link>
        </div>
      )}

      <div className={`health-bar ${running ? "ok" : "idle"}`}>
        <strong>{running ? "周期进行中" : "空闲 · 影子分数在刷新"}</strong>
        {running && currentSymbol && (
          <span>正在分析 <b>{currentSymbol}</b></span>
        )}
        {running && meta && (
          <span className="muted">
            {meta.progress.analyzed}/{meta.progress.total}
            {meta.progress.stage ? ` · ${meta.progress.stage}` : ""}
          </span>
        )}
        <span className="muted">
          市场 {meta?.clock?.is_open ? "开市" : meta?.clock ? "闭市" : (meta?.clock_reason ? "时钟不可用" : "…")}
        </span>
        <span className="muted">
          下次 {meta?.schedule.kind ?? "—"} {countdown}
        </span>
        {!wall && <Link to="?wall" className="live-wall-link">大屏</Link>}
      </div>

      {error && <div className="warn-box">{error}</div>}
      {signals?.degraded && (
        <div className="warn-box">{signals.degraded_reasons.join(" · ")}</div>
      )}

      <Pipeline
        steps={pipeline}
        active={activeStep}
        running={running}
        idleGlow={1}
      />

      <div className="grid2">
        <div className="panel">
          <h3>影子量化 · 距买入阈值 {signals?.buy_threshold != null ? signals.buy_threshold.toFixed(2) : "—"}</h3>
          <div className="muted" style={{ marginBottom: 10 }}>
            体制 {signals?.regime.label ?? "—"}
            {signals?.regime.score != null ? ` ${signals.regime.score >= 0 ? "+" : ""}${signals.regime.score.toFixed(2)}` : ""}
            {signals?.regime.vix != null ? ` · VIX ${signals.regime.vix.toFixed(1)}` : ""}
            {signals?.cache_asof ? ` · 缓存 ${shortTime(signals.cache_asof)}` : ""}
          </div>
          {!signals && <p className="muted">加载影子分数…</p>}
          {signals && (
            <table>
              <thead>
                <tr>
                  <th>标的</th><th>quant</th><th>距买入</th><th>动作</th>
                  <th>RSI</th><th>现价</th>
                </tr>
              </thead>
              <tbody>
                {signals.signals.map((row) => {
                  const gap = row.gap_to_buy;
                  const hot = running && currentSymbol === row.symbol;
                  return (
                    <tr key={row.symbol} className={hot ? "live-row-hot" : undefined}>
                      <td>
                        {row.symbol}
                        {row.held ? <span className="badge hold" style={{ marginLeft: 6 }}>持仓</span> : null}
                      </td>
                      <td className={`mono ${(row.score ?? 0) >= 0 ? "pos" : "neg"}`}>
                        {row.score != null ? row.score.toFixed(2) : "—"}
                      </td>
                      <td className={`mono ${gap == null ? "" : gap <= 0 ? "pos" : "neg"}`}>
                        {gap == null ? "—" : (gap <= 0 ? "过线" : `差 ${gap.toFixed(2)}`)}
                      </td>
                      <td>{row.action ? <span className={`badge ${row.action}`}>{row.action}</span> : (row.skip || "—")}</td>
                      <td className="mono">{row.rsi14 != null ? row.rsi14.toFixed(0) : "—"}</td>
                      <td className="mono">{usd(row.live_price ?? row.last_price, 2)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          <p className="note">{signals?.note}</p>
        </div>

        <div className="panel">
          <h3>持仓盯市</h3>
          {(!signals || signals.holdings.length === 0) && <p className="muted">无持仓，或券商不可用。</p>}
          {signals && signals.holdings.length > 0 && (
            <table>
              <thead>
                <tr><th>标的</th><th>市值</th><th>浮盈亏</th><th>现价</th></tr>
              </thead>
              <tbody>
                {signals.holdings.map((h) => (
                  <tr key={h.symbol}>
                    <td>{h.symbol}</td>
                    <td className="mono">{usd(h.market_value, 0)}</td>
                    <td className={`mono ${(h.unrealized_pl ?? 0) >= 0 ? "pos" : "neg"}`}>
                      {usd(h.unrealized_pl, 2)} {h.unrealized_plpc != null ? pct(h.unrealized_plpc) : ""}
                    </td>
                    <td className="mono">{usd(h.current_price ?? h.implied_last_price, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h3 style={{ marginTop: 18 }}>事件流</h3>
          <div className="live-ticker">
            {events.slice(-12).reverse().map((ev, i) => (
              <div key={`${ev.t}-${ev.stage}-${i}`} className="live-tick">
                <span className="muted mono">{(ev.t || "").slice(11, 19)}</span>
                <span className="live-stage">{ev.stage}</span>
                {ev.symbol ? <b>{String(ev.symbol)}</b> : null}
                {ev.score != null ? <span className="mono">{Number(ev.score).toFixed(2)}</span> : null}
                {ev.action != null ? <span className={`badge ${String(ev.action)}`}>{String(ev.action)}</span> : null}
                {ev.stance != null ? <span className="muted">{String(ev.stance)}</span> : null}
                {ev.error != null ? <span className="neg">{String(ev.error)}</span> : null}
              </div>
            ))}
            {events.length === 0 && <p className="muted">还没有周期事件。空闲时看左边影子分数。</p>}
          </div>
        </div>
      </div>
    </div>
  );
}

function fmtDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm ? `${h}h${rm}m` : `${h}h`;
}

function Pipeline({
  steps, active, running, idleGlow,
}: {
  steps: [string, string][];
  active: number;
  running: boolean;
  idleGlow: number;
}) {
  const n = 8;
  const w = 1100;
  const h = 150;
  const pad = 70;
  const span = w - pad * 2;
  const ys = 64;
  const nodes = steps.slice(0, n).map((pair, i) => ({
    title: pair[0],
    x: pad + (span * i) / (n - 1),
    y: ys,
    color: STEP_COLORS[i],
  }));
  const d = nodes.map((nd, i) => `${i === 0 ? "M" : "L"} ${nd.x} ${nd.y}`).join(" ");
  const lit = running ? active : idleGlow;

  return (
    <div className="panel live-pipeline">
      <h3>决策管线{running ? " · 逐段点亮" : " · 空闲（影子量化在跳）"}</h3>
      <svg viewBox={`0 0 ${w} ${h}`} className="live-svg" role="img" aria-label="8 步决策管线">
        <path d={d} className="live-wire" />
        <path d={d} className={`live-flow ${running ? "on" : ""}`} />
        {running && (
          <circle r="4" fill="var(--accent)">
            <animateMotion dur="4s" repeatCount="indefinite" path={d} />
          </circle>
        )}
        {nodes.map((nd, i) => {
          const on = i <= lit;
          const here = i === lit;
          return (
            <g key={nd.title}>
              <circle
                cx={nd.x} cy={nd.y} r={here ? 16 : 13}
                fill="var(--panel)"
                stroke={on ? nd.color : "var(--border)"}
                strokeWidth={here ? 3 : 1.5}
                className={here ? "live-node-here" : on ? "live-node-on" : "live-node"}
              />
              <text x={nd.x} y={nd.y + 4} textAnchor="middle"
                fill={on ? nd.color : "var(--muted)"} fontSize="11" fontWeight="700">
                {i}
              </text>
              <text x={nd.x} y={nd.y + 36} textAnchor="middle"
                fill={on ? "var(--text)" : "var(--muted)"} fontSize="11">
                {nd.title.replace(/^\d+\s*·\s*/, "")}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
