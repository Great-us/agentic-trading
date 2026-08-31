"""Phase-0 design mock for the four dashboard blind spots.

Reads the live journal read-only and emits a self-contained HTML to
tmp/dashboard-demo.html (derived output, gitignored - rerun to regenerate).
Writes nothing into src/, starts no server.
Every number on the page comes from this run - nothing is hand-typed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "journal.db"
HB = ROOT / "data" / "heartbeat.json"
OUT = ROOT / "tmp" / "dashboard-demo.html"

# Mirrors heartbeat.py:53-54 - referenced, not re-invented.
FAST_MAX_AGE_MINUTES = 35
DEEP_MAX_AGE_HOURS = 26
# Mirrors run.py:729.
TRADE_INTENT_TTL_HOURS = 72

# Mirrors daily_report.py VETO_CATEGORIES verbatim (label, sql, source).
VETO_CATEGORIES = [
    ("风控否决买入（size / sector / exposure 等）",
     "d.action='buy' AND d.order_status IS NULL AND d.reasoning LIKE '%Risk manager vetoed buy:%'",
     "decisions.reasoning"),
    ("买单被券商拒绝",
     "d.action='buy' AND d.order_status='rejected'", "decisions.order_status"),
    ("卖出失败 / 被拒（仓位仍持有）",
     "d.action='sell' AND d.order_status='rejected'", "decisions.order_status"),
    ("每周期新单上限跳过",
     "d.reasoning LIKE '%max_new_orders_per_cycle reached%'", "decisions.reasoning"),
    ("macro regime UNKNOWN 禁止开仓",
     "d.reasoning LIKE '%macro regime UNKNOWN%'", "decisions.reasoning"),
    ("缺 LLM 结论，BUY 降级 WAIT（fail closed）",
     "d.reasoning LIKE '%new entries fail closed%'", "decisions.reasoning"),
]
REFERENCE_CATEGORIES = [
    ("BUY 转 TradeIntent 排队（非否决）",
     "d.order_status='intent'", "decisions.order_status"),
    ("收盘后卖出排队次日执行（非否决）",
     "d.order_status='queued_closed'", "decisions.order_status"),
]
INTENT_EVENT_KINDS = [
    ("gap", "跳空拦截（live 价超出 signal 价 max_entry_gap_atr 个 ATR）", "丢弃"),
    ("chase_signal", "Chase 拦截（对比信号价，cap 1.0%）", "保留"),
    ("chase_open", "Chase 拦截（对比今日开盘，cap 1.5%）", "保留"),
    ("sizing", "Flush 阶段 sizing 否决（敞口 / book 风险 / sector cap）", "保留"),
    ("ttl", "TradeIntent TTL 过期", "丢弃"),
]


def connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_age(delta: timedelta) -> str:
    secs = int(delta.total_seconds())
    if secs < 3600:
        return f"{secs // 60} 分钟"
    if secs < 86400:
        return f"{secs / 3600:.1f} 小时"
    return f"{secs / 86400:.1f} 天"


def fmt_countdown(delta: timedelta) -> str:
    """Hours all the way out to the 72h TTL - a countdown rounded to '1.0 天'
    hides whether an intent dies tonight or tomorrow afternoon."""
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)} 分钟"
    return f"{hours:.1f} 小时"


def missed_sessions(stamp: datetime, now: datetime) -> int:
    """Weekdays strictly after the stamp's date, up to and including today.

    This is the distinction DEEP_MAX_AGE_HOURS cannot make: 46h of silence
    across a weekend means the market was shut, not that the scheduler died.
    """
    day = stamp.date() + timedelta(days=1)
    missed = 0
    while day <= now.date():
        if day.weekday() < 5:
            missed += 1
        day += timedelta(days=1)
    return missed


def build_heartbeat(hb: dict, now: datetime) -> str:
    rows = []
    for mode, label, limit_txt in (
        ("deep", "深周期", f"{DEEP_MAX_AGE_HOURS} 小时"),
        ("fast", "快周期", f"{FAST_MAX_AGE_MINUTES} 分钟"),
    ):
        entry = hb.get(mode) or {}
        stamp_s = entry.get("timestamp")
        if not stamp_s:
            rows.append(f'<tr><td>{label}</td><td colspan="5" class="muted">无记录</td></tr>')
            continue
        stamp = datetime.fromisoformat(stamp_s)
        age = now - stamp
        missed = missed_sessions(stamp, now)
        raw_stale = (age > timedelta(hours=DEEP_MAX_AGE_HOURS) if mode == "deep"
                     else age > timedelta(minutes=FAST_MAX_AGE_MINUTES))
        if missed == 0:
            state = "正常" if not raw_stale else "休市中"
            cls = "ok" if not raw_stale else "idle"
        else:
            state = f"停摆 · 漏掉 {missed} 个交易日"
            cls = "bad"
        cid = entry.get("cycle_id")
        rows.append(
            f'<tr><td>{label}</td><td class="mono">{escape(stamp_s[:19].replace("T", " "))}Z</td>'
            f'<td class="mono">{cid if cid is not None else "&mdash;"}</td>'
            f'<td class="mono">{fmt_age(age)}</td><td class="mono muted">{limit_txt}</td>'
            f'<td><span class="badge hb-{cls}">{escape(state)}</span></td></tr>')

    deep_entry = hb.get("deep") or {}
    stops = deep_entry.get("stops_covered")
    total_pos = deep_entry.get("positions")
    stop_txt = (f"{stops} / {total_pos}" if stops is not None else
                '<span class="muted">&mdash;（本次心跳写入时'
                '该字段尚未启用）</span>')
    deep_age = fmt_age(now - datetime.fromisoformat(deep_entry["timestamp"]))
    return f"""<div class="panel">
  <h3>① 系统心跳 <span class="tag">新增</span></h3>
  <table><thead><tr><th>层</th><th>最近戳记 (UTC)</th><th>cycle</th>
    <th>距今</th><th>阈值</th><th>状态</th></tr></thead>
  <tbody>{"".join(rows)}</tbody></table>
  <div class="kv"><span class="muted">保护性止损覆盖</span> {stop_txt}</div>
  <div class="warn-box" style="margin-top:12px">
    ⚠️ <b>为什么不能直接按阈值报警</b>：deep 已静默 {deep_age}，
    超过 <code>DEEP_MAX_AGE_HOURS = 26</code>（<code>heartbeat.py:53-54</code>）。
    但上个交易日是周五 08-28，今天是周日 &mdash;&mdash;
    漏掉的交易日数为 <b>0</b>。面板按「漏掉几个交易日」判定，
    而不是按小时数，否则每个周末都会误报一次停摆。
  </div>
  <div class="note">数据源：<code>data/heartbeat.json</code>（<code>heartbeat.py:74-122</code>
    原子写入）。阈值常量引用自 <code>heartbeat.py:53-54</code>，未在本页硬编码。</div>
