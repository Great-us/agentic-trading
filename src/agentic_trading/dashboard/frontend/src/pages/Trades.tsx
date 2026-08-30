import { useEffect, useState } from "react";
import { Line, LineChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, DecisionRow, shortTime, TradeTrip, usd } from "../api";

export default function Trades({ bookId }: { bookId: string }) {
  const [trips, setTrips] = useState<TradeTrip[]>([]);
  const [timeline, setTimeline] = useState<{ closed_at: string; cum_realized_pnl: number; symbol: string }[]>([]);
  const [degraded, setDegraded] = useState<string | null>(null);
  const [detail, setDetail] = useState<{ symbol: string; rows: DecisionRow[] } | null>(null);

  useEffect(() => {
    api.trades(bookId)
      .then((d) => {
        if (d.degraded) setDegraded(`无法读取成交流水：${d.reason}`);
        setTrips(d.trades);
        setTimeline(d.timeline);
      })
      .catch((e) => setDegraded(String(e)));
  }, [bookId]);

  const openTrade = (symbol: string) =>
    api.decisions(bookId, { symbol, limit: 8 })
      .then((d) => setDetail({ symbol, rows: d.decisions }))
      .catch(() => {});

  return (
    <>
      {degraded && <div className="warn-box">{degraded}</div>}
      <div className="panel">
        <h3>累计已实现盈亏（closed trips）</h3>
        {timeline.length ? (
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={timeline.map((t) => ({ ...t, label: t.closed_at?.slice(0, 10) }))}>
              <CartesianGrid stroke="#21262d" />
              <XAxis dataKey="label" tick={{ fill: "#8b949e", fontSize: 11 }} />
              <YAxis tick={{ fill: "#8b949e", fontSize: 11 }} domain={["auto", "auto"]} />
              <Tooltip contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                formatter={(v: number | string) => [usd(Number(v), 2), "累计已实现"]} />
              <Line dataKey="cum_realized_pnl" stroke="#3fb950" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="muted">尚无平仓成交</div>
        )}
      </div>

      <div className="panel">
        <h3>Round-trips（点击查看该股最近决策链）</h3>
        <table>
          <thead>
            <tr>
              <th>代码</th><th>状态</th><th>开仓时间</th><th>平仓时间</th><th>数量</th>
              <th>成本价</th><th>卖均价</th><th>已实现盈亏</th><th>浮动盈亏</th>
            </tr>
          </thead>
          <tbody>
            {trips.map((t, i) => (
              <tr key={`${t.symbol}-${i}`} style={{ cursor: "pointer" }} onClick={() => openTrade(t.symbol)}>
                <td>{t.symbol}</td>
                <td>{t.open ? <span className="badge hold">持仓中</span> : <span className="badge sell">已平仓</span>}</td>
                <td className="muted">{shortTime(t.opened_at)}</td>
                <td className="muted">{shortTime(t.closed_at)}</td>
                <td className="mono">{Math.round(t.qty * 1000) / 1000}</td>
                <td className="mono">${t.avg_entry?.toFixed(2) ?? "—"}</td>
                <td className="mono">{t.avg_exit ? `$${t.avg_exit.toFixed(2)}` : "—"}</td>
                <td className={`mono ${t.realized_pnl >= 0 ? "pos" : "neg"}`}>{usd(t.realized_pnl, 2)}</td>
                <td className={`mono ${(t.unrealized_pnl ?? 0) >= 0 ? "pos" : "neg"}`}>
                  {t.unrealized_pnl != null ? usd(t.unrealized_pnl, 2) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="note">数据源：Alpaca 成交流水（只读 GET），均价为平均成本法；journal 只记录下单意图不记录成交。</div>
      </div>

      {detail && (
        <div className="panel">
          <h3>{detail.symbol} 最近决策（{detail.rows.length} 条）</h3>
          {detail.rows.map((r) => (
            <div key={r.id} className="decision-card">
              <div className="decision-head">
                <span className={`badge ${r.action.toLowerCase()}`}>{r.action}</span>
                <span className="muted">{shortTime(r.cycle_time)}</span>
                <span className="mono muted">quant {r.quant_score?.toFixed(2)}</span>
                <span className="muted">{r.order_status ?? ""}</span>
              </div>
              <div className="reasoning">{r.reasoning || r.llm_rationale}</div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
