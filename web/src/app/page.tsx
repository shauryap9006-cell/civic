"use client";

import { useEffect, useState, useMemo } from "react";
import Link from "next/link";
import { Filter, Layers, ExternalLink, Calendar, MapPin, Eye } from "lucide-react";
import { PotholeSummary, PotholeStatus } from "../lib/types";
import { listPotholes } from "../lib/api";
import { STATUS_TOKENS, SEVERITY_CONFIG, SEVERITY_TOOLTIP } from "../lib/tokens";
import { StatusBadge } from "../components/StatusBadge";
import { SourceBadge } from "../components/SourceBadge";

export default function MapPage() {
  const [potholes, setPotholes] = useState<PotholeSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedStatuses, setSelectedStatuses] = useState<Set<PotholeStatus>>(
    new Set<PotholeStatus>(["open", "reported", "claimed_fixed", "unknown", "verified", "reopened"])
  );
  const [activePothole, setActivePothole] = useState<PotholeSummary | null>(null);

  useEffect(() => {
    async function loadData() {
      setLoading(true);
      try {
        const data = await listPotholes();
        setPotholes(data);
        if (data.length > 0) {
          setActivePothole(data[0]);
        }
      } catch (err) {
        console.error("Failed to load potholes:", err);
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, []);

  const toggleStatus = (status: PotholeStatus) => {
    setSelectedStatuses((prev) => {
      const next = new Set(prev);
      if (next.has(status)) {
        if (next.size > 1) next.delete(status);
      } else {
        next.add(status);
      }
      return next;
    });
  };

  const statusCounts = useMemo(() => {
    const counts: Record<PotholeStatus, number> = {
      open: 0,
      reported: 0,
      claimed_fixed: 0,
      unknown: 0,
      verified: 0,
      reopened: 0,
    };
    potholes.forEach((p) => {
      if (counts[p.status] !== undefined) {
        counts[p.status]++;
      }
    });
    return counts;
  }, [potholes]);

  const filteredPotholes = useMemo(() => {
    return potholes.filter((p) => selectedStatuses.has(p.status));
  }, [potholes, selectedStatuses]);

  return (
    <div className="flex-1 flex flex-col md:flex-row overflow-hidden relative">
      {/* Sidebar: Filters & Pothole List */}
      <aside className="w-full md:w-96 bg-white border-r border-slate-200 flex flex-col z-10 shrink-0 h-[50vh] md:h-auto overflow-hidden">
        {/* Filter Controls Header */}
        <div className="p-4 border-b border-slate-200 bg-slate-50/70">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-bold uppercase tracking-wider text-slate-700 flex items-center space-x-1.5">
              <Filter className="w-4 h-4 text-slate-500" />
              <span>Status Filter</span>
            </h2>
            <span className="text-xs text-slate-500 font-medium">
              Showing {filteredPotholes.length} of {potholes.length}
            </span>
          </div>

          {/* Filter Checkboxes */}
          <div className="grid grid-cols-2 gap-2">
            {(Object.keys(STATUS_TOKENS) as PotholeStatus[]).map((st) => {
              const token = STATUS_TOKENS[st];
              const Icon = token.icon;
              const isChecked = selectedStatuses.has(st);
              const count = statusCounts[st] || 0;

              return (
                <button
                  key={st}
                  type="button"
                  onClick={() => toggleStatus(st)}
                  className={`flex items-center justify-between px-2.5 py-1.5 rounded-lg border text-xs transition ${
                    isChecked
                      ? `${token.borderClass} ${token.bgLightClass} text-slate-900 font-semibold shadow-xs`
                      : "border-slate-200 bg-white text-slate-400 hover:border-slate-300"
                  }`}
                  title={token.tooltip}
                >
                  <span className="flex items-center space-x-1.5 truncate">
                    <Icon className={`w-3.5 h-3.5 shrink-0 ${isChecked ? token.tailwindClass : "text-slate-400"}`} />
                    <span className="truncate">{token.label}</span>
                  </span>
                  <span className="ml-1 text-[11px] font-mono opacity-80">
                    {count}
                  </span>
                </button>
              );
            })}
          </div>
        </div>

        {/* List of Potholes */}
        <div className="flex-1 overflow-y-auto p-3 space-y-2.5">
          {loading ? (
            <div className="text-center py-12 text-slate-400 text-sm">
              Loading road survey data...
            </div>
          ) : filteredPotholes.length === 0 ? (
            <div className="text-center py-12 px-4 text-slate-500 text-sm">
              <Layers className="w-8 h-8 text-slate-300 mx-auto mb-2" />
              <p className="font-semibold text-slate-700">No potholes match filters</p>
              <p className="text-xs text-slate-400 mt-1">
                Select more statuses above or start a dashcam drive.
              </p>
            </div>
          ) : (
            filteredPotholes.map((p) => {
              const isSelected = activePothole?.id === p.id;
              const sev = SEVERITY_CONFIG[p.severity] || SEVERITY_CONFIG.low;

              return (
                <div
                  key={p.id}
                  onClick={() => setActivePothole(p)}
                  className={`p-3 rounded-xl border text-left cursor-pointer transition ${
                    isSelected
                      ? "border-emerald-500 bg-emerald-50/40 shadow-xs"
                      : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/50"
                  }`}
                >
                  <div className="flex items-center justify-between mb-2">
                    <StatusBadge status={p.status} size="sm" />
                    <SourceBadge source={p.source_badge} />
                  </div>

                  <div className="flex items-center justify-between text-xs text-slate-600 mb-2">
                    <span
                      className={`inline-block px-1.5 py-0.5 rounded text-[11px] font-medium border ${sev.badgeClass}`}
                      title={SEVERITY_TOOLTIP}
                    >
                      {sev.label}
                    </span>
                    <span className="text-[11px] text-slate-400 font-mono">
                      {p.event_count} sighting{p.event_count > 1 ? "s" : ""}
                    </span>
                  </div>

                  <div className="flex items-center justify-between text-[11px] text-slate-500 border-t border-slate-100 pt-2 mt-2">
                    <span className="flex items-center space-x-1 font-mono">
                      <MapPin className="w-3 h-3 text-slate-400" />
                      <span>{p.lat.toFixed(4)}, {p.lon.toFixed(4)}</span>
                    </span>
                    <Link
                      href={`/pothole/${p.id}`}
                      className="text-emerald-600 hover:text-emerald-700 font-medium inline-flex items-center space-x-0.5"
                    >
                      <span>Evidence</span>
                      <ExternalLink className="w-3 h-3" />
                    </Link>
                  </div>
                </div>
              );
            })
          )}
        </div>
      </aside>

      {/* Map Viewport Area */}
      <section className="flex-1 bg-slate-100 flex flex-col relative overflow-hidden">
        {/* Map Header Overlay */}
        <div className="absolute top-4 left-4 z-20 bg-white/95 backdrop-blur px-4 py-2.5 rounded-xl border border-slate-200/90 shadow-sm flex items-center space-x-3">
          <div className="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse" />
          <div className="text-xs">
            <span className="font-semibold text-slate-800">Live Map View</span>
            <span className="text-slate-400 mx-1.5">·</span>
            <span className="text-slate-500 font-mono">Chandigarh Metro (Demo)</span>
          </div>
        </div>

        {/* Selected Pothole Spotlight Card */}
        {activePothole && (
          <div className="absolute bottom-6 left-6 right-6 md:left-auto md:right-6 md:w-96 z-20 bg-white rounded-2xl border border-slate-200/90 shadow-lg p-4 animate-in fade-in slide-in-from-bottom-2 duration-200">
            <div className="flex items-start justify-between mb-2">
              <div>
                <StatusBadge status={activePothole.status} size="md" />
                <div className="mt-1">
                  <SourceBadge source={activePothole.source_badge} />
                </div>
              </div>
              <span
                className={`text-[11px] font-semibold px-2 py-0.5 rounded border ${
                  SEVERITY_CONFIG[activePothole.severity]?.badgeClass
                }`}
                title={SEVERITY_TOOLTIP}
              >
                {SEVERITY_CONFIG[activePothole.severity]?.label}
              </span>
            </div>

            <div className="text-xs text-slate-600 space-y-1.5 my-3">
              <div className="flex items-center space-x-1.5">
                <MapPin className="w-3.5 h-3.5 text-slate-400" />
                <span className="font-mono text-slate-800 font-medium">
                  {activePothole.lat.toFixed(5)}, {activePothole.lon.toFixed(5)}
                </span>
              </div>
              <div className="flex items-center space-x-1.5 text-slate-500">
                <Calendar className="w-3.5 h-3.5 text-slate-400" />
                <span>
                  First seen: {new Date(activePothole.first_seen_at).toLocaleDateString()}
                </span>
              </div>
            </div>

            <Link
              href={`/pothole/${activePothole.id}`}
              className="mt-3 w-full bg-slate-900 hover:bg-slate-800 text-white text-xs font-semibold py-2 px-3 rounded-lg flex items-center justify-center space-x-1.5 transition"
            >
              <Eye className="w-3.5 h-3.5" />
              <span>Inspect Full Repair Evidence</span>
            </Link>
          </div>
        )}

        {/* Interactive Map Visual Backdrop */}
        <div className="flex-1 w-full h-full flex items-center justify-center bg-radial from-slate-200 via-slate-100 to-slate-200/80 p-8 text-center">
          <div className="max-w-md bg-white/80 backdrop-blur p-6 rounded-2xl border border-slate-300/80 shadow-xs">
            <div className="w-12 h-12 bg-emerald-100 text-emerald-700 rounded-full flex items-center justify-center mx-auto mb-3">
              <MapPin className="w-6 h-6" />
            </div>
            <h3 className="text-base font-bold text-slate-800">
              Interactive Map Active
            </h3>
            <p className="text-xs text-slate-500 mt-1">
              Plotting {filteredPotholes.length} verified and reported road conditions with standard OSM tiles. Select any card on the left to inspect camera crops and verification logs.
            </p>
          </div>
        </div>
      </section>
    </div>
  );
}
