import { useEffect, useState } from "react";
import {
  api, TodayPayload, FunnelPayload, FunnelOrder, FunnelSizingSnapshot,
  FunnelChain, FunnelCarryover, FunnelSummaryCounts, pct, shortTime, usd,
} from "../api";

export default function Today({ bookId }: { bookId: string }) {
  const [data, setData] = useState<TodayPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [funnel, setFunnel] = useState<FunnelPayload | null>(null);
  const [funnelError, setFunnelError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    setError(null);
    setFunnel(null);
    setFunnelError(null);
    api.today(bookId).then(setData).catch((e) => setError(String(e)));
    // The funnel degrades on its own (old journal, missing endpoint on a
    // non-P1 deployment): it must never take the rest of the page down.
    api.funnel(bookId).then(setFunnel).catch((e) => setFunnelError(String(e)));
  }, [bookId]);

  if (error) return <div className="warn-box">{error}</div>;
  if (!data) return <div className="muted">加载今日…</div>;

  return <TodayView data={data} funnel={funnel} funnelError={funnelError} />;
}

// Exported for the offline render test (tests/test_frontend_render.py), which
// renders this view with react-dom/server: everything on the loaded page
// except the useEffect fetch wiring in the default export above.
export function TodayView({ data, funnel, funnelError }: {
  data: TodayPayload;
  funnel: FunnelPayload | null;
  funnelError?: string | null;
}) {
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

      <FunnelPanel funnel={funnel} funnelError={funnelError} />

      <div className="grid2">
        <div className="panel">
          <h3>{llmBook ? "本轮拦截 / 未决" : "排队意图与最近事件"}</h3>
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
              {shortTime(ev.timestamp)} {ev.symbol} · {ev.kind}{intentEventLabel(ev.kind, ev.deferred)}
              {ev.detail ? ` — ${ev.detail}` : ""}
            </div>
          ))}
          {data.intent_events.length > 0 && (
            <p className="note">完整漏斗见上方「执行漏斗」；这里只保留最近 8 条原始事件尾迹。</p>
          )}
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

// ---- A-4 (c2c_a7e2 §五): execution funnel -----------------------------------
// Replaces the old blanket "想买但没买成" story: per-decision chains with stage,
// reason, attempts vs wait counts, order status (accepted ≠ filled) and the
// sizing snapshot echoed from the event that produced it. Carryover intents
// get their own section outside the session denominator. Degraded sources
// (old schema, failed/truncated fills, identity-less legacy rows) are shown
// as unknown/degraded — never as fake zeros.
//
// Review R8: the sizing line shows EVERY margin (cash / exposure / sector-
// theme / stop-risk room / position cap / pre-post haircut / available /
// minimum), keeps zero and NULL strictly apart (NULL = 未评估, not $0), names
// the event kind that carried the snapshot, and marks replaced/terminal
// chains as 历史快照 instead of the blanket 意图保留. Review R5 display rule:
// order wording follows the backend's phase/submit_phase — rejected/canceled
// never read as 已受理. R8 iteration-3 review: the intent lifecycle
// (classification: discarded/cleared/superseded are history; only
// waiting/created_pending may say 意图保留) is separate from the order
// terminal state (partial is NOT 链已终结).

const FUNNEL_CLASS_LABEL: Record<string, string> = {
  submitted: "已提交", waiting: "等待中", discarded: "已丢弃", cleared: "已清除",
  created_pending: "已建待执行", not_created: "未建意图", evidence_missing: "证据缺失",
  submit_rejected: "提交被拒绝", submit_unknown: "提交结果不明", superseded: "已被替代",
};

const funnelClassLabel = (c?: string | null): string =>
  (c && FUNNEL_CLASS_LABEL[c]) || "未知";

// Backend order/chain phase vocabulary (dashboard/views.py _order_phase /
// _chain_phase). The page renders these labels verbatim instead of guessing
// from a broker status string.
const ORDER_PHASE_LABEL: Record<string, string> = {
  complete: "整单完成", partial: "部分成交", terminal_unfilled: "终结未成交",
  submit_rejected: "提交被拒绝", submitted: "已提交",
  submit_unknown: "提交结果不明", unknown: "状态未知",
};

