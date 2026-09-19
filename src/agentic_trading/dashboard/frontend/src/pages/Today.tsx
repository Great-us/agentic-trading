import { useEffect, useState } from "react";
import {
  api, TodayPayload, pct, shortTime, usd,
} from "../api";

export default function Today({ bookId }: { bookId: string }) {
  const [data, setData] = useState<TodayPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.today(bookId).then(setData).catch((e) => setError(String(e)));
  }, [bookId]);

  if (error) return <div className="warn-box">{error}</div>;
  if (!data) return <div className="muted">加载今日…</div>;

  const healthCls = {
    ok: "ok", weekend: "idle", stale: "bad", missing: "bad", corrupt: "bad",
    stale_intraday: "bad", after_hours: "idle", closed_or_holiday: "idle",
    stops_unknown: "bad",
  }[data.health.status] ?? "idle";
  const binding = data.vetoes.filter((v) => v.recent > 0 || v.total > 0);
  const pool = data.holdings.filter((h) => h.bucket === "pool");
  const legacy = data.holdings.filter((h) => h.bucket === "legacy");
  const llmBook = data.book_kind === "llm_book";
  const card = data.progress?.card;
  const statusCls = {
    ok: "ok", no_trade: "idle", incomplete: "bad", failed: "bad",
  }[card?.round?.status ?? ""] ?? "idle";

  return (
    <>
      <div className={`health-bar ${healthCls}`}>
        <strong>{data.health.message}</strong>
        <span className="muted">深周期 {shortTime(data.health.deep.timestamp)}</span>
        {!llmBook && <span className="muted">快扫 {shortTime(data.health.fast.timestamp)}</span>}
        {data.health.stop_check && (
          // The most recent ACTUAL check, whichever mode ran it — not
          // necessarily "deep" — so it's its own line rather than folded
          // into either mode's timestamp above.
          <span
            className="muted"
            title={data.health.stop_check.unknown ? (data.health.stop_check.reason ?? undefined) : undefined}
          >
            · 止损（{data.health.stop_check.mode === "deep" ? "深周期" : "快扫"}{" "}
            {shortTime(data.health.stop_check.checked_at)}）
            {data.health.stop_check.unknown
              ? " 覆盖未核验"
              : ` ${data.health.stop_check.stops_covered}/${data.health.stop_check.positions}`}
            {data.health.stop_check.naked ? " · 有裸仓" : ""}
          </span>
        )}
        {data.health.note && <span className="muted">· {data.health.note}</span>}
      </div>

      <div className={`panel handoff ${statusCls}`}>
        <h3>交班</h3>
        {!data.progress?.present && (
          <p className="muted">本轮还没有交班卡。周期结束后会写 data/progress.json。</p>
        )}
        {card && (
          <>
            <div className="cards">
              <Kpi label="本轮" value={`${card.round?.name ?? "—"} · ${card.round?.status ?? "—"}`} small />
              <Kpi label="下一班" value={card.next_job?.slot ?? "—"} small />
              <Kpi label="下一班时刻 ET" value={card.next_job?.when_et ?? "—"} small />
              <Kpi label="止损覆盖" value={
                card.risk?.stops_total != null
                  ? `${card.risk.stops_covered ?? "—"}/${card.risk.stops_total}`
                  : "—"
              } small />
            </div>
            {card.next_job?.instruction && <p>{card.next_job.instruction}</p>}
            {!!card.did?.length && <p className="muted">做了：{card.did.join("；")}</p>}
            {!!card.did_not?.length && <p className="muted">没做：{card.did_not.join("；")}</p>}
            {!!card.unresolved?.length && (
              <ul>
                {card.unresolved.map((u, i) => (
                  <li key={`${u.kind}-${i}`}>{u.kind}{u.detail ? ` — ${u.detail}` : ""}</li>
                ))}
              </ul>
            )}
            {!!card.risk?.locks?.length && (
              <p className="muted">锁：{card.risk.locks.join("、")}</p>
            )}
          </>
        )}
      </div>

      <div className="cards">
        <Kpi label="交易日" value={data.session_date} small />
        <Kpi label="日历今天" value={data.weekend ? `${data.calendar_today} 周末` : data.calendar_today} small />
        <Kpi label="今日成交" value={String(data.fills_today.length)} />
        {!llmBook && <Kpi label="排队意图" value={String(data.intents.length)} />}
        {!llmBook && <Kpi label="LLM fail-closed" value={String(data.fail_closed_today)} />}
        <Kpi label="持仓" value={llmBook
          ? String(data.holdings.length)
          : `${pool.length} 池内 / ${legacy.length} 遗留`} small />
      </div>

      <div className="panel">
        <h3>今天成交了什么（券商 fills，不是 journal）</h3>
        {data.fills_degraded && <p className="muted">{data.fills_reason || "券商不可用，成交无法核对。"}</p>}
        {!data.fills_degraded && data.fills_today.length === 0 && (
          <p className="muted">交易日 {data.session_date} 没有成交。{data.notes.fills}</p>
        )}
        {data.fills_today.length > 0 && (
          <table>
            <thead>
              <tr><th>时间</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th></tr>
            </thead>
            <tbody>
              {data.fills_today.map((f, i) => (
                <tr key={`${f.symbol}-${f.transaction_time}-${i}`}>
                  <td>{shortTime(f.transaction_time)}</td>
                  <td>{f.symbol}</td>
                  <td className={f.side === "buy" ? "pos" : "neg"}>{f.side}</td>
                  <td className="mono">{f.qty}</td>
                  <td className="mono">{usd(f.price, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="note">{data.notes.fills}</p>
      </div>

      <div className="grid2">
        <div className="panel">
          <h3>{llmBook ? "本轮拦截 / 未决" : "想买但没买成"}</h3>
          {data.intents.length === 0 && data.intent_events.length === 0 && (
            <p className="muted">当前没有排队意图。{data.notes.intents}</p>
          )}
          {data.intents.map((it) => (
            <div key={it.symbol} className="decision-card">
              <div className="decision-head">
                <span className="sym">{it.symbol}</span>
                <span className="badge wait">{it.status}</span>
                <span className="muted mono">quant {it.quant_score?.toFixed(2)}</span>
                {it.ttl_hours != null && <span className="muted">TTL {it.ttl_hours}h</span>}
              </div>
              {it.reasoning && <div className="reasoning">{it.reasoning}</div>}
            </div>
          ))}
          {data.intent_events.slice(0, 8).map((ev, i) => (
            <div key={`${ev.symbol}-${ev.timestamp}-${i}`} className="reasoning muted">
              {shortTime(ev.timestamp)} {ev.symbol} · {ev.kind}
              {ev.deferred ? "（保留）" : "（丢弃）"}
              {ev.detail ? ` — ${ev.detail}` : ""}
            </div>
          ))}
        </div>
        <div className="panel">
          <h3>拦截计数</h3>
          <table>
            <thead><tr><th>类别</th><th>近7天</th><th>累计</th></tr></thead>
            <tbody>
              {binding.map((v) => (
                <tr key={v.label}>
                  <td>{v.label}</td>
                  <td className="mono">{v.recent}</td>
                  <td className="mono">{v.total}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {binding.length === 0 && <p className="muted">还没有可计数的拦截。</p>}
        </div>
      </div>

      <div className="panel">
        <h3>现在持着什么</h3>
        {data.holdings.length === 0 && <p className="muted">无持仓。</p>}
        {data.holdings.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>标的</th><th>口径</th><th>市值</th><th>浮盈亏</th>
                <th>quant</th><th>距峰值</th>
              </tr>
            </thead>
            <tbody>
              {data.holdings.map((h) => (
                <tr key={h.symbol}>
                  <td>{h.symbol}</td>
                  <td><span className={`badge ${!llmBook && h.bucket === "legacy" ? "wait" : "hold"}`}>
                    {llmBook ? "持仓" : h.bucket === "legacy" ? "遗留" : "池内"}
                  </span></td>
                  <td className="mono">{usd(h.market_value, 0)}</td>
                  <td className={`mono ${(h.unrealized_pl ?? 0) >= 0 ? "pos" : "neg"}`}>
                    {usd(h.unrealized_pl, 2)} {h.unrealized_plpc != null ? pct(h.unrealized_plpc) : ""}
                  </td>
                  <td className="mono">{h.quant_score != null ? h.quant_score.toFixed(2) : "—"}</td>
                  <td className="mono">{h.giveback_pct != null ? pct(h.giveback_pct) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {!llmBook && <p className="note">{data.notes.legacy}</p>}
      </div>
    </>
  );
}

function Kpi({ label, value, small }: { label: string; value: string; small?: boolean }) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="value" style={small ? { fontSize: 16 } : undefined}>{value}</div>
    </div>
  );
}
