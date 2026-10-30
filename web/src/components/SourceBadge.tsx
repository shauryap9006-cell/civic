import React from "react";

interface SourceBadgeProps {
  source: "live" | "replay" | "seed";
  showLive?: boolean;
}

export const SourceBadge: React.FC<SourceBadgeProps> = ({
  source,
  showLive = false,
}) => {
  if (source === "live" && !showLive) {
    return null;
  }

  const badgeStyles = {
    seed: "bg-purple-50 text-purple-700 border-purple-200",
    replay: "bg-blue-50 text-blue-700 border-blue-200",
    live: "bg-emerald-50 text-emerald-700 border-emerald-200",
  }[source];

  const labels = {
    seed: "Seed Data",
    replay: "Replay",
    live: "Live",
  }[source];

  return (
    <span
      className={`inline-flex items-center text-[10px] uppercase tracking-wider font-semibold px-2 py-0.5 rounded border ${badgeStyles}`}
      title={
        source === "seed"
          ? "Seeded synthetic data for evaluation"
          : source === "replay"
          ? "Recorded dashcam drive replayed through system"
          : "Live phone detection"
      }
    >
      {labels}
    </span>
  );
};