const SUBMIT_PHASE_LABEL: Record<string, string> = {
  submitted_accepted: "已受理", submit_rejected: "被拒绝", submit_unknown: "结果不明",
};

// Terminal non-fill broker statuses — mirror of views.py _DEAD_ORDER_STATUSES
// (read-only display; only used as a fallback when the backend's own phase
// fields are absent, e.g. hand-rolled old payloads).
const DEAD_ORDER_STATUSES = new Set([
  "rejected", "expired", "canceled", "cancelled", "replaced", "stopped",
  "suspended", "done_for_day",
]);

// Chain phases meaning REAL order activity happened — if the sizing snapshot
// cannot be paired to its own order, the order/terminal state is genuinely
// UNDETERMINED (R8 iteration-5 review), never borrowed from the chain or a
// sibling attempt. R8 (iteration-7 review): submit_rejected chains get the
// SAME treatment when the snapshot cannot be paired — a chain-level rejection
// proves nothing about the attempt that carried THIS snapshot, so an
// unpaired snapshot there stays undetermined too (never 链已终结).
const UNDETERMINED_CHAIN_PHASES = new Set([
  "partial", "complete", "terminal_unfilled", "submit_rejected",
]);

// R8 (iteration-3 review): the intent LIFECYCLE is a separate fact from the
// order phase. classification is the backend's own funnel classification
// (views.py _funnel_classify plus the chain-level overrides):
//   dead  = discarded (TTL/gap 删除) | cleared (已清除) | superseded (已被替代)
//   alive = waiting | created_pending — the ONLY classes that may still say
//           意图保留待后续尝试
// order-bound classes (submitted / submit_rejected / submit_unknown) and the
// structural ones (not_created / evidence_missing / other) are judged by the
// order phase above, never by a page-side guess.
const DEAD_INTENT_CLASSES = new Set(["discarded", "cleared", "superseded"]);
const ALIVE_INTENT_CLASSES = new Set(["waiting", "created_pending"]);

// Why a dead intent's sizing is history — mirrored from the backend's
// classification/stage (stage distinguishes ttl vs gap among discards).
function deadIntentReason(classification: string | null | undefined,
                          stage: string | null | undefined): string {
  if (classification === "cleared") return "意图已清除";
  if (classification === "superseded") return "已被新版本替代";
  if (classification === "discarded") {
    return stage === "gap" ? "追价间隔丢弃" : "TTL 丢弃";
  }
  return "意图已终结";
}

const LEGACY_DISCARD_KINDS = new Set(["gap", "ttl"]);
const LEGACY_KEEP_KINDS = new Set(["chase_signal", "chase_open", "sizing"]);

// Old pre-A-2 flush events: gap/ttl really discarded the intent; the wait
// kinds kept it when deferred. NEW kinds (order_submitted, order_filled, …)
// are their own stage and get NO blanket drop/keep verdict — the old code
// wrote "（丢弃）" on every deferred=0 row, which mislabels submissions and
// fills (PLAN §五).
function intentEventLabel(kind: string, deferred: number | null | undefined): string {
  if (LEGACY_DISCARD_KINDS.has(kind)) return "（丢弃）";
  if (LEGACY_KEEP_KINDS.has(kind)) return deferred ? "（保留待触发）" : "（当轮未保留）";
  return "";
}

function orderFillNote(order: FunnelOrder): string | null {
  const observed = (order.last_observation_kind ?? "").toLowerCase();
  const status = (order.broker_status ?? "").toLowerCase();
  const matchedQty = order.fills_matched ? (order.fills_qty ?? 0) : 0;
  if (order.phase === "complete" || order.phase === "partial") return null;
  if (observed === "order_filled" || status === "filled") return null;
  if (observed === "order_partial" || status === "partially_filled") return null;
  if (matchedQty > 0) return null;
  // R5/R8 口径：语义来自后端 phase / submit_phase——rejected / canceled /
  // unknown 一律不得写成"已受理"。仅当确有受理证据（submit_phase 已受理，
  // 或观察到的券商状态是非终结的存活状态）才说"已受理"。
  if (order.phase === "submit_rejected" || order.submit_phase === "submit_rejected") {
    return "提交被拒绝——券商未受理";
  }
  if (order.phase === "terminal_unfilled" || DEAD_ORDER_STATUSES.has(status)) {
    return `订单已终结（${order.terminal_status ?? status}）且未成交`;
  }
  const acceptEvidence = order.submit_phase === "submitted_accepted"
    || (!!status && !DEAD_ORDER_STATUSES.has(status));
  if (acceptEvidence) return "已受理，未确认成交（accepted ≠ filled）";
  if (order.submit_phase === "submit_unknown" || order.phase === "submit_unknown") {
    return "提交结果不明——无法确认受理";
  }
  if (order.phase === "submitted") return "已提交，受理确认缺失";
  return "券商状态未知——提交后未回查成功";
}

