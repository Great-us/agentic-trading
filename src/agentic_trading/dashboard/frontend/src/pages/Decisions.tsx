import { useEffect, useState } from "react";
import { api, CycleRow, DecisionRow, pct, shortTime } from "../api";

export default function Decisions({ bookId }: { bookId: string }) {
  const [cycles, setCycles] = useState<CycleRow[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [rows, setRows] = useState<DecisionRow[]>([]);

  useEffect(() => {
    api.cycles(bookId, 60)
      .then((d) => {
        setCycles(d.cycles);
        if (d.cycles.length) setSelected(d.cycles[0].id);
      })
      .catch(() => {});
  }, [bookId]);

  useEffect(() => {
    if (selected === null) return;
    api.decisions(bookId, { cycle_id: selected, limit: 100 })
      .then((d) => setRows(d.decisions))
      .catch(() => {});
  }, [bookId, selected]);

  return (
    <div className="grid2">
      <div className="panel cycles-list">
        <h3>周期（选择一轮查看完整决策链）</h3>
        {cycles.map((c) => (
          <div
            key={c.id}
            className={`cycle-row ${c.id === selected ? "selected" : ""}`}
            onClick={() => setSelected(c.id)}
          >
            <span>
              #{c.id} · {shortTime(c.timestamp)}{" "}
              <span className={`badge regime-${c.regime_label}`}>{c.regime_label}</span>
            </span>
            <span className="muted mono">{c.n_decisions} 决策 / {c.equity != null ? `$${Math.round(c.equity).toLocaleString()}` : ""}</span>
          </div>
        ))}
      </div>
      <div>
        {rows.map((d) => (
          <DecisionCard key={d.id} row={d} />
        ))}
        {!rows.length && <div className="panel muted">该轮无决策记录</div>}
      </div>
    </div>
  );
}

function DecisionCard({ row }: { row: DecisionRow }) {
  return (
    <div className="decision-card">
      <div className="decision-head">
        <span className="sym">{row.symbol}</span>
        <span className={`badge ${row.action.toLowerCase()}`}>{row.action}</span>
        <span className="chain-step mono muted">quant {fmtScore(row.quant_score)}</span>
        <span className="chain-step mono muted">combined {fmtScore(row.combined_score)}</span>
        {row.llm_stance && (
          <span className="chain-step muted">
            LLM {row.llm_stance}{row.llm_confidence != null ? ` (${pct(row.llm_confidence, 0)})` : ""}
          </span>
        )}
        {row.order_status && <span className="muted">订单: {row.order_status}</span>}
      </div>
      {(row.reasoning || row.sizing_reason) && (
        <div className="reasoning">{row.sizing_reason ? `[sizing] ${row.sizing_reason}` : ""}{row.reasoning ? `\n${row.reasoning}` : ""}</div>
      )}
      {(row.llm_rationale || row.llm_risk_flags || row.grok_stance) && (
        <div className="reasoning muted">
          {row.llm_rationale && <div>[LLM] {row.llm_rationale}</div>}
          {row.llm_risk_flags && <div>[risk_flags] {row.llm_risk_flags}</div>}
          {row.grok_stance && <div>[grok] {row.grok_stance}{row.grok_confidence != null ? ` (${pct(row.grok_confidence, 0)})` : ""} — 情绪参考，非否决</div>}
        </div>
      )}
    </div>
  );
}

const fmtScore = (v: number | null): string =>
  v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2);
