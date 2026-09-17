import { useEffect, useState } from "react";
import {
  Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api, BookSummary, CycleRow, EquityPoint, HealthPayload, pct, shortTime, usd } from "../api";

export default function Overview({ bookId }: { bookId: string }) {
  const [series, setSeries] = useState<EquityPoint[]>([]);
  const [cycles, setCycles] = useState<CycleRow[]>([]);
  const [book, setBook] = useState<BookSummary | null>(null);
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.equity(bookId).then((d) => setSeries(d.series)).catch((e) => setError(String(e)));
    api.cycles(bookId, 12).then((d) => setCycles(d.cycles)).catch(() => {});
    api.books().then((d) => setBook(d.books.find((b) => b.id === bookId) ?? null)).catch(() => {});
    api.health(bookId).then(setHealth).catch(() => {});
  }, [bookId]);

  if (error) return <div className="warn-box">{error}</div>;
  const exposure = book && book.equity ? 1 - (book.cash ?? 0) / book.equity : null;

  const healthCls = health
    ? { ok: "ok", weekend: "idle", stale: "bad", missing: "bad", corrupt: "bad" }[health.status] ?? "idle"
    : "idle";

  return (
    <>
      {health && (
        <div className={`health-bar ${healthCls}`}>
          <strong>{health.message}</strong>
          <span className="muted">深 {shortTime(health.deep.timestamp)}</span>
          <span className="muted">快 {shortTime(health.fast.timestamp)}</span>
        </div>
      )}
      <div className="cards">
        <Kpi label="净值" value={usd(book?.equity)} />
        <Kpi label="现金" value={usd(book?.cash)} />
        <Kpi label="仓位占用" value={pct(exposure)} />
        <Kpi
          label="市场体制"
          value={book?.regime_label ?? "—"}
          className={regimeClass(book?.regime_label)}
        />
        <Kpi label="持仓数" value={String(book?.open_positions ?? "—")} />
        <Kpi label="最近周期" value={shortTime(book?.last_cycle_at)} small />
      </div>

      <div className="panel">
        <h3>权益曲线 与 回撤（日线，journal 深周期）</h3>
        <ResponsiveContainer width="100%" height={320}>
          <ComposedChart data={series}>
            <CartesianGrid stroke="#21262d" />
            <XAxis dataKey="date" tick={{ fill: "#8b949e", fontSize: 11 }} />
            <YAxis yAxisId="eq" tick={{ fill: "#8b949e", fontSize: 11 }} domain={["auto", "auto"]} />
            <YAxis yAxisId="dd" orientation="right" tick={{ fill: "#8b949e", fontSize: 11 }}
              tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`} domain={[-1, 0]} />
            <Tooltip
              contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
              formatter={(value: number | string, name: string) =>
                name === "drawdown" ? [`${(Number(value) * 100).toFixed(2)}%`, "回撤"] : [usd(Number(value), 2), name === "equity" ? "净值" : name]
              }
            />
            <Line yAxisId="eq" dataKey="equity" stroke="#58a6ff" dot={false} strokeWidth={2} name="equity" />
            <Area yAxisId="dd" dataKey="drawdown" stroke="#f85149" fill="#f85149" fillOpacity={0.15} name="drawdown" />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      <div className="panel">
        <h3>最近决策周期</h3>
        <table>
          <thead>
            <tr><th>时间 (UTC)</th><th>模式</th><th>净值</th><th>体制</th><th>决策数</th></tr>
          </thead>
          <tbody>
            {cycles.map((c) => (
              <tr key={c.id}>
                <td>{shortTime(c.timestamp)}</td>
                <td className="muted">{c.mode}</td>
                <td className="mono">{usd(c.equity)}</td>
                <td><span className={`badge regime-${c.regime_label}`}>{c.regime_label}</span></td>
                <td>{c.n_decisions}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Kpi({ label, value, className, small }: {
  label: string; value: string; className?: string; small?: boolean;
}) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`value ${className ?? ""}`} style={small ? { fontSize: 14 } : undefined}>{value}</div>
    </div>
  );
}

function regimeClass(label: string | null | undefined): string {
  return label ? `badge regime-${label}` : "";
}