function OrderLine({ order }: { order: FunnelOrder }) {
  const parts: string[] = [];
  parts.push(`订单 ${order.order_id ?? "（无 order_id）"}`);
  parts.push(`状态 ${order.phase ? (ORDER_PHASE_LABEL[order.phase] ?? order.phase) : "未知"}`);
  if (order.submit_phase) {
    parts.push(`提交 ${SUBMIT_PHASE_LABEL[order.submit_phase] ?? order.submit_phase}`);
  }
  parts.push(order.broker_status ? `券商状态 ${order.broker_status}` : "券商状态未知");
  const partial = order.last_observation_kind === "order_partial"
    || (order.broker_status ?? "").toLowerCase() === "partially_filled";
  if (partial) {
    const qty = order.filled_qty ?? order.fills_qty;
    parts.push(`部分成交 ${qty != null ? qty : "?"} 股`
      + (order.filled_avg_price != null ? ` @ ${usd(order.filled_avg_price, 2)}` : ""));
  }
  if (order.terminal_status && order.phase !== "terminal_unfilled"
    && order.phase !== "submit_rejected") {
    parts.push(`订单已终结（${order.terminal_status}）`);
  }
  if (order.fills_matched === true) {
    parts.push(`fills 匹配 ${order.fills_qty ?? 0} 股 / ${usd(order.fills_notional, 2)}`);
  } else if (order.fills_unavailable) {
    parts.push("fills 不可用——成交证据缺失，不代表零成交");
  }
  const note = orderFillNote(order);
  if (note) parts.push(note);
  return <div className="reasoning muted">{parts.join(" · ")}</div>;
}

// R8: NULL（未评估）与 0（真实为零）严格区分——null/undefined 一律渲染为
// "未评估"，绝不回填 $0。
const NOT_EVALUATED = "未评估";
const money = (v: number | null | undefined): string =>
  v === null || v === undefined ? NOT_EVALUATED : usd(v, 2);
const pctOrNA = (v: number | null | undefined): string =>
  v === null || v === undefined ? NOT_EVALUATED : pct(v);

// R8 (iteration-4 review): the snapshot's OWN order. The sizing snapshot is
// carried by the submit event, whose timestamp equals that order's
// submitted_at — so the snapshot binds to the order submitted at ITS event
// time. R8 (iteration-7 review): a submit attempt WITHOUT an order_id is
// still an attempt with a verifiable submit event — views.py keys each
// identity-less submission as its own order entry (submitted_at /
// submit_status / submit_phase included) — so the match set is ALL entries,
// not only ones with an order_id. Ambiguity (several orders at the same
// instant, no timestamp, or a snapshot from a pre-submit veto) never borrows
// another attempt's state: the page then stays silent about the remainder
// instead of guessing.
function snapshotOrder(snap: FunnelSizingSnapshot,
                       orders: FunnelOrder[] | null | undefined): FunnelOrder | null {
  // R8 (iteration-7 final review): ONLY a snapshot that itself came from an
  // order_submitted event may pair with a submit attempt. A sizing/veto
  // snapshot sharing the same timestamp must never borrow that submit's
  // verdict — event kind gates the pairing, then the unique time match.
  if (snap.event_kind !== "order_submitted") return null;
  if (!snap.event_ts) return null;
  // R8 (iteration-7 review): identity-less attempts ARE in the match set —
  // each is its own order entry keyed by its own submit event. The old
  // `!!o.order_id` filter is what let a later attempt's snapshot fall
  // through to a chain-level rejection borrowed from an earlier attempt.
  const matched = (orders ?? []).filter((o) => o.submitted_at === snap.event_ts);
  return matched.length === 1 ? matched[0] : null;
}

