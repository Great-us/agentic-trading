import { useEffect, useState } from "react";
import { api, LogicPayload } from "../api";

const LABELS: Record<string, string> = {
  buy_threshold: "买入阈值 (quant 分)",
  sell_threshold: "卖出阈值",
  risk_per_trade_pct: "单笔风险 / 净值",
  max_position_pct: "单仓上限",
  max_open_positions: "最大持仓数",
  max_new_orders_per_cycle: "每轮新订单上限",
  max_total_exposure_pct: "总敞口上限",
  atr_stop_multiple: "ATR 止损倍数",
  min_stop_pct: "止损下限",
  max_stop_pct: "止损上限",
  trailing_stop_pct: "trailing 止损",
  take_profit_pct: "固定止盈（null=关闭）",
  risk_off_size_multiplier: "risk-off 仓位乘数",
  risk_off_score_penalty: "risk-off 入场加严",
  regime_max_exposure: "分体制敞口上限",
  max_sector_pct: "行业 cap",
  default_max_sector_pct: "默认行业 cap",
  corr_lookback: "相关性回看窗口",
  corr_penalty_threshold: "相关性惩罚阈值",
  corr_size_multiplier: "相关性减半系数",
  entry_window_start_et: "入场窗口开始 (ET)",
  entry_window_end_et: "入场窗口结束 (ET)",
  max_entry_gap_atr: "gap 否决 (ATR)",
  max_chase_vs_open_pct: "追高上限 vs 开盘",
  max_chase_vs_signal_pct: "追高上限 vs 信号价",
  max_portfolio_stop_risk_pct: "book 级止损风险上限",
  min_cash_buffer_pct: "常备现金缓冲",
  min_intraday_confirm_scans: "盘中连续确认次数",
  require_llm_for_entry: "入场必须 LLM 在场",
  trim_trigger_score: "TRIM 触发分数",
  trim_confirm_cycles: "TRIM 确认周期数",
};

export default function Logic({ bookId }: { bookId: string }) {
  const [payload, setPayload] = useState<LogicPayload | null>(null);

  useEffect(() => {
    api.logic(bookId).then(setPayload).catch(() => {});
  }, [bookId]);

  if (!payload) return <div className="panel muted">加载中…</div>;

  const flatRows: [string, string][] = [];
  for (const [key, value] of Object.entries(payload.risk)) {
    if (!LABELS[key]) continue;
    flatRows.push([LABELS[key], formatValue(value)]);
  }
  const extraRows = Object.entries(payload.risk)
    .filter(([k]) => !LABELS[k] && !["regime_max_exposure", "max_sector_pct"].includes(k))
    .map(([k, v]) => [k, formatValue(v)] as [string, string]);

  return (
    <>
      <div className="panel">
        <h3>这套系统怎么想 —— 决策管线</h3>
        {payload.pipeline.map(([title, desc]) => (
          <div key={title} className="pipeline-step">
            <div className="step-title">{title}</div>
            <div className="step-desc">{desc}</div>
          </div>
        ))}
        <div className="note">LLM 只能否决或降级为 HOLD，永远不能发起买卖；硬性风控在决策之前生效。</div>
      </div>

      <div className="grid2">
        <div className="panel">
          <h3>生效风控参数（{bookId}/config/risk.yaml）</h3>
          <table>
            <tbody>
              {[...flatRows, ...extraRows].map(([label, value]) => (
                <tr key={label}><td>{label}</td><td className="mono">{value}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="panel">
          <h3>交易宇宙（watchlist.yaml）</h3>
          <div className="chips" style={{ marginBottom: 14 }}>
            {payload.watchlist.map((s) => <span key={s} className="chip">{s}</span>)}
          </div>
          {payload.context_symbols.length > 0 && (
            <>
              <h3>体制参考（只看不买）</h3>
              <div className="chips">
                {payload.context_symbols.map((s) => <span key={s} className="chip warn">{s}</span>)}
              </div>
            </>
          )}
        </div>
      </div>
    </>
  );
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return String(value);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
