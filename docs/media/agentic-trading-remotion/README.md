# Agentic Trading README introduction

Remotion source for the bilingual 20-second GitHub introduction animation.

The composition is intentionally an explanatory, synthetic-data visual: it makes
no return or profitability claim, depicts P1 long-only US-equities paper trading
on Alpaca, and labels the paper-only / validation-in-progress status on screen.

## Operational boundaries depicted

The stop card describes an operational control, not an execution guarantee.
Protective stops are attempted and reconciled at the start and end of active
cycles. After submission, the broker adapter re-reads the order when it can; if
that read fails, it retains the submit response. Reported resting status and
quantity are then used for the coverage check. None of this guarantees that an
order stays resting, fills, or fills at a given price.

## Deliverables

- `renders/agentic-trading-intro.gif` — README-ready loop, 960×540 at 12fps.
- `renders/agentic-trading-intro.mp4` — source-quality 1280×720 H.264 export.

## Re-render

```powershell
npm install
.\node_modules\.bin\remotion.cmd render AgenticTradingIntro renders\agentic-trading-intro.mp4 --codec=h264 --crf=18 --concurrency=2
ffmpeg -y -i renders\agentic-trading-intro.mp4 -vf "fps=12,scale=960:-2:flags=lanczos,palettegen=stats_mode=diff" renders\palette.png
ffmpeg -y -i renders\agentic-trading-intro.mp4 -i renders\palette.png -filter_complex "fps=12,scale=960:-2:flags=lanczos[x];[x][1:v]paletteuse=dither=sierra2_4a" -loop 0 renders\agentic-trading-intro.gif
Remove-Item renders\palette.png
```

The GIF is intentionally derived from the MP4 at 960px wide / 12fps to keep it
small enough for a repository page.