// The paired order's OWN fill extent, read off its own evidence — the same
// three signals the backend accumulates partial_evidence from
// (views.py _funnel_orders: order_partial kind / partially_filled status /
// partial fills). Never inferred from the whole chain's phase.
function orderPartialEvidence(o: FunnelOrder): boolean {
  return o.partial_evidence === true
    || o.last_observation_kind === "order_partial"
    || (o.broker_status ?? "").toLowerCase() === "partially_filled";
}

function SizingLine({ snap, supersededBy, phase, classification, stage, orders }: {
  snap: FunnelSizingSnapshot;
  supersededBy?: string | null;
  phase?: string | null;
  // R8 (iteration-3 review): the intent's own lifecycle classification from
  // the backend — discarded/cleared/superseded make this snapshot history
  // even when no order ever existed (phase stays None).
  classification?: string | null;
  stage?: string | null;
  // R8 (iteration-4 review): the chain's orders, so the snapshot can find
  // ITS OWN order's independent terminal_status instead of inferring life
  // or death from the partial phase alone.
  orders?: FunnelOrder[] | null;
}) {
  const s = snap.sizing ?? {};
  // Top-level copies exist on veto payloads (run.py 写 cash_available /
  // available_notional / min_notional 于 payload 顶层)——诊断缺失时回退。
  const cash = s.cash_available ?? snap.cash_available ?? null;
  const available = s.available_notional ?? snap.available_notional ?? null;
  const min = s.min_position_notional ?? snap.min_notional ?? null;
  // R8: intent lifecycle (superseded/dead classification) and order terminal
  // state are SEPARATE facts. A partial, unterminated order means the sizing
  // is just a submit-time snapshot — the chain is not dead.
  const replaced = supersededBy != null;
  const intentDead = !replaced && DEAD_INTENT_CLASSES.has(classification ?? "");
  // R8 (iteration-5 review): ALL order-related judgment happens AFTER pairing.
  // 唯一配对成功 → 只读该订单自己的证据，优先级 = 完成（filled_evidence）>
  // 部分成交 > 明确拒绝 > 终态（与 views.py _order_phase 一致）；订单已确认
  // 完成时，完成证据优先于它历史上保留的部分成交证据。整条链的 phase
  // （views.py _chain_phase：链上任意订单 complete 即 complete）不决定这张
  // 快照的订单终态——同链另一尝试的终态不得覆盖本订单的状态。
  // R8 (iteration-7 review): 配对成功且该次提交自己带 submit_phase=
  // submit_rejected → 拒绝展示的证据就是那次提交，不是链级借用。需要订单
  // 关联但配对失败（缺时间 / 零匹配 / 同一时刻多个订单）→ 统一"对应订单/
  // 终态未能确定"，不论链级 phase 是 partial、complete、terminal_unfilled
  // 还是 submit_rejected——链级拒绝不再下放给配不上的快照。
  const snapOrder = snapshotOrder(snap, orders);
  const pairedComplete = snapOrder != null
    && snapOrder.filled_evidence === true;
  const pairedPartial = snapOrder != null && !pairedComplete
    && orderPartialEvidence(snapOrder);
  const pairedRejected = snapOrder != null && !pairedComplete
    && !pairedPartial && snapOrder.submit_phase === "submit_rejected";
  const pairedTerminalNoFill = snapOrder != null && !pairedComplete
    && !pairedPartial && !pairedRejected && !!snapOrder.terminal_status;
  const unpaired = snapOrder == null;
  const unpairedUndetermined = unpaired
    && UNDETERMINED_CHAIN_PHASES.has(phase ?? "");
  const historical = replaced || intentDead || pairedComplete
    || (pairedPartial && !!snapOrder?.terminal_status) || pairedTerminalNoFill
    || pairedRejected || unpairedUndetermined;
  let head: string;
  if (replaced) head = `历史快照（已被 ${supersededBy} 替代）`;
  else if (intentDead) {
    head = `历史快照（${deadIntentReason(classification, stage)}）`;
  } else if (pairedComplete) {
    head = "历史快照（整单完成）";
  } else if (pairedPartial && !!snapOrder?.terminal_status) {
    const st = snapOrder?.terminal_status ?? "";
    head = (st === "canceled" || st === "cancelled")
      ? "历史快照（部分成交，余单已取消）"
      : `历史快照（部分成交，余单已终结：${st}）`;
  } else if (pairedPartial) {
    head = "提交时快照（部分成交，剩余订单未终结）";
  } else if (pairedRejected) {
    head = "历史快照（该次提交被拒绝）";
  } else if (pairedTerminalNoFill) {
    head = `历史快照（订单已终结未成交：${snapOrder?.terminal_status ?? ""}）`;
  } else if (unpairedUndetermined) {
    head = "sizing 快照（对应订单/终态未能确定——余单存活与终结均不作断言）";
  } else head = "sizing 快照";
  const margins = [
    `现金 ${money(cash)}`,
    `敞口余量 ${money(s.exposure_room)}`,
    `行业/主题有效余量 ${money(s.sector_theme_room)}`,
    `组合止损风险余量 ${pctOrNA(s.stop_risk_room_pct)} / ${money(s.stop_risk_room_notional)}`,
    `单仓上限 ${money(s.position_cap)}`,
    `折扣前 ${money(s.pre_haircut_notional)} / 折扣后 ${money(s.post_haircut_notional)}`,
    `可用预算 ${money(available)}`,
    `最低仓位 ${money(min)}`,
  ].join(" · ");
  const tail: string[] = [];
  const constraints = s.binding_constraints ?? [];
  if (constraints.length) tail.push(`限制项：${constraints.join("、")}`);
  if (s.reject_code) tail.push(`拒绝码 ${s.reject_code}`);
  const belowMin = available != null && min != null && available < min;
  // R8 (iteration-3 review): 意图保留 is a claim about the INTENT lifecycle —
  // only alive-waiting classifications (waiting/created_pending) on a
  // non-terminal chain may say it. Dead intents and order-bound chains never do.
  if (belowMin && !historical && ALIVE_INTENT_CLASSES.has(classification ?? "")) {
    tail.push("本次 sizing 未通过（可用预算低于最低仓位），意图保留待后续尝试");
  }
  return (
    <div className="reasoning muted">
      {/* R8：快照自带事件时间与 attempt 身份（views._funnel_sizing_snapshot
          已暴露 event_ts / attempt_id），页面只转述，不重算不编造。 */}
      <div>{head} · 事件 {snap.event_kind ?? "未知"}
        {snap.event_ts ? ` · 快照时间 ${snap.event_ts}` : ""}
        {snap.attempt_id ? ` · 尝试 ${snap.attempt_id}` : ""}
        （数值来自当时快照，未重算）</div>
      <div>{margins}</div>
      {tail.length > 0 && <div>{tail.join(" · ")}</div>}
    </div>
  );
}

