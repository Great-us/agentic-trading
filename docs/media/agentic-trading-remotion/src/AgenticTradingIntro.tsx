import React from "react";
import {
  AbsoluteFill,
  Easing,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { slide } from "@remotion/transitions/slide";

const ink = "#071A2B";
const paper = "#EAF2F5";
const verified = "#19B5A5";
const checkpoint = "#F7B955";
const muted = "#A9BDC8";
const cjk = '"Microsoft YaHei", "Noto Sans CJK SC", sans-serif';
const mono = '"Cascadia Mono", Consolas, monospace';
const serif = "Georgia, serif";
const sceneFrames = 124;
const timing = linearTiming({ durationInFrames: 5 });

type AnimatedProps = {
  children: React.ReactNode;
  delay?: number;
  x?: number;
  y?: number;
  scale?: number;
  style?: React.CSSProperties;
};

const Animated: React.FC<AnimatedProps> = ({
  children,
  delay = 0,
  x = 0,
  y = 28,
  scale = 1,
  style,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const progress = interpolate(frame, [delay, delay + Math.round(fps * 0.6)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });
  return (
    <div
      style={{
        opacity: progress,
        translate: `${interpolate(progress, [0, 1], [x, 0])}px ${interpolate(progress, [0, 1], [y, 0])}px`,
        scale: interpolate(progress, [0, 1], [scale, 1]),
        ...style,
      }}
    >
      {children}
    </div>
  );
};

const Base: React.FC<{
  chapter: string;
  chinese: string;
  ghost: string;
  children: React.ReactNode;
  footerLeft: string;
  footerRight: string;
}> = ({ chapter, chinese, ghost, children, footerLeft, footerRight }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  return (
    <AbsoluteFill style={{ backgroundColor: ink, color: paper, overflow: "hidden", fontFamily: mono }}>
      <div
        style={{
          position: "absolute",
          right: -18,
          bottom: -56,
          color: "rgba(234, 242, 245, 0.15)",
          fontFamily: serif,
          fontSize: 176,
          letterSpacing: -8,
          lineHeight: 0.8,
          opacity: interpolate(frame, [0, 18, durationInFrames - 18, durationInFrames - 1], [0, 1, 1, 0], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
          }),
        }}
      >
        {ghost}
      </div>
      <div style={{ display: "flex", flexDirection: "column", height: "100%", padding: "48px 72px 42px", boxSizing: "border-box", position: "relative" }}>
        <Animated delay={3} x={-36} y={0}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", color: muted, fontSize: 15, letterSpacing: 2.3, textTransform: "uppercase" }}>
            <span style={{ color: verified }}>{chapter}</span>
            <span style={{ fontFamily: cjk, letterSpacing: 1.3, textTransform: "none" }}>{chinese}</span>
          </div>
        </Animated>
        <Animated delay={8} x={0} y={0} style={{ width: "100%" }}>
          <div style={{ backgroundColor: verified, height: 2, marginTop: 18 }} />
        </Animated>
        {children}
        <Animated delay={39} y={8} style={{ marginTop: "auto" }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 24, color: muted, fontFamily: cjk, fontSize: 13, lineHeight: 1.4 }}>
            <span>{footerLeft}</span>
            <span style={{ fontFamily: mono, textAlign: "right" }}>{footerRight}</span>
          </div>
        </Animated>
      </div>
    </AbsoluteFill>
  );
};

const Intro: React.FC = () => {
  return (
    <Base chapter="OPEN SOURCE · US EQUITIES" chinese="开源 · 美股系统" ghost="AUDIT" footerLeft="P1 · 股票多头模拟交易 · LONG-ONLY EQUITIES PAPER TRADING" footerRight="MIT LICENSE · README INTRO">
      <div style={{ marginTop: "auto", paddingBottom: 54 }}>
        <Animated delay={15} x={-60} y={0}>
          <h1 style={{ fontFamily: serif, fontSize: 102, fontWeight: 400, letterSpacing: -5, lineHeight: 0.86, margin: 0 }}>Agentic<br />Trading</h1>
        </Animated>
        <Animated delay={24} x={-30} y={0}>
          <div style={{ marginTop: 14, fontFamily: cjk, fontSize: 30, fontWeight: 700, letterSpacing: 1.4 }}>可审计的 AI 辅助美股多头模拟交易系统</div>
        </Animated>
        <Animated delay={32} y={28}>
          <div style={{ marginTop: 22, maxWidth: 900, color: muted, fontSize: 20, lineHeight: 1.45 }}>Quant signals → AI analysis → risk controls → Alpaca paper execution → audit trail.</div>
        </Animated>
        <Animated delay={37} x={24} y={0} style={{ position: "absolute", right: 72, top: 52 }}>
          <div style={{ backgroundColor: checkpoint, color: ink, padding: "8px 12px", fontSize: 13, fontWeight: 800, letterSpacing: 1.4 }}>PAPER ONLY</div>
        </Animated>
      </div>
    </Base>
  );
};