</div>"""


def build_intents(intents: list[dict], now: datetime) -> str:
    rows = []
    for it in intents:
        created = datetime.fromisoformat(it["created_at"])
        not_before = datetime.fromisoformat(it["not_before"])
        expires = created + timedelta(hours=TRADE_INTENT_TTL_HOURS)
        if now >= expires:
            state, cls = "TTL 已过期", "bad"
        elif now < not_before:
            state, cls = "窗口未开", "idle"
        else:
            state, cls = "窗口已开 · 待执行", "ok"
        left = expires - now
        left_cls = "neg" if left < timedelta(hours=24) else ""
        rows.append(
            f'<tr><td><b>{escape(it["symbol"])}</b></td>'
            f'<td class="mono">${it["signal_price"]:,.2f}</td>'
            f'<td class="mono muted">{it["atr14"]:.2f}</td>'
            f'<td class="mono">{it["quant_score"]:+.3f}</td>'
            f'<td class="mono"><b>{it["combined_score"]:+.3f}</b></td>'
            f'<td class="mono muted">{escape(it["not_before"][:16].replace("T", " "))}Z</td>'
            f'<td class="mono {left_cls}">{fmt_countdown(left)}</td>'
            f'<td><span class="badge hb-{cls}">{escape(state)}</span></td></tr>'
            f'<tr class="sub"><td></td><td colspan="7" class="reasoning">'
            f'{escape(it["reasoning"])}</td></tr>')
    return f"""<div class="panel">
  <h3>② 待执行意图 · TradeIntent <span class="tag">新增</span>
    <span class="count">{len(intents)}</span></h3>
  <table><thead><tr><th>符号</th><th>信号价</th><th>ATR14</th><th>quant</th>
    <th>combined</th><th>窗口开启 (UTC)</th><th>TTL 剩余</th><th>状态</th></tr></thead>
  <tbody>{"".join(rows)}</tbody></table>
  <div class="warn-box" style="margin-top:12px">
    ⚠️ 这两条的执行窗口<b>早已开启</b>（08-29 14:00Z = 10:00 ET），
    TTL 也还没到，但系统自 08-28 起没再跑过任何 cycle &mdash;&mdash;
    于是它们就这么<b>悬着</b>，现有 dashboard 上完全看不到。
    这正是这块面板存在的理由。
  </div>
  <div class="note">数据源：<code>trade_intents</code> 表（<code>journal/logger.py:369-385</code>
    写入）。TTL 72h 取自 <code>run.py:729</code>；窗口 10:00–15:30 ET 见
    <code>config/risk.yaml</code>。表内不记录成交 &mdash;&mdash; 成交只在券商侧。</div>