function ChainAttempts({ c }: {
  c: { attempts?: number | null; wait_events?: number | null;
       created_runs?: number | null; observed_runs?: number | null };
}) {
  // Attempts (flush-stage runs) and wait events are different counters, and
  // creation / status-observation runs are counted separately (R6).
  if ((c.attempts ?? 0) === 0 && (c.wait_events ?? 0) === 0) return null;
  const extra: string[] = [];
  if ((c.created_runs ?? 0) > 0) extra.push(`创建 ${c.created_runs}`);
  if ((c.observed_runs ?? 0) > 0) extra.push(`状态观察 ${c.observed_runs}`);
  return (
    <span className="muted mono">
      尝试 {c.attempts ?? 0} 次 · 等待 {c.wait_events ?? 0} 次
      {extra.length > 0 ? `（${extra.join("、")}另计）` : ""}
    </span>
  );
}

function ChainCard({ chain }: { chain: FunnelChain }) {
  return (
    <div className="decision-card">
      <div className="decision-head">
        <span className="sym">{chain.symbol ?? "?"}</span>
        <span className="badge wait">{funnelClassLabel(chain.classification)}</span>
        <span className="muted">阶段 {chain.stage ?? "未知"}</span>
        {chain.phase && (
          <span className="muted">状态 {ORDER_PHASE_LABEL[chain.phase] ?? chain.phase}</span>
        )}
        <ChainAttempts c={chain} />
        {chain.replaced_previous_version && (
          <span className="muted">替换了前版本 {chain.replaced_previous_version}</span>
        )}
      </div>
      {chain.reason && <div className="reasoning">{chain.reason}</div>}
      {(chain.orders ?? []).map((o, i) => <OrderLine key={o.order_id ?? i} order={o} />)}
      {chain.sizing && (
        <SizingLine snap={chain.sizing}
          supersededBy={chain.superseded_by} phase={chain.phase}
          classification={chain.classification} stage={chain.stage}
          orders={chain.orders} />
      )}
    </div>
  );
}

