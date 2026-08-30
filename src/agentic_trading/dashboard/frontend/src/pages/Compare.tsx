import { useEffect, useState } from "react";
import {
  Area, AreaChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api, ComparePayload } from "../api";

export default function Compare() {
  const [payload, setPayload] = useState<ComparePayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.compare().then(setPayload).catch((e) => setError(String(e)));
  }, []);

  if (error) return <div className="warn-box">{error}</div>;
  if (!payload) return <div className="panel muted">加载中…</div>;

  // Merge both rebased series onto common dates for one chart.
  const p2ByDate = new Map(payload.normalized_equity.p2.map((p) => [p.date, p.value]));
  const merged = payload.normalized_equity.p1.map((p) => ({
    date: p.date, p1: p.value, p2: p2ByDate.get(p.date) ?? null,
  }));

  return (
    <>
      <div className="panel">
        <h3>净值叠加（各自窗口起点归一为 1.0）</h3>
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={merged}>
            <CartesianGrid stroke="#21262d" />
            <XAxis dataKey="date" tick={{ fill: "#8b949e", fontSize: 11 }} />
            <YAxis tick={{ fill: "#8b949e", fontSize: 11 }} domain={["auto", "auto"]} />
            <Tooltip contentStyle={{ background: "#161b22", border: "1px solid #30363d" }} />
            <Legend />
            <Line dataKey="p1" name="一号盘 · 成长池" stroke="#58a6ff" dot={false} strokeWidth={2} />
            <Line dataKey="p2" name="二号盘 · 主题轮动" stroke="#d29922" dot={false} strokeWidth={2} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="grid2">
        <div className="panel">
          <h3>持仓重叠度（|A∩B| / max(|A|,|B|)）</h3>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={payload.overlap}>
              <CartesianGrid stroke="#21262d" />
              <XAxis dataKey="date" tick={{ fill: "#8b949e", fontSize: 11 }} />
              <YAxis domain={[0, 1]} tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`}
                tick={{ fill: "#8b949e", fontSize: 11 }} />
              <Tooltip contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
                formatter={(v: number | string) => `${(Number(v) * 100).toFixed(0)}%`} />
              <Area dataKey="overlap" stroke="#58a6ff" fill="#58a6ff" fillOpacity={0.15} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
        <div className="panel">
          <h3>60 日滚动收益相关系数</h3>
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={payload.rolling_correlation}>
              <CartesianGrid stroke="#21262d" />
              <XAxis dataKey="date" tick={{ fill: "#8b949e", fontSize: 11 }} />
              <YAxis domain={[0, 1]} tick={{ fill: "#8b949e", fontSize: 11 }} />
              <ReferenceLine y={0.95} stroke="#f85149" strokeDasharray="4 4"
                label={{ value: "判定线 0.95", fill: "#f85149", fontSize: 11 }} />
              <Tooltip contentStyle={{ background: "#161b22", border: "1px solid #30363d" }} />
              <Line dataKey="correlation" stroke="#3fb950" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="note">{payload.note}——重叠度长期 &gt;70% 且相关性持续 &gt;0.95 ⇒ 判定轮动未产生增量信息。</div>
    </>
  );
}