</div>"""


def build_vetoes(vetoes: list[tuple], refs: list[tuple], ievents: list[tuple]) -> str:
    vmax = max([t for *_, t in vetoes] + [1])
    vrows = "".join(
        f'<tr><td>{escape(lbl)}</td><td class="mono">{r}</td><td class="mono"><b>{t}</b></td>'
        f'<td class="barcell"><span class="bar" style="width:{t / vmax * 100:.1f}%"></span></td>'
        f'<td class="muted src">{escape(src)}</td></tr>'
        for lbl, src, r, t in vetoes)
    rrows = "".join(
        f'<tr><td class="muted">{escape(lbl)}</td><td class="mono">{r}</td>'
        f'<td class="mono">{t}</td><td colspan="2" class="muted src">{escape(src)}</td></tr>'
        for lbl, src, r, t in refs)
    erows = "".join(
        f'<tr><td><code>{escape(k)}</code></td><td class="lbl">{escape(lbl)}</td>'
        f'<td class="mono">{"&mdash;" if n == 0 else n}</td>'
        f'<td><span class="chip">{escape(fate)}</span></td></tr>'
        for k, lbl, fate, n in ievents)
    return f"""<div class="panel">
  <h3>③ 意图拦截 · 两层 <span class="tag">新增</span></h3>
  <div class="grid2">
    <div>
      <h4>决策层否决<span class="muted"> · 近 7 日 / 累计</span></h4>
      <table><thead><tr><th>类别</th><th>7日</th><th>累计</th><th></th>
        <th>来源</th></tr></thead>
      <tbody>{vrows}<tr class="spacer"><td colspan="5"></td></tr>{rrows}</tbody></table>
    </div>
    <div>
      <h4>执行层否决<span class="muted"> · intent_events</span></h4>
      <table><thead><tr><th>kind</th><th>含义</th><th>累计</th>
        <th>intent 去向</th></tr></thead><tbody>{erows}</tbody></table>
      <div class="empty-state">
        <b>五类全为 0 &mdash;&mdash; 这是真实的空态，不是占位数据。</b><br>
        <code>intent_events</code> 表由上一次 commit 新建，<b>2026-08-30 起才开始落库</b>，
        而系统最后一次运行是 08-28。此前的 flush 拦截只存在于
        <code>logs/*.log</code> 的非结构化文本里。下一个 cycle 跑完就会有数据。
      </div>
    </div>
  </div>
  <div class="note">决策层 SQL 谓词逐字复用 <code>daily_report.py:135-149</code> 的
    <code>VETO_CATEGORIES</code>（每条都能对着 journal 复核）；
    执行层写入点见 <code>run.py:735/760/776/793/822</code>。</div>
</div>"""


def build_outcomes(outcomes: list[dict]) -> str:
    def cell(val, n):
        if n == 0 or val is None:
            return '<td class="mono muted">n/a<span class="nn">n=0</span></td>'
        cls = "pos" if val > 0 else ("neg" if val < 0 else "")
        warn = " thin" if n < 30 else ""
        flag = "!" if n < 5 else ""
        return (f'<td class="mono {cls}">{val:+.2f}%'
                f'<span class="nn{warn}">n={n}{flag}</span></td>')

    rows = "".join(
        f'<tr><td><span class="badge {escape(o["a"])}">{escape(o["a"]).upper()}</span></td>'
        f'<td class="mono">{o["n"]}</td>'
        f'{cell(o["r1"], o["n1"])}{cell(o["r5"], o["n5"])}{cell(o["r20"], o["n20"])}'
        f'<td class="mono pos">{o["mfe"]:+.2f}%</td>'
        f'<td class="mono neg">{o["mae"]:+.2f}%</td></tr>'
        for o in outcomes)
    return f"""<div class="panel">
  <h3>④ 信号验证 · signal_outcomes
    <span class="tag done">后端已就绪</span></h3>
  <table><thead><tr><th>动作</th><th>决策数</th><th>+1d</th><th>+5d</th>
    <th>+20d</th><th>MFE 20d</th><th>MAE 20d</th></tr></thead><tbody>{rows}</tbody></table>
  <div class="warn-box" style="margin-top:12px">
    ⚠️ <b>每个 horizon 的样本数不同，禁止跨行直接比较。</b>
    HOLD 桶 192 条决策里只有 <b>43</b> 条评出了 +1d，而 AVOID 是 <b>293</b>/326；
    <code>+20d 全部为空（0/703）</code>，因为 20 个交易日的持有期还没走完。
    WAIT 桶 +5d 显示 <b>+4.78%</b>，但 <b>n 只有 5</b> &mdash;&mdash;
    这是噪声，不是发现。
  </div>
  <div class="note">数据源：<code>signal_outcomes</code> 表（<code>journal/evaluate.py</code>
    回填）。现有 <code>/api/books/&#123;id&#125;/signal-outcomes</code> 端点与
    <code>views.py:109</code> 的 <code>outcomes_summary()</code> <b>早已写好且已上线</b>，
    <code>api.ts:147</code> 也定义了客户端 &mdash;&mdash; 但没有任何页面调用它。
    本面板即为那个缺失的消费端，唯一需要的后端改动是补上 per-horizon 的 n。</div>
</div>"""


CSS = """
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
--accent:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;font-size:14px}
.shell{max-width:1200px;margin:0 auto;padding:0 20px 60px}
header{display:flex;align-items:center;gap:24px;padding:14px 0;
border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:10}
.brand{font-weight:700;font-size:16px;letter-spacing:.5px}
.book-tabs{display:flex;gap:4px}
.book-tab{padding:6px 14px;border-radius:6px;color:var(--muted);border:1px solid transparent}
.book-tab.active{color:var(--text);border-color:var(--border);background:var(--panel)}
.page-nav{display:flex;gap:2px;margin:18px 0;flex-wrap:wrap}
.page-nav span{padding:7px 13px;border-radius:6px 6px 0 0;color:var(--muted);
border-bottom:2px solid transparent}
.page-nav span.new{color:var(--accent);border-bottom-color:var(--accent)}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;
padding:16px;margin-bottom:18px}
.panel h3{margin:0 0 12px;font-size:14px;color:var(--muted);font-weight:600}
.panel h4{margin:0 0 10px;font-size:13px;color:var(--text);font-weight:600}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:24px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{text-align:right;color:var(--muted);font-size:12px;font-weight:500;padding:8px 10px;
border-bottom:1px solid var(--border)}
td{text-align:right;padding:8px 10px;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
td.lbl{text-align:left;font-size:12px;color:var(--muted)}
tr:hover td{background:rgba(88,166,255,.04)}
tr.sub td,tr.sub:hover td{background:none;padding-top:0}
tr.spacer td{border:none;height:10px}
.pos{color:var(--green)}.neg{color:var(--red)}.muted{color:var(--muted)}
.mono{font-variant-numeric:tabular-nums}
.reasoning{text-align:left;font-size:12.5px;color:var(--muted);line-height:1.5}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.badge.buy{background:rgba(63,185,80,.15);color:var(--green)}
.badge.hold,.badge.wait,.badge.avoid{background:rgba(139,148,158,.15);color:var(--muted)}
.badge.hb-ok{background:rgba(63,185,80,.15);color:var(--green)}
.badge.hb-idle{background:rgba(210,153,34,.15);color:var(--amber)}
.badge.hb-bad{background:rgba(248,81,73,.15);color:var(--red)}
.chip{background:var(--bg);border:1px solid var(--border);border-radius:12px;
padding:2px 10px;font-size:12px}
.tag{background:rgba(88,166,255,.15);color:var(--accent);border-radius:10px;
padding:2px 8px;font-size:11px;font-weight:600}
.tag.done{background:rgba(63,185,80,.15);color:var(--green)}
.count{color:var(--muted);font-weight:400}
.note{color:var(--muted);font-size:12px;margin-top:10px;line-height:1.6}
.warn-box{border:1px solid var(--amber);color:var(--amber);border-radius:8px;
padding:10px 14px;font-size:13px;line-height:1.65}
.empty-state{border:1px dashed var(--border);border-radius:8px;padding:12px 14px;
margin-top:12px;color:var(--muted);font-size:12.5px;line-height:1.65}
.demo-banner{border:1px solid var(--accent);border-radius:8px;padding:12px 16px;
margin:18px 0;font-size:13px;color:var(--accent);line-height:1.75}
code{background:var(--bg);border:1px solid var(--border);border-radius:4px;
padding:1px 5px;font-size:12px}
.barcell{width:120px}
.bar{display:block;height:6px;background:var(--accent);border-radius:3px;opacity:.55}
.src{font-size:11px}
.nn{display:block;font-size:10.5px;color:var(--muted);font-weight:400}
.nn.thin{color:var(--amber)}
.kv{margin-top:10px;font-size:13px}
"""


def main() -> int:
    now = datetime.now(timezone.utc)
    conn = connect_ro(DB)
    hb = json.loads(HB.read_text(encoding="utf-8"))
    cycle = dict(conn.execute(
        "SELECT id, timestamp, mode, equity, cash, regime_score, regime_label "
        "FROM cycles ORDER BY id DESC LIMIT 1").fetchone())
    intents = [dict(r) for r in conn.execute(
        "SELECT * FROM trade_intents ORDER BY combined_score DESC")]
    cutoff = (now - timedelta(days=7)).isoformat()

    def counts(cond: str) -> tuple[int, int]:
        total = conn.execute(
            f"SELECT COUNT(*) FROM decisions d WHERE {cond}").fetchone()[0]
        recent = conn.execute(
            f"SELECT COUNT(*) FROM decisions d JOIN cycles c ON c.id = d.cycle_id "
            f"WHERE ({cond}) AND c.timestamp >= ?", (cutoff,)).fetchone()[0]
        return recent, total

    vetoes = [(lbl, src, *counts(sql)) for lbl, sql, src in VETO_CATEGORIES]
    refs = [(lbl, src, *counts(sql)) for lbl, sql, src in REFERENCE_CATEGORIES]
    ievents = [
        (k, lbl, fate,
         conn.execute("SELECT COUNT(*) FROM intent_events WHERE kind = ?",
                      (k,)).fetchone()[0])
        for k, lbl, fate in INTENT_EVENT_KINDS
    ]
    outcomes = [dict(r) for r in conn.execute(
        "SELECT d.action a, COUNT(*) n, COUNT(o.ret_1d) n1, COUNT(o.ret_5d) n5,"
        " COUNT(o.ret_20d) n20, AVG(o.ret_1d)*100 r1, AVG(o.ret_5d)*100 r5,"
        " AVG(o.ret_20d)*100 r20, AVG(o.mfe_20d)*100 mfe, AVG(o.mae_20d)*100 mae"
        " FROM signal_outcomes o JOIN decisions d ON d.id = o.decision_id"
        " GROUP BY d.action ORDER BY n DESC")]
    conn.close()

    body = (build_heartbeat(hb, now) + build_intents(intents, now)
            + build_vetoes(vetoes, refs, ievents) + build_outcomes(outcomes))

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Dashboard 盲区补全 · 设计稿</title>
<style>{CSS}</style></head>
<body><div class="shell">
<header><span class="brand">Agentic Trading</span>
  <nav class="book-tabs">
    <span class="book-tab active">一号盘 · 成长池</span>
    <span class="book-tab">二号盘 · 主题轮动</span>
    <span class="book-tab">双盘对比</span>
  </nav>
</header>
<nav class="page-nav">
  <span>总览</span><span>持仓</span><span>交易记录</span>
  <span>决策链</span><span class="new">意图与拦截 ●</span>
  <span class="new">信号验证 ●</span><span>策略逻辑</span>
</nav>
<div class="demo-banner">
  <b>Phase 0 设计稿</b> · 生成于 {now.strftime('%Y-%m-%d %H:%M')} UTC ·
  页面上<b>每一个数字都来自本机
  <code>data/journal.db</code>（<code>mode=ro</code>）与
  <code>data/heartbeat.json</code></b>，由 <code>tmp/gen_demo.py</code>
  在生成时读取，无一手写、无一编造。
  空的地方就显示空。本页不改 <code>src/</code>
  任何文件，也不启动服务。<br>
  当前账面：cycle <b>#{cycle['id']}</b> ·
  净值 <b>${cycle['equity']:,.2f}</b> ·
  现金 <b>${cycle['cash']:,.2f}</b> ·
  体制 <b>{cycle['regime_label']}</b>（{cycle['regime_score']:+.3f}）
</div>
{body}
<div class="note" style="text-align:center;margin-top:28px">
  设计 token 取自 <code>frontend/src/index.css</code> &mdash;&mdash;
  与现有 7 个页面同一套配色、字号、
  <code>tabular-nums</code> 与 1200px 定宽。
</div>
</div></body></html>"""
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT}  ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
