import { Composition } from "remotion";
import { AgenticTradingIntro } from "./AgenticTradingIntro";

export const MyComposition: React.FC = () => {
  return (
    <Composition
      id="AgenticTradingIntro"
      component={AgenticTradingIntro}
      durationInFrames={600}
      fps={30}
      width={1280}
      height={720}
    />
  );
};
