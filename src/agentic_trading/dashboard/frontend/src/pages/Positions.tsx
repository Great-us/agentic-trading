import { useEffect, useState } from "react";
import { api, pct, PositionRow, usd } from "../api";

export default function Positions({ bookId }: { bookId: string }) {
  const [rows, setRows] = useState<PositionRow[]>([]);
  const [degraded, setDegraded] = useState<string | null>(null);

  useEffect(() => {
    api.positions(bookId)
      .then((d) => {
        setRows(d.positions);
        if (d.degraded) setDegraded(`broker 不可用，展示 journal 快照：${d.reason}`);
      })
      .catch((e) => setDegraded(String(e)));
  }, [bookId]);

  const broker = rows[0]?.source === "broker";

  return (
    <div className="panel">
      {degraded && <div className="warn-box">{degraded}</div>}
      <h3>当前持仓{broker ? "" : "（journal 快照）"}</h3>
      <table>
        <thead>
          <tr>
            <th>代码</th><th>数量</th>
            {broker && <th>现价</th>}
            {broker && <th>成本价</th>}
            {broker && <th>市值</th>}
            {broker && <th>浮动盈亏</th>}
            <th>止损价</th><th>距止损</th><th>高点回吐</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.symbol}>
              <td>{p.symbol}</td>
              <td className="mono">{round(p.qty ?? p.last_qty)}</td>
              {broker && <td className="mono">${fmt(p.current_price)}</td>}
              {broker && <td className="mono">${fmt(p.avg_entry_price)}</td>}
              {broker && <td className="mono">{usd(p.market_value)}</td>}
              {broker && (
                <td className={`mono ${(p.unrealized_pl ?? 0) >= 0 ? "pos" : "neg"}`}>
                  {usd(p.unrealized_pl)} ({pct(p.unrealized_plpc)})
                </td>
              )}
              <td className="mono">{p.stop_price ? `$${fmt(p.stop_price)}` : "—"}</td>
              <td className={`mono ${stopClass(p.stop_distance_pct)}`}>
                {p.stop_distance_pct !== null && p.stop_distance_pct !== undefined ? pct(p.stop_distance_pct) : "—"}
              </td>
              <td className="mono muted">{p.giveback_pct != null ? pct(p.giveback_pct) : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="note">距止损 = 现价高于保护性 stop 单的幅度；高点回吐 = 现价相对 journal 记录的高水位。</div>
    </div>
  );
}

const fmt = (v: number | null | undefined): string => (v == null ? "—" : v.toFixed(2));
const round = (v: number | null | undefined): string => (v == null ? "—" : String(Math.round(v * 1000) / 1000));
const stopClass = (v: number | null | undefined): string =>
  v == null ? "" : v < 0.02 ? "neg" : v < 0.05 ? "" : "pos";
