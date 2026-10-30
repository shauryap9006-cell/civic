"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import {
  CheckCircle2,
  AlertTriangle,
  FileQuestion,
  Wrench,
  Calendar,
  MapPin,
  Clock,
  ShieldCheck,
} from "lucide-react";
import { FixedPreview, FixedClaimResponse } from "../../../lib/types";
import { previewFixed, claimFixed } from "../../../lib/api";
import { SEVERITY_CONFIG, SEVERITY_TOOLTIP } from "../../../lib/tokens";
import { StatusBadge } from "../../../components/StatusBadge";
import { SourceBadge } from "../../../components/SourceBadge";

export default function AuthorityFixedPage() {
  const params = useParams();
  const token = params?.token as string;

  const [preview, setPreview] = useState<FixedPreview | null>(null);
  const [claimResult, setClaimResult] = useState<FixedClaimResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [note, setNote] = useState("");

  useEffect(() => {
    if (!token) return;
    async function fetchPreview() {
      try {
        const data = await previewFixed(token);
        setPreview(data);
      } catch (err: unknown) {
        const status = (err as { status?: number })?.status || 500;
        setErrorStatus(status);
      } finally {
        setLoading(false);
      }
    }
    fetchPreview();
  }, [token]);

  const handleClaim = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token || submitting) return;
    setSubmitting(true);
    try {
      const res = await claimFixed(token, note.trim() || undefined);
      setClaimResult(res);
    } catch (err: unknown) {
      const status = (err as { status?: number })?.status || 500;
      setErrorStatus(status);
    } finally {
      setSubmitting(false);
    }
  };

  // State: Loading
  if (loading) {
    return (
      <div className="min-h-[70vh] flex items-center justify-center p-4">
        <div className="text-slate-500 text-sm">Validating repair link...</div>
      </div>
    );
  }

  // State: 410 Superseded
  if (errorStatus === 410) {
    return (
      <div className="min-h-[70vh] flex items-center justify-center p-4">
        <div className="max-w-md w-full bg-white rounded-2xl border border-rose-200 shadow-md p-6 text-center space-y-4">
          <div className="w-12 h-12 rounded-full bg-rose-100 text-rose-700 flex items-center justify-center mx-auto">
            <AlertTriangle className="w-6 h-6" />
          </div>
          <h1 className="text-lg font-bold text-slate-800">
            Link Superseded
          </h1>
          <p className="text-xs text-slate-600 leading-relaxed">
            This link is out of date because the pothole was reopened after new evidence. Please use the link in the latest email.
          </p>
        </div>
      </div>
    );
  }

  // State: 404 Not Found
  if (errorStatus === 404 || (!preview && errorStatus !== null)) {
    return (
      <div className="min-h-[70vh] flex items-center justify-center p-4">
        <div className="max-w-md w-full bg-white rounded-2xl border border-slate-200 shadow-md p-6 text-center space-y-4">
          <div className="w-12 h-12 rounded-full bg-slate-100 text-slate-500 flex items-center justify-center mx-auto">
            <FileQuestion className="w-6 h-6" />
          </div>
          <h1 className="text-lg font-bold text-slate-800">
            Link Not Found
          </h1>
          <p className="text-xs text-slate-500">
            We couldn&apos;t find this link. It may have expired or been entered incorrectly.
          </p>
        </div>
      </div>
    );
  }

  // State: Post-claim confirmation
  if (claimResult) {
    return (
      <div className="min-h-[70vh] flex items-center justify-center p-4">
        <div className="max-w-md w-full bg-white rounded-2xl border border-emerald-200 shadow-lg p-6 text-center space-y-4">
          <div className="w-12 h-12 rounded-full bg-emerald-100 text-emerald-700 flex items-center justify-center mx-auto">
            <CheckCircle2 className="w-6 h-6" />
          </div>
          <h1 className="text-lg font-bold text-slate-900">
            Claim Registered
          </h1>
          <div className="bg-emerald-50 border border-emerald-200 text-emerald-900 rounded-xl p-4 text-xs text-left leading-relaxed">
            Thank you. Marked as claimed fixed on{" "}
            <span className="font-semibold">
              {new Date(claimResult.claimed_fixed_at).toLocaleString()}
            </span>
            . CivicLens will verify the repair using later drives. A claim alone is not treated as proof.
          </div>
          <div className="text-[11px] text-slate-400">
            Pothole ID: {claimResult.pothole_id}
          </div>
        </div>
      </div>
    );
  }

  if (!preview) return null;
  const { pothole, can_claim, message } = preview;
  const sev = SEVERITY_CONFIG[pothole.severity] || SEVERITY_CONFIG.low;

  return (
    <div className="min-h-[75vh] flex items-center justify-center p-4 sm:p-6">
      <div className="max-w-lg w-full bg-white rounded-2xl border border-slate-200 shadow-lg p-6 space-y-6">
        {/* Header */}
        <div className="border-b border-slate-100 pb-4">
          <div className="flex items-center space-x-2 text-slate-500 text-xs font-semibold uppercase tracking-wider mb-1">
            <Wrench className="w-4 h-4 text-emerald-600" />
            <span>Authority Repair Confirmation</span>
          </div>
          <h1 className="text-xl font-bold text-slate-900">
            Road Surface Defect Review
          </h1>
        </div>

        {/* Pothole Summary Card */}
        <div className="bg-slate-50 rounded-xl border border-slate-200 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <StatusBadge status={pothole.status} size="md" />
            <span
              className={`text-[11px] font-semibold px-2 py-0.5 rounded border ${sev.badgeClass}`}
              title={SEVERITY_TOOLTIP}
            >
              {sev.label}
            </span>
          </div>

          <div className="space-y-1.5 text-xs text-slate-600">
            <div className="flex items-center space-x-2">
              <MapPin className="w-3.5 h-3.5 text-slate-400 shrink-0" />
              <span className="font-mono text-slate-800 font-medium">
                {pothole.lat.toFixed(5)}, {pothole.lon.toFixed(5)}
              </span>
              <SourceBadge source={pothole.source_badge} />
            </div>
            <div className="flex items-center space-x-2 text-slate-500">
              <Calendar className="w-3.5 h-3.5 text-slate-400 shrink-0" />
              <span>
                Reported on:{" "}
                {new Date(pothole.first_seen_at).toLocaleDateString()}
              </span>
            </div>
          </div>

          {pothole.thumbnail_url && (
            <div className="aspect-video bg-slate-200 rounded-lg overflow-hidden border border-slate-300">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={pothole.thumbnail_url}
                alt="Pothole detection crop"
                className="w-full h-full object-cover"
              />
            </div>
          )}
        </div>

        {/* Action Form or Read-Only State */}
        {can_claim ? (
          <form onSubmit={handleClaim} className="space-y-4">
            <div className="text-xs text-slate-600">
              A driver&apos;s camera detected this pothole. If it has been repaired, please confirm.
            </div>

            <div>
              <label
                htmlFor="note"
                className="block text-xs font-semibold text-slate-700 mb-1"
              >
                Work Order / Note (Optional)
              </label>
              <input
                id="note"
                type="text"
                maxLength={500}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="e.g. Work Order #WO-8912, patched with asphalt"
                className="w-full text-xs px-3 py-2 rounded-lg border border-slate-300 focus:outline-hidden focus:ring-2 focus:ring-emerald-500 focus:border-emerald-500"
              />
            </div>

            <button
              type="submit"
              disabled={submitting}
              className="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-semibold text-sm py-2.5 px-4 rounded-xl shadow-xs transition flex items-center justify-center space-x-2 disabled:opacity-50"
            >
              <ShieldCheck className="w-4 h-4" />
              <span>{submitting ? "Submitting..." : "Mark as fixed"}</span>
            </button>

            <p className="text-[11px] text-slate-400 text-center">
              This marks a claim. CivicLens will check it with later drives.
            </p>
          </form>
        ) : (
          <div className="bg-amber-50/70 border border-amber-200 rounded-xl p-4 text-xs text-amber-800 space-y-1">
            <div className="font-semibold flex items-center space-x-1.5">
              <Clock className="w-4 h-4 text-amber-600 shrink-0" />
              <span>Claim already registered or not required</span>
            </div>
            <p className="text-[11px] text-amber-700">{message}</p>
          </div>
        )}
      </div>
    </div>
  );
}