const Signals: React.FC = () => {
  const card = (title: string, body: string, delay: number, x: number) => (
    <Animated key={title} delay={delay} x={x} y={0} style={{ flex: 1 }}>
      <div style={{ minHeight: 174, border: `1px solid ${muted}`, padding: 22, boxSizing: "border-box", backgroundColor: "rgba(7, 26, 43, .65)", display: "flex", flexDirection: "column" }}>
        <div style={{ color: verified, fontSize: 13, letterSpacing: 1.8 }}>{title}</div>
        <div style={{ marginTop: "auto", fontFamily: serif, fontSize: 44, lineHeight: 0.92 }}>{body}</div>
      </div>
    </Animated>
  );
  return (
    <Base chapter="01 / QUANT SIGNALS" chinese="量化信号筛选" ghost="SIGNAL" footerLeft="所有视觉数据均为合成示例" footerRight="PAPER ONLY · VALIDATION IN PROGRESS">
      <div style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "center", gap: 25 }}>
        <div style={{ display: "flex", gap: 20 }}>{card("MARKET REGIME", "Filter", 16, -64)}{card("TREND + MOMENTUM", "Screen", 22, 0)}{card("LIQUIDITY + ELIGIBILITY", "Check", 28, 64)}</div>
        <Animated delay={35} y={34}>
          <div style={{ borderLeft: `2px solid ${checkpoint}`, padding: "13px 0 13px 18px", color: muted, fontFamily: cjk, fontSize: 19, lineHeight: 1.45 }}>合成流程示意 · 规则化输入先筛选候选项。<br />Illustrative workflow only — a candidate is not a trade.</div>
        </Animated>
      </div>
    </Base>
  );
};

const Analyst: React.FC = () => {
  const rows = ["STANCE · 立场", "CONFIDENCE · 置信度", "RATIONALE · 理由", "RISK FLAGS · 风险标记"];
  return (
    <Base chapter="02 / AI ANALYST" chinese="结构化分析，不是自动信任" ghost="REVIEW" footerLeft="模型输出可回溯 · 不代表投资建议" footerRight="EXAMPLE DATA · PAPER ONLY">
      <div style={{ flex: 1, display: "grid", gridTemplateColumns: "1.18fr .82fr", alignItems: "center", gap: 34 }}>
        <Animated delay={16} y={48}>
          <div style={{ border: `1px solid ${muted}`, backgroundColor: "rgba(234, 242, 245, .06)", padding: 30, minHeight: 252, boxSizing: "border-box", display: "flex", flexDirection: "column" }}>
            <div style={{ color: verified, fontSize: 13, letterSpacing: 1.8 }}>ANALYST OUTPUT · EXAMPLE FORMAT</div>
            <div style={{ marginTop: 25, fontFamily: cjk, fontSize: 25, lineHeight: 1.5 }}>AI 结合信号、新闻与基本面，输出立场、置信度、理由与风险标记。</div>
            <div style={{ marginTop: "auto", color: muted, fontFamily: cjk, fontSize: 14, lineHeight: 1.45 }}>按配置调用 AI 分析，交易仍需通过风控。<br />AI analysis follows configuration; trades remain subject to risk checks.</div>
          </div>
        </Animated>
        <div style={{ borderLeft: `2px solid ${checkpoint}`, paddingLeft: 24, display: "flex", flexDirection: "column", gap: 18 }}>
          {rows.map((row, index) => <Animated key={row} delay={20 + index * 5} x={40} y={0}><div style={{ paddingBottom: 12, borderBottom: `1px solid rgba(169, 189, 200, .42)`, color: index === 3 ? checkpoint : paper, fontFamily: cjk, fontSize: 20 }}>{row}</div></Animated>)}
        </div>
      </div>
    </Base>
  );
};

const Risk: React.FC = () => {
  const cards = [
    ["LIMIT 01", "Position\nsizing", "仓位大小由账户、止损与配置限制共同约束。", "SIZE IS CAPPED"],
    ["LIMIT 02", "Exposure\ncaps", "体制、组合敞口与行业相关性都可能否决新增风险。", "BUY CAN BE VETOED"],
    ["LIMIT 03", "Broker-side\nstops", "尝试提交并检查止损覆盖；不保证挂单、成交或价格。", "SUBMISSION + COVERAGE CHECKS · NOT GUARANTEED"],
  ];
  return (
    <Base chapter="03 / RISK GATE" chinese="风控优先于下单" ghost="LIMIT" footerLeft="尝试提交并检查止损覆盖；不保证挂单、成交或价格" footerRight="PAPER-FORWARD VALIDATION IN PROGRESS">
      <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 18 }}>
        {cards.map(([index, title, copy, status], cardIndex) => <Animated key={index} delay={16 + cardIndex * 6} x={cardIndex === 0 ? -45 : cardIndex === 2 ? 45 : 0} y={cardIndex === 1 ? 45 : 0} style={{ flex: 1 }}><div style={{ minHeight: 264, borderTop: `7px solid ${checkpoint}`, backgroundColor: "#102A3B", padding: 22, boxSizing: "border-box", display: "flex", flexDirection: "column" }}><div style={{ color: checkpoint, fontSize: 13, letterSpacing: 1.8 }}>{index}</div><div style={{ marginTop: 16, whiteSpace: "pre-line", fontFamily: serif, fontSize: 38, lineHeight: .94 }}>{title}</div><div style={{ marginTop: 16, color: muted, fontFamily: cjk, fontSize: 17, lineHeight: 1.45 }}>{copy}</div><div style={{ marginTop: "auto", color: verified, fontSize: 12, letterSpacing: 1.2 }}>{status}</div></div></Animated>)}
      </div>
      <Animated delay={36} y={0} scale={0.92}>
        <div style={{ backgroundColor: checkpoint, color: ink, padding: "14px 18px", fontFamily: cjk, fontSize: 18, fontWeight: 800 }}>A BUY MUST PASS EVERY LIMIT · 单个买入必须通过全部限制</div>
      </Animated>
    </Base>
  );
};

