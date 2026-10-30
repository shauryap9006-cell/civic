"use client";

import { useEffect, useState } from "react";
import {
  BarChart3,
  CheckCircle2,
  AlertCircle,
  Mail,
  Wrench,
  HelpCircle,
  RotateCcw,
  Navigation,
  Info,
} from "lucide-react";
import { Stats } from "../../lib/types";
import { getStats } from "../../lib/api";
import { STATUS_TOKENS } from "../../lib/tokens";

export default function ImpactPage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [loading, setLoading] = useState(true);
  const [includeSynthetic, setIncludeSynthetic] = useState(true);

  useEffect(() => {
    async function fetchStats() {
      try {
        const data = await getStats();
        setStats(data);
      } catch (err) {
        console.error("Failed to load statistics:", err);
      } finally {
        setLoading(false);
      }
    }
    fetchStats();
  }, []);

  if (loading) {
    return (
      <div className="max-w-6xl mx-auto px-4 py-16 text-center text-slate-500">
        Loading impact statistics...
      </div>
    );
  }

  const total = stats?.potholes_total || 0;
  const verified = stats?.verified || 0;
  const claimed = stats?.claimed_fixed || 0;
  const reported = stats?.reports_sent || 0;
  const reopened = stats?.reopened || 0;
  const unknown = stats?.unknown || 0;
  const km = stats?.km_covered || 0;

  const pct = (val: number) => {
    if (total === 0) return "0.0%";
    return `${((val / total) * 100).toFixed(1)}%`;
  };

  return (
    <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-10 space-y-10">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-slate-200 pb-6">
        <div>
          <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-emerald-700 mb-1">
            <BarChart3 className="w-4 h-4" />
            <span>Civic Impact Dashboard</span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-extrabold text-slate-900 tracking-tight">
            Road Infrastructure & Repair Verification
          </h1>
          <p className="text-xs text-slate-500 mt-1 max-w-2xl">
            Real-time aggregate data on detected road defects, municipal repair claims, and quality-gated verification results.
          </p>
        </div>

        {/* Toggle Replay/Seed */}
        <div className="flex items-center space-x-3 bg-slate-50 border border-slate-200 px-3.5 py-2 rounded-xl text-xs shrink-0 self-start sm:self-auto">
          <label htmlFor="toggle-synthetic" className="text-slate-600 font-medium cursor-pointer">
            Include replay & seed data
          </label>
          <input
            id="toggle-synthetic"
            type="checkbox"
            checked={includeSynthetic}
            onChange={(e) => setIncludeSynthetic(e.target.checked)}
            className="w-4 h-4 accent-emerald-600 rounded cursor-pointer"
          />
        </div>
      </div>

      {/* Main Metric Cards Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
        {/* Total Detected */}
        <div className="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs relative overflow-hidden">
          <div className="flex items-center justify-between text-slate-500 text-xs font-semibold uppercase tracking-wider">
            <span>Potholes Detected</span>
            <AlertCircle className="w-5 h-5 text-[#B5472B]" />
          </div>
          <div className="mt-4 flex items-baseline space-x-2">
            <span className="text-4xl font-extrabold text-slate-900 tracking-tight">
              {total}
            </span>
          </div>
          <p className="text-[11px] text-slate-400 mt-2">
            Aggregate confirmed sightings across connected camera drives.
          </p>
        </div>

        {/* Verified Fixed */}
        <div className="bg-white rounded-2xl border border-emerald-200/80 bg-emerald-50/20 p-6 shadow-xs relative overflow-hidden">
          <div className="flex items-center justify-between text-emerald-800 text-xs font-semibold uppercase tracking-wider">
            <span>Verified Repaired</span>
            <CheckCircle2 className="w-5 h-5 text-[#3F7D4E]" />
          </div>
          <div className="mt-4 flex items-baseline space-x-2">
            <span className="text-4xl font-extrabold text-[#3F7D4E] tracking-tight">
              {verified}
            </span>
            <span className="text-xs font-semibold text-emerald-700">
              ({pct(verified)})
            </span>
          </div>
          <p className="text-[11px] text-emerald-600 mt-2 font-medium">
            Confirmed by multiple quality-gated passes with clear camera views.
          </p>
        </div>

        {/* Reports Sent */}
        <div className="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs relative overflow-hidden">
          <div className="flex items-center justify-between text-slate-500 text-xs font-semibold uppercase tracking-wider">
            <span>Notified to Authorities</span>
            <Mail className="w-5 h-5 text-[#D99A2B]" />
          </div>
          <div className="mt-4 flex items-baseline space-x-2">
            <span className="text-4xl font-extrabold text-slate-900 tracking-tight">
              {reported}
            </span>
            <span className="text-xs font-semibold text-slate-500">
              ({pct(reported)})
            </span>
          </div>
          <p className="text-[11px] text-slate-400 mt-2">
            Formal reports dispatched to municipal road maintenance units.
          </p>
        </div>

        {/* Claimed Fixed */}
        <div className="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs relative overflow-hidden">
          <div className="flex items-center justify-between text-slate-500 text-xs font-semibold uppercase tracking-wider">
            <span>Claimed Fixed (Unverified)</span>
            <Wrench className="w-5 h-5 text-[#8A8F3A]" />
          </div>
          <div className="mt-4 flex items-baseline space-x-2">
            <span className="text-4xl font-extrabold text-slate-900 tracking-tight">
              {claimed}
            </span>
            <span className="text-xs font-semibold text-slate-500">
              ({pct(claimed)})
            </span>
          </div>
          <p className="text-[11px] text-slate-400 mt-2">
            Authorities reported completion. Awaiting independent pass verification.
          </p>
        </div>

        {/* Reopened Defective */}
        <div className="bg-white rounded-2xl border border-rose-200/80 bg-rose-50/20 p-6 shadow-xs relative overflow-hidden">
          <div className="flex items-center justify-between text-rose-800 text-xs font-semibold uppercase tracking-wider">
            <span>Contradicted & Reopened</span>
            <RotateCcw className="w-5 h-5 text-[#8C1F2D]" />
          </div>
          <div className="mt-4 flex items-baseline space-x-2">
            <span className="text-4xl font-extrabold text-[#8C1F2D] tracking-tight">
              {reopened}
            </span>
            <span className="text-xs font-semibold text-rose-700">
              ({pct(reopened)})
            </span>
          </div>
          <p className="text-[11px] text-rose-600 mt-2 font-medium">
            Post-claim sightings detected defects persisting or reappearing.
          </p>
        </div>

        {/* Can't Confirm (Unknown) */}
        <div className="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs relative overflow-hidden">
          <div className="flex items-center justify-between text-slate-500 text-xs font-semibold uppercase tracking-wider">
            <span>Inconclusive (Unknown)</span>
            <HelpCircle className="w-5 h-5 text-[#7A7468]" />
          </div>
          <div className="mt-4 flex items-baseline space-x-2">
            <span className="text-4xl font-extrabold text-slate-900 tracking-tight">
              {unknown}
            </span>
            <span className="text-xs font-semibold text-slate-500">
              ({pct(unknown)})
            </span>
          </div>
          <p className="text-[11px] text-slate-400 mt-2">
            Drives occurred but were too blurry, dark, or occluded to confirm.
          </p>
        </div>
      </div>

      {/* Coverage Distance & Methodology Callout */}
      <div className="bg-slate-900 text-white rounded-2xl p-6 sm:p-8 flex flex-col sm:flex-row items-center justify-between gap-6">
        <div className="space-y-1 text-center sm:text-left">
          <span className="text-xs font-bold uppercase tracking-wider text-emerald-400 flex items-center justify-center sm:justify-start space-x-1.5">
            <Navigation className="w-4 h-4" />
            <span>Survey Distance Monitored</span>
          </span>
          <div className="text-3xl sm:text-4xl font-extrabold font-mono tracking-tight text-white">
            {km.toFixed(1)} km
          </div>
          <p className="text-xs text-slate-400 max-w-lg mt-1">
            Total linear road kilometers covered across continuous 5 Hz video telemetry sessions.
          </p>
        </div>

        <div className="bg-slate-800/80 border border-slate-700/80 rounded-xl p-4 text-xs text-slate-300 max-w-md space-y-2 text-left">
          <div className="font-semibold text-white flex items-center space-x-1.5">
            <Info className="w-4 h-4 text-emerald-400 shrink-0" />
            <span>Transparency Standard</span>
          </div>
          <p className="text-[11px] text-slate-400 leading-relaxed">
            Counts include demo and replay data when shown with a badge. Negative absence is counted as evidence only when all camera, lighting, speed, and GPS quality checks pass.
          </p>
        </div>
      </div>
    </div>
  );
}
