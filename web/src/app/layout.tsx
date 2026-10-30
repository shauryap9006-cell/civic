import type { Metadata } from "next";
import Link from "next/link";
import { Eye, Map, BarChart3, Settings2 } from "lucide-react";
import "./globals.css";

export const metadata: Metadata = {
  title: "CivicLens - Pothole Detection & Repair Verification",
  description:
    "Turns dashcams into a pothole reporter that also verifies road repairs using quality-gated passes.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="flex flex-col min-h-screen bg-slate-50 text-slate-900 antialiased">
        {/* Navigation Header */}
        <header className="sticky top-0 z-50 bg-white/95 backdrop-blur border-b border-slate-200">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div className="flex items-center justify-between h-16">
              {/* Brand Logo & Title */}
              <Link href="/" className="flex items-center space-x-3 group">
                <div className="w-9 h-9 rounded-lg bg-emerald-600 flex items-center justify-center text-white shadow-sm group-hover:bg-emerald-700 transition">
                  <Eye className="w-5 h-5" />
                </div>
                <div>
                  <span className="text-lg font-bold tracking-tight text-slate-900 block leading-tight">
                    CivicLens
                  </span>
                  <span className="text-xs text-slate-500 font-medium hidden sm:block">
                    Verified Pothole Repairs
                  </span>
                </div>
              </Link>

              {/* Navigation Links */}
              <nav className="flex items-center space-x-1 sm:space-x-2">
                <Link
                  href="/"
                  className="flex items-center space-x-1.5 px-3 py-2 rounded-md text-sm font-medium text-slate-700 hover:text-slate-900 hover:bg-slate-100 transition"
                >
                  <Map className="w-4 h-4 text-slate-500" />
                  <span>Map</span>
                </Link>
                <Link
                  href="/impact"
                  className="flex items-center space-x-1.5 px-3 py-2 rounded-md text-sm font-medium text-slate-700 hover:text-slate-900 hover:bg-slate-100 transition"
                >
                  <BarChart3 className="w-4 h-4 text-slate-500" />
                  <span>Impact</span>
                </Link>
                <Link
                  href="/demo"
                  className="flex items-center space-x-1.5 px-3 py-2 rounded-md text-sm font-medium text-amber-700 bg-amber-50 hover:bg-amber-100 border border-amber-200/80 transition"
                >
                  <Settings2 className="w-4 h-4 text-amber-600" />
                  <span className="hidden xs:inline">Demo Lab</span>
                </Link>
              </nav>
            </div>
          </div>
        </header>

        {/* Main Content Area */}
        <main className="flex-1 flex flex-col">{children}</main>

        {/* Footer with Honest Reporting Statements */}
        <footer className="bg-white border-t border-slate-200 py-6 text-xs text-slate-500">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex flex-col sm:flex-row items-center justify-between gap-4">
            <p>
              CivicLens MVP · Dashcam-verified road repair infrastructure.
            </p>
            <p className="text-slate-400 text-center sm:text-right">
              Severity is a graded estimate, not a measurement.
            </p>
          </div>
        </footer>
      </body>
    </html>
  );
}
