"use client";

import React, { useEffect, useRef } from "react";
import type { Map as MapLibreMap, Marker, Popup } from "maplibre-gl";
import { PotholeSummary } from "../lib/types";
import { STATUS_TOKENS, SEVERITY_CONFIG, SEVERITY_TOOLTIP } from "../lib/tokens";

interface MapLibreViewProps {
  potholes: PotholeSummary[];
  activePothole: PotholeSummary | null;
  onSelectPothole: (pothole: PotholeSummary) => void;
}

export const MapLibreView: React.FC<MapLibreViewProps> = ({
  potholes,
  activePothole,
  onSelectPothole,
}) => {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const markersRef = useRef<Marker[]>([]);
  const popupsRef = useRef<Popup[]>([]);

  useEffect(() => {
    if (!mapContainerRef.current) return;
    let isCancelled = false;

    // Dynamically import MapLibre GL to avoid SSR issues
    import("maplibre-gl").then((maplibregl) => {
      if (isCancelled || !mapContainerRef.current) return;

      const defaultCenter: [number, number] = [76.7794, 30.7333]; // Default Chandigarh Metro

      const map = new maplibregl.Map({
        container: mapContainerRef.current,
        style: {
          version: 8,
          sources: {
            osm: {
              type: "raster",
              tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
              tileSize: 256,
              attribution:
                '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors',
            },
          },
          layers: [
            {
              id: "osm-layer",
              type: "raster",
              source: "osm",
              minzoom: 0,
              maxzoom: 19,
            },
          ],
        },
        center: defaultCenter,
        zoom: 13,
      });

      map.addControl(
        new maplibregl.NavigationControl({ showCompass: true, showZoom: true }),
        "top-right"
      );

      mapRef.current = map;
    });

    return () => {
      isCancelled = true;
      if (mapRef.current) {
        mapRef.current.remove();
        mapRef.current = null;
      }
    };
  }, []);

  // Update markers whenever potholes or map changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    // Clear existing markers & popups
    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];
    popupsRef.current.forEach((p) => p.remove());
    popupsRef.current = [];

    import("maplibre-gl").then((maplibregl) => {
      potholes.forEach((p) => {
        const token = STATUS_TOKENS[p.status] || STATUS_TOKENS.open;
        const sev = SEVERITY_CONFIG[p.severity] || SEVERITY_CONFIG.low;

        // Custom DOM Marker element
        const el = document.createElement("div");
        el.className =
          "group cursor-pointer transform -translate-x-1/2 -translate-y-full hover:scale-110 transition-transform duration-150";

        // Marker Pin HTML
        el.innerHTML = `
          <div style="background-color: ${token.colorHex};" class="w-8 h-8 rounded-full shadow-md flex items-center justify-center text-white border-2 border-white ring-1 ring-black/10">
            <svg class="w-4 h-4 fill-current" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="8" fill="white" fill-opacity="0.25"/>
              <circle cx="12" cy="12" r="4" fill="white"/>
            </svg>
          </div>
          <div style="border-top-color: ${token.colorHex};" class="w-0 h-0 border-l-[6px] border-l-transparent border-r-[6px] border-r-transparent border-t-[8px] mx-auto -mt-0.5"></div>
        `;

        // Popup HTML with honest reporting details
        const sourceBadgeHtml =
          p.source_badge !== "live"
            ? `<span class="inline-block text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-purple-100 text-purple-800 border border-purple-200 ml-1.5">${p.source_badge}</span>`
            : "";

        const popupContent = `
          <div class="p-3 text-slate-900 font-sans max-w-xs text-left">
            <div class="flex items-center justify-between gap-2 mb-2">
              <span style="color: ${token.colorHex};" class="text-xs font-bold uppercase tracking-wide flex items-center gap-1">
                <span class="w-2 h-2 rounded-full" style="background-color: ${token.colorHex};"></span>
                ${token.label}
              </span>
              ${sourceBadgeHtml}
            </div>
            <div class="text-[11px] text-slate-500 mb-2">
              <span class="font-medium text-slate-700">${sev.label}</span>
              <span class="block text-[10px] text-slate-400 mt-0.5" title="${SEVERITY_TOOLTIP}">
                (${p.event_count} sighting${p.event_count > 1 ? "s" : ""})
              </span>
            </div>
            <div class="text-[10px] text-slate-500 mb-3 font-mono">
              ${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}
            </div>
            <a href="/pothole/${p.id}" class="block text-center bg-slate-900 hover:bg-slate-800 text-white text-xs font-semibold py-1.5 px-3 rounded-md transition">
              View Evidence & Passes
            </a>
          </div>
        `;

        const popup = new maplibregl.Popup({
          offset: 25,
          closeButton: false,
          className: "civiclens-popup",
        }).setHTML(popupContent);

        const marker = new maplibregl.Marker({ element: el })
          .setLngLat([p.lon, p.lat])
          .setPopup(popup)
          .addTo(map);

        el.addEventListener("click", () => {
          onSelectPothole(p);
        });

        markersRef.current.push(marker);
        popupsRef.current.push(popup);
      });
    });
  }, [potholes, onSelectPothole]);

  // Center on active pothole
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !activePothole) return;

    map.flyTo({
      center: [activePothole.lon, activePothole.lat],
      zoom: 15,
      essential: true,
      speed: 1.2,
    });
  }, [activePothole]);

  return (
    <div className="relative w-full h-full min-h-[400px]">
      <div ref={mapContainerRef} className="w-full h-full" />
    </div>
  );
};