function CarryoverCard({ entry }: { entry: FunnelCarryover }) {
  return (
    <div className="decision-card">
      <div className="decision-head">
        <span className="sym">{entry.symbol ?? "?"}</span>
        <span className="badge wait">{funnelClassLabel(entry.classification)}</span>
        <span className="muted">阶段 {entry.stage ?? "未知"}</span>
        {entry.phase && (
          <span className="muted">状态 {ORDER_PHASE_LABEL[entry.phase] ?? entry.phase}</span>
        )}
        <ChainAttempts c={entry} />
        <span className="muted">创建于 {entry.created_on ?? "未知"}</span>
      </div>
      {entry.reason && <div className="reasoning">{entry.reason}</div>}
      {(entry.orders ?? []).map((o, i) => <OrderLine key={o.order_id ?? i} order={o} />)}
      {entry.sizing && (
        <SizingLine snap={entry.sizing}
          supersededBy={entry.superseded_by} phase={entry.phase}
          classification={entry.classification} stage={entry.stage}
          orders={entry.orders} />
      )}
    </div>
  );
}

function FunnelSummaryCards({ s }: { s: FunnelSummaryCounts }) {
  const num = (v?: number | null) => (v == null ? "—" : String(v));
  return (
    <div className="cards">
      <Kpi label="BUY 决策" value={num(s.decisions)} small />
      <Kpi label="已建意图" value={num(s.with_intent)} small />
      <Kpi label="已提交" value={s.submitted_frac ?? "—"} small />
      <Kpi label="券商确认成交" value={num(s.filled_verified)} small />
      <Kpi label="部分成交" value={num(s.partial)} small />
      <Kpi label="提交被拒" value={num(s.submit_rejected)} small />
      <Kpi label="结果不明" value={num(s.submit_unknown)} small />
      <Kpi label="已被替代" value={num(s.superseded)} small />
      <Kpi label="等待中" value={num(s.waiting)} small />
      <Kpi label="明确未建" value={num(s.not_created)} small />
      <Kpi label="证据缺失" value={num(s.evidence_missing)} small />
    </div>
  );
}

// R6-B: identity-less history is grouped by mode. "unsplit" is the backend's
// reference bucket for mode-NULL rows — rendered as 未分模式 and NEVER folded
// into the page's own mode (paper / dry_run / unsplit stay apart).
const modeBucketLabel = (m?: string | null): string =>
  (!m || m === "unsplit") ? "未分模式" : m;

