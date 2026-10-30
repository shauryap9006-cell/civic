"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  ArrowLeft,
  Calendar,
  Clock,
  MapPin,
  CheckCircle2,
  XCircle,
  HelpCircle,
  ShieldAlert,
  Car,
  Image as ImageIcon,
} from "lucide-react";
import { PotholeDetail, CoverageItem, StatusHistoryItem } from "../../../lib/types";
import { getPothole } from "../../../lib/api";
import {
  STATUS_TOKENS,
  SEVERITY_CONFIG,
  SEVERITY_TOOLTIP,
} from "../../../lib/tokens";
import { StatusBadge } from "../../../components/StatusBadge";
import { SourceBadge } from "../../../components/SourceBadge";

const PASS_REASON_LABELS: Record<string, string> = {
  ok: "Clear view, no pothole",
  blurred: "Image too blurry",
  too_dark: "Too dark",
  occluded: "View blocked by vehicles",
  detector_inactive: "Detector was not running",
  low_inference_fps: "Detector too slow",
  poor_gps: "GPS not precise enough",
  too_fast: "Driving too fast to be reliable",
  insufficient_window_samples: "Not enough good views of the spot",
  detection_in_pass: "A detection was recorded",
};

const TIMELINE_REASON_LABELS: Record<string, (authority?: string) => string> = {
  first_event: () => "First detected",
  report_sent: (auth) => `Reported to ${auth || "road authority"}`,
  fixed_claim: () => "Authority claimed it is fixed",
  clean_passes_met: () => "Confirmed by clean drives",
  no_valid_clean_pass: () => "Later drives could not confirm the repair",
  contradicting_event: () => "New detection after the repair claim",
  reopen_report_sent: () => "Authority notified again",
  reminder_sent: () => "Reminder sent",
  escalation_sent: () => "Escalated",
};

