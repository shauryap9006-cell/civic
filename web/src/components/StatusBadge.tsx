import React from "react";
import { PotholeStatus } from "../lib/types";
import { getStatusToken } from "../lib/tokens";

interface StatusBadgeProps {
  status: PotholeStatus;
  showTooltip?: boolean;
  size?: "sm" | "md" | "lg";
}

export const StatusBadge: React.FC<StatusBadgeProps> = ({
  status,
  showTooltip = true,
  size = "md",
}) => {
  const token = getStatusToken(status);
  const Icon = token.icon;

  const sizeClasses = {
    sm: "text-xs px-2 py-0.5 space-x-1",
    md: "text-xs font-medium px-2.5 py-1 space-x-1.5",
    lg: "text-sm font-semibold px-3 py-1.5 space-x-2",
  }[size];

  const iconSizes = {
    sm: "w-3 h-3",
    md: "w-3.5 h-3.5",
    lg: "w-4 h-4",
  }[size];

  return (
    <span
      className={`inline-flex items-center rounded-full border ${token.borderClass} ${token.bgLightClass} ${token.tailwindClass} ${sizeClasses}`}
      title={showTooltip ? token.tooltip : undefined}
    >
      <Icon className={iconSizes} aria-hidden="true" />
      <span>{token.label}</span>
    </span>
  );
};