function FunnelDegradedNotes({ funnel }: { funnel: FunnelPayload }) {
  const d = funnel.degraded;
  const notes: string[] = [];
  if (d?.fills?.degraded) {
    notes.push(`fills 不可用：${d.fills.reason ?? "券商读取失败"}——成交证据缺失，不代表零成交`);
  }
  if (d?.fills?.truncated) notes.push("fills 读取被截断——匹配可能不全");
  const legacy = d?.legacy_flush_events ?? 0;
  if (legacy > 0) notes.push(`旧行无身份：${legacy} 条（只按 kind 计数，不强行配对）`);
  const dupes = d?.duplicate_fill_activities ?? 0;
  if (dupes > 0) notes.push(`重复 fill 活动（按 activity id 去重）：${dupes} 条`);
  const postCutoff = d?.post_cutoff_fills ?? 0;
  if (postCutoff > 0) notes.push(`截止时间之后的成交（不入证据）：${postCutoff} 条`);
  // R6-B: identity-less rows BY MODE — grouped counts first, then each row
  // tagged with its own mode. Never one conflated "已观察" line.
  const byMode = Object.entries(d?.identityless_by_mode ?? {});
  if (byMode.length > 0) {
    notes.push(`身份缺失记录（不补造版本、不成链、不入分母）：${
      byMode.map(([k, n]) => `${modeBucketLabel(k)} ${n} 条`).join("、")}`);
  }
  for (const row of d?.identityless_observed_today ?? []) {
    notes.push(`身份缺失[${modeBucketLabel(row.mode)}] ${row.symbol ?? "?"} · ${row.kind ?? "?"}`);
  }
  // R6-B: order events that could not be attributed — order_id + reason,
  // straight from the backend's structured list.
  for (const u of d?.unattributed_order_events ?? []) {
    notes.push(`无法归因订单事件[${modeBucketLabel(u.mode)}] ${u.symbol ?? "?"}`
      + ` · 订单 ${u.order_id ?? "?"} · ${u.kind ?? "?"} · ${u.note ?? "原因未知"}`);
  }
  const unattributed = d?.unattributed_decision_events ?? 0;
  if (unattributed > 0) notes.push(`${unattributed} 条 BUY 决策事件无法归因到任何 run/mode`);
  const badTs = d?.unparseable_timestamps ?? 0;
  if (badTs > 0) notes.push(`${badTs} 条时间戳不可解析`);
  const badPayload = d?.unparseable_payloads ?? 0;
  if (badPayload > 0) notes.push(`${badPayload} 条 payload 不可解析`);
  for (const o of d?.orphan_intents ?? []) {
    notes.push(`孤儿意图 ${o.symbol ?? "?"}：${o.note ?? "今日创建但对应 decision_buy 缺失，不计分母"}`);
  }
  for (const u of d?.unknown_origin_intents ?? []) {
    notes.push(`来源不明意图 ${u.symbol ?? "?"}：${u.note ?? "找不到创建事件"}`);
  }
  for (const n of d?.notes ?? []) notes.push(n);
  if (!notes.length) return null;
  return <p className="note">{notes.join("；")}</p>;
}

