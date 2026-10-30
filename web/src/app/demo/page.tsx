"use client";

import { useEffect, useState } from "react";
import {
  Settings2,
  Clock,
  RotateCcw,
  FastForward,
  Activity,
  AlertTriangle,
  KeyRound,
  CheckCircle2,
  Database,
  HardDrive,
} from "lucide-react";
import { simulateTime, resetDemo } from "../../lib/api";
import { SimulateTimeResponse } from "../../lib/types";

export default function DemoLabPage() {
  const [adminSecret, setAdminSecret] = useState("change-me");
  const [simResult, setSimResult] = useState<SimulateTimeResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [resetMessage, setResetMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [healthStatus, setHealthStatus] = useState<{
    db: string;
    storage: string;
    time: string;
    offset: number;
  }>({
    db: "ok",
    storage: "ok",
    time: new Date().toISOString(),
    offset: 0,
  });

  const fetchHealth = async () => {
    try {
      const res = await fetch(
        (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000") + "/healthz"
      );
      if (res.ok) {
        const data = await res.json();
        setHealthStatus({
          db: data.db || "ok",
          storage: data.storage || "ok",
          time: data.time || new Date().toISOString(),
          offset: data.clock_offset_seconds || 0,
        });
      }
    } catch {
      // Offline fallback
      setHealthStatus((prev) => ({
        ...prev,
        db: "offline",
        storage: "offline",
      }));
    }
  };

  useEffect(() => {
    fetchHealth();
  }, []);

  const handleSimulate = async (days: number) => {
    setLoading(true);
    setErrorMessage(null);
    setResetMessage(null);
    try {
      const seconds = days * 86400;
      const res = await simulateTime(seconds, adminSecret);
      setSimResult(res);
      await fetchHealth();
    } catch (err: unknown) {
      setErrorMessage(
        (err as { detail?: { error?: { message?: string } } })?.detail?.error
          ?.message || "Failed to advance simulation time."
      );
    } finally {
      setLoading(false);
    }
  };

  const handleReset = async () => {
    if (
      !confirm(
        "Are you sure you want to reset demo data? This resets the simulated clock offset to zero."
      )
    ) {
      return;
    }
    setLoading(true);
    setErrorMessage(null);
    setResetMessage(null);
    try {
      await resetDemo(adminSecret);
      setResetMessage("Demo environment and clock offset successfully reset to 0.");
      setSimResult(null);
      await fetchHealth();
    } catch (err: unknown) {
      setErrorMessage(
        (err as { detail?: { error?: { message?: string } } })?.detail?.error
          ?.message || "Failed to reset demo state."
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 py-10 space-y-8">
      {/* Simulation Banner per docs/SCREENS.md §3.5 */}
      <div className="bg-amber-500/10 border border-amber-300 text-amber-900 rounded-2xl p-4 flex items-center space-x-3 text-xs">
        <AlertTriangle className="w-5 h-5 text-amber-600 shrink-0" />
        <span className="font-medium">
          Demo mode. Time skipping and reset are simulations for evaluating lifecycle workflows.
        </span>
      </div>

      {/* Header */}
      <div className="border-b border-slate-200 pb-5">
        <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-amber-700 mb-1">
          <Settings2 className="w-4 h-4" />
          <span>Interactive Demo Controls</span>
        </div>
        <h1 className="text-2xl font-bold text-slate-900">
          Simulated Clock & Escalation Testbench
        </h1>
        <p className="text-xs text-slate-500 mt-1">
          Fast-forward time to test automatic authority reminders, escalation notices, and multi-pass repair verification.
        </p>
      </div>

      {/* Secret Key Configuration */}
      <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-xs flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="flex items-center space-x-3">
          <div className="w-9 h-9 rounded-lg bg-slate-100 flex items-center justify-center text-slate-600">
            <KeyRound className="w-5 h-5" />
          </div>
          <div>
            <label
              htmlFor="adminSecret"
              className="block text-xs font-bold text-slate-800"
            >
              Admin Secret Key
            </label>
            <p className="text-[11px] text-slate-400">
              Required by backend admin endpoints (kept in memory only).
            </p>
          </div>
        </div>
        <input
          id="adminSecret"
          type="password"
          value={adminSecret}
          onChange={(e) => setAdminSecret(e.target.value)}
          className="text-xs font-mono px-3 py-2 rounded-lg border border-slate-300 focus:outline-hidden focus:ring-2 focus:ring-amber-500 w-full sm:w-60"
        />
      </div>

      {/* Health & Simulated Time Panel */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-xs space-y-2">
          <div className="flex items-center justify-between text-xs text-slate-500 font-semibold uppercase tracking-wider">
            <span>Simulated Clock</span>
            <Clock className="w-4 h-4 text-amber-600" />
          </div>
          <div className="text-lg font-mono font-bold text-slate-900">
            {new Date(healthStatus.time).toLocaleString()}
          </div>
          <p className="text-[11px] text-slate-400 font-mono">
            Offset: +{(healthStatus.offset / 86400).toFixed(1)} days ({healthStatus.offset}s)
          </p>
        </div>

        <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-xs space-y-2">
          <div className="flex items-center justify-between text-xs text-slate-500 font-semibold uppercase tracking-wider">
            <span>Service Health</span>
            <Activity className="w-4 h-4 text-emerald-600" />
          </div>
          <div className="flex items-center space-x-4 text-xs font-medium pt-1">
            <span className="flex items-center space-x-1.5 text-slate-700">
              <Database className="w-3.5 h-3.5 text-slate-400" />
              <span>PostGIS:</span>
              <span className="font-semibold text-emerald-600 uppercase">
                {healthStatus.db}
              </span>
            </span>
            <span className="flex items-center space-x-1.5 text-slate-700">
              <HardDrive className="w-3.5 h-3.5 text-slate-400" />
              <span>MinIO S3:</span>
              <span className="font-semibold text-emerald-600 uppercase">
                {healthStatus.storage}
              </span>
            </span>
          </div>
          <p className="text-[11px] text-slate-400">
            All server transactions respect the simulated clock.
          </p>
        </div>
      </div>

      {/* Time-skip & Reset Action Controls */}
      <div className="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs space-y-5">
        <h2 className="text-sm font-bold uppercase tracking-wider text-slate-800 flex items-center space-x-2">
          <FastForward className="w-4 h-4 text-amber-600" />
          <span>Simulate Escalation Timers</span>
        </h2>

        <div className="flex flex-wrap gap-3">
          <button
            type="button"
            disabled={loading}
            onClick={() => handleSimulate(1)}
            className="flex-1 min-w-[120px] bg-slate-900 hover:bg-slate-800 text-white text-xs font-semibold py-2.5 px-4 rounded-xl shadow-xs transition disabled:opacity-50"
          >
            +1 Day (Reminder Window)
          </button>
          <button
            type="button"
            disabled={loading}
            onClick={() => handleSimulate(14)}
            className="flex-1 min-w-[120px] bg-amber-600 hover:bg-amber-700 text-white text-xs font-semibold py-2.5 px-4 rounded-xl shadow-xs transition disabled:opacity-50"
          >
            +14 Days (Escalation Window)
          </button>
          <button
            type="button"
            disabled={loading}
            onClick={() => handleSimulate(28)}
            className="flex-1 min-w-[120px] bg-amber-700 hover:bg-amber-800 text-white text-xs font-semibold py-2.5 px-4 rounded-xl shadow-xs transition disabled:opacity-50"
          >
            +28 Days (Full Cycle)
          </button>
        </div>

        <div className="pt-3 border-t border-slate-100 flex flex-wrap items-center justify-between gap-3">
          <button
            type="button"
            disabled={loading}
            onClick={handleReset}
            className="inline-flex items-center space-x-1.5 text-xs font-semibold text-rose-700 hover:bg-rose-50 border border-rose-200 px-3 py-2 rounded-lg transition disabled:opacity-50"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            <span>Reset Demo State & Clock</span>
          </button>

          <button
            type="button"
            disabled={loading}
            onClick={fetchHealth}
            className="text-xs font-medium text-slate-600 hover:text-slate-900 underline"
          >
            Refresh Status
          </button>
        </div>
      </div>

      {/* Feedback & Simulation Results */}
      {resetMessage && (
        <div className="p-4 bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs rounded-xl flex items-center space-x-2">
          <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
          <span>{resetMessage}</span>
        </div>
      )}

      {errorMessage && (
        <div className="p-4 bg-rose-50 border border-rose-200 text-rose-800 text-xs rounded-xl flex items-center space-x-2">
          <AlertTriangle className="w-4 h-4 text-rose-600 shrink-0" />
          <span>{errorMessage}</span>
        </div>
      )}

      {simResult && (
        <div className="bg-slate-900 text-white rounded-2xl p-6 shadow-md space-y-4 text-xs font-mono">
          <div className="flex items-center justify-between text-amber-400 font-bold">
            <span>Simulation Execution Result</span>
            <span>Reports Sent: {simResult.reports_sent}</span>
          </div>
          <div className="text-slate-300">
            New Time: {new Date(simResult.now).toLocaleString()} (+{simResult.clock_offset_seconds}s offset)
          </div>
          <div>
            <div className="text-slate-400 mb-1">Status Transitions:</div>
            {simResult.status_changes && simResult.status_changes.length > 0 ? (
              <ul className="space-y-1 list-disc list-inside text-slate-200">
                {simResult.status_changes.map((sc, i) => (
                  <li key={i}>
                    Pothole {sc.pothole_id.substring(0, 8)}: {sc.from_status} → {sc.to_status}
                  </li>
                ))}
              </ul>
            ) : (
              <span className="text-slate-500">None (all states stable).</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
