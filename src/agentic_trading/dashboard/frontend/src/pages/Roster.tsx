import { useEffect, useState } from "react";
import { api, RosterData, RosterPayload } from "../api";

export default function Roster({ bookId }: { bookId: string }) {
  const [payload, setPayload] = useState<RosterPayload | null>(null);

  useEffect(() => {
    api.roster(bookId).then(setPayload).catch((e) =>
      setPayload({ available: false, reason: String(e) }));
  }, [bookId]);

  if (!payload) return <div className="panel muted">加载中…</div>;
  if (!payload.available) return <div className="warn-box">名单不可用：{payload.reason}</div>;

  return (
    <div className="grid2">
      {(["evening", "midday"] as const).map((kind) => {
        const data = payload[kind];
        return data ? <RosterPanel key={kind} data={data} /> : (
          <div key={kind} className="panel muted">{kind === "evening" ? "晚间轮" : "午间轮"}：暂无档案</div>
        );
      })}
    </div>
  );
}

function RosterPanel({ data }: { data: RosterData }) {
  const title = data.round === "midday" ? "午间轮（含 forming bar，带迟滞）" : "晚间轮（settled bar）";
  return (
    <div className="panel">
      <h3>{title}{data.generated_at ? ` · ${data.generated_at} UTC` : ""} · 池 {data.pool_size ?? "?"}</h3>
      <table>
        <thead>
          <tr><th>#</th><th>代码</th><th>RS</th><th>超额20d</th><th>超额60d</th><th>现价</th><th>streak</th><th>可买</th></tr>
        </thead>
        <tbody>
          {data.rows.map((r) => (
            <tr key={r.symbol}>
              <td>{r.rank}</td>
              <td>{data.held.includes(r.symbol) ? <span className="badge hold">{r.symbol}</span> : r.symbol}</td>
              <td className="mono">{r.rs.toFixed(1)}</td>
              <td className="mono pos">{r.ex_20d}</td>
              <td className="mono pos">{r.ex_60d}</td>
              <td className="mono">${r.last.toFixed(2)}</td>
              <td className="mono">{r.streak}</td>
              <td>{r.buyable ? <span className="badge buy">yes</span> : <span className="muted">no</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ marginTop: 10 }} className="chips">
        {data.added.length > 0 && <span className="chip" style={{ color: "var(--green)" }}>+{data.added.join(" +")}</span>}
        {data.removed.length > 0 && <span className="chip" style={{ color: "var(--red)" }}>−{data.removed.join(" −")}</span>}
        {data.added.length === 0 && data.removed.length === 0 && <span className="chip">名单无变化</span>}
      </div>
      <div className="note">streak≥2 才允许买入（迟滞）；已持仓者跌出不强卖，走既有止损自然退出。</div>
    </div>
  );
}