function FunnelPanel({ funnel, funnelError }: {
  funnel: FunnelPayload | null;
  funnelError?: string | null;
}) {
  const schemaDegraded = (funnel?.schema_degraded ?? false) && funnel != null;
  return (
    <div className="panel">
      <h3>执行漏斗{funnel?.session_date ? `（${funnel.session_date} · ${funnel.mode ?? "?"}）` : ""}</h3>
      {!funnel && funnelError && (
        <p className="muted">漏斗不可用：{funnelError}——其余页面不受影响，不代表今天没有买入链。</p>
      )}
      {!funnel && !funnelError && <p className="muted">漏斗加载中…</p>}
      {schemaDegraded && (
        <>
          <p className="muted">
            旧数据/降级：{funnel.schema_reason ?? "journal schema 过旧"}
            ——无法做身份级漏斗，只按事件类型计数，不伪造零。
          </p>
          {funnel.legacy_event_counts && Object.keys(funnel.legacy_event_counts).length > 0 && (
            <table>
              <thead><tr><th>事件类型</th><th>今日条数</th></tr></thead>
              <tbody>
                {Object.entries(funnel.legacy_event_counts).map(([kind, n]) => (
                  <tr key={kind}><td>{kind}</td><td className="mono">{n}</td></tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
      {funnel && !schemaDegraded && (
        <>
          {funnel.summary && <FunnelSummaryCards s={funnel.summary} />}
          {(() => {
            const reasons = funnel.summary?.not_created_reasons;
            if (!reasons || Object.keys(reasons).length === 0) return null;
            return (
              <p className="muted">
                未建原因：{Object.entries(reasons).map(([k, v]) => `${k} ×${v}`).join("、")}
              </p>
            );
          })()}
          {/* R6-B: the empty state may only claim what the evidence shows.
              Un-attributable or identity-less records mean the chain could
              not be BUILT — they are not proof that no BUY happened. Every
              degraded class the backend reports participates: un-attributed
              order events / BUY decisions, identity-less rows, orphan and
              unknown-origin intents, unparseable timestamps / payloads, and
              unlinked fills. Any one of them downgrades the conclusion to
              "没有可完整归因的执行链" plus the concrete reasons. */}
          {(() => {
            if ((funnel.chains ?? []).length > 0 || (funnel.carryover ?? []).length > 0) return null;
            const d = funnel.degraded;
            const reasons: string[] = [];
            if ((d?.unattributed_order_events ?? []).length > 0) {
              reasons.push("存在无法归因的订单事件");
            }
            if ((d?.identityless_observed_today ?? []).length > 0
              || Object.keys(d?.identityless_by_mode ?? {}).length > 0) {
              reasons.push("存在意图身份缺失的记录");
            }
            if ((d?.unattributed_decision_events ?? 0) > 0) {
              reasons.push("存在无法归因的 BUY 决策事件");
            }
            if ((d?.orphan_intents ?? []).length > 0) {
              reasons.push("存在孤儿意图（对应 decision_buy 缺失）");
            }
            if ((d?.unknown_origin_intents ?? []).length > 0) {
              reasons.push("存在来源不明意图");
            }
            if ((d?.unparseable_timestamps ?? 0) > 0) {
              reasons.push("存在时间戳不可解析的记录");
            }
            if ((d?.unparseable_payloads ?? 0) > 0) {
              reasons.push("存在 payload 不可解析的记录");
            }
            if ((funnel.unlinked_fills ?? []).length > 0) {
              reasons.push("存在未归因成交");
            }
            return reasons.length > 0
              ? <p className="muted">本会话没有可完整归因的执行链——{reasons.join("；")}；不能把无法还原等同于没有发生。</p>
              : <p className="muted">本会话没有 BUY 决策，也没有承接意图。</p>;
          })()}
          {(funnel.chains ?? []).map((c, i) => (
            <ChainCard key={`${c.decision_key ?? c.run_id ?? i}:${c.symbol ?? i}`} chain={c} />
          ))}
          {(funnel.carryover ?? []).length > 0 && (
            <div>
              <h4>承接意图（往日创建、今日继续；不算今天分母）</h4>
              {(funnel.carryover ?? []).map((entry, i) => (
                <CarryoverCard key={entry.intent_id ?? i} entry={entry} />
              ))}
            </div>
          )}
          {/* R4/R5：order_id 归属不明的今日成交——正面证据单独列出，不并入
              分母、不造链、也不丢弃。R6-B：标题中性（不预设"台账中无订单
              事件"——归属冲突、身份缺失提交、无台账订单是不同的案型），
              每行显示后端自己的 note。 */}
          {(funnel.unlinked_fills ?? []).length > 0 && (
            <div>
              <h4>未归因成交</h4>
              <table>
                <thead>
                  <tr><th>时间</th><th>标的</th><th>方向</th><th>数量</th><th>金额</th><th>订单</th><th>无法归因原因</th></tr>
                </thead>
                <tbody>
                  {(funnel.unlinked_fills ?? []).map((f, i) => (
                    <tr key={f.activity_id ?? i}>
                      <td>{shortTime(f.transaction_time ?? null)}</td>
                      <td>{f.symbol ?? "—"}</td>
                      <td className={f.side === "buy" ? "pos" : "neg"}>{f.side ?? "—"}</td>
                      <td className="mono">{f.qty ?? "—"}</td>
                      <td className="mono">{f.notional != null ? usd(f.notional, 2) : "—"}</td>
                      <td className="mono">{f.order_id ?? "—"}</td>
                      <td>{f.note ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {/* R6-B: the run-cap skip shows the verbatim detail AND the
              structured unprocessed remainder as a list — the affected
              symbols are data, not something to be re-parsed out of prose. */}
          {(funnel.run_skips ?? []).length > 0 && (
            <div>
              <h4>本轮全局跳过</h4>
              {(funnel.run_skips ?? []).map((r, i) => (
                <p className="muted" key={`${r.run_id ?? "?"}-${i}`}>
                  {r.run_id ?? "?"}（{r.reason ?? "原因未知"}）
                  {r.detail ? ` — ${r.detail}` : ""}
                  {(r.unprocessed ?? []).length > 0
                    ? ` · 未处理：${(r.unprocessed ?? []).join("、")}` : ""}
                </p>
              ))}
            </div>
          )}
        </>
      )}
      {funnel && <FunnelDegradedNotes funnel={funnel} />}
    </div>
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