const Audit: React.FC = () => {
  const steps = [
    ["A / EXECUTE", "Alpaca\npaper", "只连接 Alpaca 模拟账户；不使用真实资金或实盘订单。", "PAPER ACCOUNT ONLY"],
    ["B / RECONCILE", "Intent\nto fill", "订单意图、成交与持仓对账分层记录，异常不被静默忽略。", "TRACEABLE EVENTS"],
    ["C / INSPECT", "Read-only\ndashboard", "本地只读仪表盘展示周期、风控、决策与对账证据。", "LOCAL · READ ONLY"],
  ];
  return (
    <Base chapter="04 / EXECUTE + AUDIT" chinese="模拟执行与可追溯性" ghost="TRACE" footerLeft="开源代码可审阅 · MIT" footerRight="NO VERIFIED PROFITABILITY CLAIMS">
      <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 16 }}>
        {steps.map(([index, title, copy, status], stepIndex) => <React.Fragment key={index}><Animated delay={16 + stepIndex * 8} y={stepIndex === 1 ? 40 : 0} x={stepIndex === 0 ? -45 : stepIndex === 2 ? 45 : 0} style={{ flex: 1 }}><div style={{ minHeight: 270, border: `1px solid ${muted}`, padding: 22, boxSizing: "border-box", backgroundColor: "rgba(7, 26, 43, .72)", display: "flex", flexDirection: "column" }}><div style={{ color: verified, fontSize: 13, letterSpacing: 1.8 }}>{index}</div><div style={{ marginTop: 18, whiteSpace: "pre-line", fontFamily: serif, fontSize: 39, lineHeight: .94 }}>{title}</div><div style={{ marginTop: 18, color: muted, fontFamily: cjk, fontSize: 17, lineHeight: 1.45 }}>{copy}</div><div style={{ marginTop: "auto", color: checkpoint, fontSize: 12, letterSpacing: 1.1 }}>{status}</div></div></Animated>{stepIndex < 2 ? <Animated delay={21 + stepIndex * 8} x={-20} y={0}><div style={{ width: 28, height: 2, backgroundColor: checkpoint }} /></Animated> : null}</React.Fragment>)}
      </div>
      <Animated delay={39} x={0} y={24}>
        <div style={{ color: verified, fontSize: 19 }}>github.com/Great-us/agentic-trading</div>
      </Animated>
      <Animated delay={44} y={0} scale={0.94}>
        <div style={{ marginTop: 13, backgroundColor: checkpoint, color: ink, padding: "13px 16px", fontFamily: cjk, fontSize: 16, fontWeight: 800 }}>PAPER TRADING ONLY · 仅模拟交易 · SYNTHETIC VISUAL DATA · 合成视觉数据</div>
      </Animated>
    </Base>
  );
};

export const AgenticTradingIntro: React.FC = () => {
  return (
    <TransitionSeries>
      <TransitionSeries.Sequence durationInFrames={sceneFrames} name="Intro"><Intro /></TransitionSeries.Sequence>
      <TransitionSeries.Transition presentation={slide({ direction: "from-right" })} timing={timing} />
      <TransitionSeries.Sequence durationInFrames={sceneFrames} name="Quant signals"><Signals /></TransitionSeries.Sequence>
      <TransitionSeries.Transition presentation={slide({ direction: "from-bottom" })} timing={timing} />
      <TransitionSeries.Sequence durationInFrames={sceneFrames} name="AI analyst"><Analyst /></TransitionSeries.Sequence>
      <TransitionSeries.Transition presentation={slide({ direction: "from-right" })} timing={timing} />
      <TransitionSeries.Sequence durationInFrames={sceneFrames} name="Risk gate"><Risk /></TransitionSeries.Sequence>
      <TransitionSeries.Transition presentation={slide({ direction: "from-bottom" })} timing={timing} />
      <TransitionSeries.Sequence durationInFrames={sceneFrames} name="Paper execution and audit"><Audit /></TransitionSeries.Sequence>
    </TransitionSeries>
  );
};