export default function PotholeDetailPage() {
  const params = useParams();
  const id = params?.id as string;

  const [pothole, setPothole] = useState<PotholeDetail | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    async function load() {
      try {
        const data = await getPothole(id);
        setPothole(data);
      } catch (err) {
        console.error("Failed to fetch pothole details:", err);
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [id]);

  if (loading) {
    return (
      <div className="max-w-4xl mx-auto px-4 py-16 text-center text-slate-500">
        Loading evidence record...
      </div>
    );
  }

  if (!pothole) {
    return (
      <div className="max-w-4xl mx-auto px-4 py-16 text-center">
        <h2 className="text-xl font-bold text-slate-800">Record not found</h2>
        <p className="text-sm text-slate-500 mt-2">
          The requested pothole identifier does not exist.
        </p>
        <Link
          href="/"
          className="inline-flex items-center space-x-1.5 mt-4 text-emerald-600 hover:underline text-sm font-medium"
        >
          <ArrowLeft className="w-4 h-4" />
          <span>Back to map</span>
        </Link>
      </div>
    );
  }

  const sev = SEVERITY_CONFIG[pothole.severity] || SEVERITY_CONFIG.low;
  const isReopened = pothole.status === "reopened";
  const isVerified = pothole.status === "verified";

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 py-8 space-y-8">
      {/* Navigation Breadcrumb */}
      <div>
        <Link
          href="/"
          className="inline-flex items-center space-x-1.5 text-xs font-semibold text-slate-500 hover:text-slate-800 transition"
        >
          <ArrowLeft className="w-4 h-4" />
          <span>Back to Map View</span>
        </Link>
      </div>

      {/* 1. Header Section */}
      <section className="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <div className="flex items-center space-x-2">
            <StatusBadge status={pothole.status} size="lg" />
            <SourceBadge source={pothole.source_badge} />
          </div>
          <span
            className={`text-xs font-semibold px-2.5 py-1 rounded-full border ${sev.badgeClass}`}
            title={SEVERITY_TOOLTIP}
          >
            {sev.label}
          </span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs text-slate-600 border-t border-slate-100 pt-4">
          <div className="space-y-2">
            <div className="flex items-center space-x-2">
              <MapPin className="w-4 h-4 text-slate-400" />
              <span className="font-mono font-medium text-slate-800">
                {pothole.lat.toFixed(5)}, {pothole.lon.toFixed(5)}
              </span>
            </div>
            {pothole.authority_name && (
              <div className="flex items-center space-x-2 text-slate-700">
                <ShieldAlert className="w-4 h-4 text-slate-400" />
                <span>Responsible: {pothole.authority_name}</span>
              </div>
            )}
          </div>
          <div className="space-y-2">
            <div className="flex items-center space-x-2">
              <Calendar className="w-4 h-4 text-slate-400" />
              <span>
                First detected:{" "}
                {new Date(pothole.first_seen_at).toLocaleString()}
              </span>
            </div>
            <div className="flex items-center space-x-2">
              <Clock className="w-4 h-4 text-slate-400" />
              <span>
                Latest sighting:{" "}
                {new Date(pothole.last_event_at).toLocaleString()}
              </span>
            </div>
          </div>
        </div>
      </section>

      {/* 2. Evidence Photos Section */}
      <section className="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs space-y-4">
        <h2 className="text-sm font-bold uppercase tracking-wider text-slate-800 flex items-center space-x-2">
          <ImageIcon className="w-4 h-4 text-slate-500" />
          <span>Evidence Imagery</span>
        </h2>

        {isReopened ? (
          /* Reopened: Two columns Before and After */
          <div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="bg-slate-50 p-4 rounded-xl border border-slate-200">
                <span className="text-xs font-bold uppercase tracking-wide text-slate-700 block mb-2">
                  Before Repair Claim
                </span>
                <div className="aspect-video bg-slate-200 rounded-lg flex items-center justify-center text-slate-400 overflow-hidden">
                  {pothole.before_event?.crop_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={pothole.before_event.crop_url}
                      alt="Pothole before repair claim"
                      className="w-full h-full object-cover"
                    />
                  ) : (
                    <span className="text-xs">Road Crop (Before Claim)</span>
                  )}
                </div>
                <p className="text-[11px] text-slate-500 mt-2">
                  Captured:{" "}
                  {pothole.claimed_fixed_at
                    ? new Date(pothole.claimed_fixed_at).toLocaleDateString()
                    : "Prior to claim"}
                </p>
              </div>

              <div className="bg-rose-50/50 p-4 rounded-xl border border-rose-200">
                <span className="text-xs font-bold uppercase tracking-wide text-rose-800 block mb-2">
                  After Repair Claim (Contradicting Detection)
                </span>
                <div className="aspect-video bg-rose-100 rounded-lg flex items-center justify-center text-rose-400 overflow-hidden">
                  {pothole.after_events && pothole.after_events.length > 0 && pothole.after_events[0].crop_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={pothole.after_events[0].crop_url}
                      alt="Pothole after repair claim"
                      className="w-full h-full object-cover"
                    />
                  ) : (
                    <span className="text-xs text-rose-700">
                      New Detection Road Crop
                    </span>
                  )}
                </div>
                <p className="text-[11px] text-rose-700 mt-2">
                  Why reopened: A new detection was captured after the repair claim.
                </p>
              </div>
            </div>
          </div>
        ) : isVerified ? (
          /* Verified: Before image only + pass verification text */
          <div className="space-y-3">
            <div className="max-w-md bg-slate-50 p-4 rounded-xl border border-slate-200">
              <span className="text-xs font-bold uppercase tracking-wide text-slate-700 block mb-2">
                Original Detection Crop
              </span>
              <div className="aspect-video bg-slate-200 rounded-lg flex items-center justify-center text-slate-400 overflow-hidden">
                {pothole.before_event?.crop_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={pothole.before_event.crop_url}
                    alt="Original pothole detection crop"
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <span className="text-xs">Original Road Crop</span>
                )}
              </div>
            </div>
            <div className="bg-emerald-50 border border-emerald-200 text-emerald-800 p-3.5 rounded-xl text-xs flex items-center space-x-2.5">
              <CheckCircle2 className="w-5 h-5 text-emerald-600 shrink-0" />
              <span>
                Repair successfully confirmed: No pothole was seen in later drives with good camera conditions. (No artificial &ldquo;after&rdquo; photo is manufactured).
              </span>
            </div>
          </div>
        ) : (
          /* Open / Reported / Claimed / Unknown: Best crop + events */
          <div className="space-y-3">
            <div className="max-w-md bg-slate-50 p-4 rounded-xl border border-slate-200">
              <span className="text-xs font-bold uppercase tracking-wide text-slate-700 block mb-2">
                Best Detection Crop
              </span>
              <div className="aspect-video bg-slate-200 rounded-lg flex items-center justify-center text-slate-400 overflow-hidden">
                {pothole.thumbnail_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={pothole.thumbnail_url}
                    alt="Pothole crop"
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <span className="text-xs">Road-only Camera Crop</span>
                )}
              </div>
              <p className="text-[11px] text-slate-400 mt-2">
                Road-surface region of interest recorded by driver dashcam.
              </p>
            </div>
          </div>
        )}
      </section>

      {/* 3. Timeline from status_history */}
      <section className="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs space-y-4">
        <h2 className="text-sm font-bold uppercase tracking-wider text-slate-800 flex items-center space-x-2">
          <Clock className="w-4 h-4 text-slate-500" />
          <span>Status Lifecycle Timeline</span>
        </h2>

        <div className="relative pl-6 border-l-2 border-slate-200 space-y-6 my-2">
          {pothole.history && pothole.history.length > 0 ? (
            pothole.history.map((h: StatusHistoryItem, idx: number) => {
              const token = STATUS_TOKENS[h.to_status] || STATUS_TOKENS.open;
              const Icon = token.icon;
              const reasonLabelFn = TIMELINE_REASON_LABELS[h.reason];
              const reasonText = reasonLabelFn
                ? reasonLabelFn(pothole.authority_name)
                : h.reason;

              return (
                <div key={idx} className="relative">
                  {/* Pin Circle */}
                  <div
                    style={{ backgroundColor: token.colorHex }}
                    className="absolute -left-[31px] top-0.5 w-4 h-4 rounded-full border-2 border-white ring-2 ring-slate-100 flex items-center justify-center"
                  />
                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="text-xs font-bold text-slate-800">
                        {reasonText}
                      </span>
                      <StatusBadge status={h.to_status} size="sm" />
                    </div>
                    <span className="text-[11px] text-slate-400 block mt-0.5">
                      {new Date(h.at).toLocaleString()}
                    </span>
                  </div>
                </div>
              );
            })
          ) : (
            <p className="text-xs text-slate-400">No transition history logged yet.</p>
          )}
        </div>
      </section>

      {/* 4. Passes after the claim */}
      <section className="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-bold uppercase tracking-wider text-slate-800 flex items-center space-x-2">
            <Car className="w-4 h-4 text-slate-500" />
            <span>Post-Claim Verification Passes</span>
          </h2>
          <span className="text-xs text-slate-500">
            Required clean drives: {pothole.required_clean_passes ?? 2}
          </span>
        </div>

        {pothole.coverage_after_claim && pothole.coverage_after_claim.length > 0 ? (
          <div className="overflow-x-auto border border-slate-200 rounded-xl">
            <table className="min-w-full divide-y divide-slate-200 text-xs">
              <thead className="bg-slate-50 text-slate-600 font-semibold text-left">
                <tr>
                  <th className="py-2.5 px-3">Crossing Time</th>
                  <th className="py-2.5 px-3">Verification Result</th>
                  <th className="py-2.5 px-3">Quality Reason</th>
                  <th className="py-2.5 px-3">Window Samples</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 text-slate-700">
                {pothole.coverage_after_claim.map((cov: CoverageItem, idx: number) => (
                  <tr key={idx} className="hover:bg-slate-50/50">
                    <td className="py-2.5 px-3 font-mono text-[11px]">
                      {new Date(cov.crossing_at).toLocaleString()}
                    </td>
                    <td className="py-2.5 px-3">
                      {cov.clean ? (
                        <span className="inline-flex items-center space-x-1 text-emerald-700 font-medium">
                          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" />
                          <span>Valid Clean Pass</span>
                        </span>
                      ) : (
                        <span className="inline-flex items-center space-x-1 text-amber-700 font-medium">
                          <XCircle className="w-3.5 h-3.5 text-amber-600" />
                          <span>Not Usable</span>
                        </span>
                      )}
                    </td>
                    <td className="py-2.5 px-3 text-slate-600">
                      {PASS_REASON_LABELS[cov.reason] || cov.reason}
                    </td>
                    <td className="py-2.5 px-3 font-mono text-[11px] text-slate-500">
                      {cov.window_samples ?? 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="bg-slate-50 p-4 rounded-xl border border-slate-200 text-xs text-slate-500">
            No subsequent drives have passed this location since the claim was made. Later driver passes will automatically verify repair status.
          </div>
        )}

        {/* 5. Occlusion check note if disabled */}
        {!pothole.occlusion_check_enabled && (
          <p className="text-[11px] text-slate-400 italic">
            Blocked views were not checked in this version.
          </p>
        )}
      </section>

      {/* 6. Honest Footer Note */}
      <div className="text-[11px] text-slate-400 bg-slate-50 p-4 rounded-xl border border-slate-200 space-y-1">
        <p className="font-semibold text-slate-500 flex items-center space-x-1">
          <HelpCircle className="w-3.5 h-3.5 text-slate-400" />
          <span>Measurement & Location Accuracy</span>
        </p>
        <p>
          Severity is a graded estimate, not a physical depth measurement. Locations come from phone GPS and can vary by several metres depending on environmental conditions.
        </p>
      </div>
    </div>
  );
}
