/**
 * Yorik Photos — React-shell wrapper around the Immich iframe.
 *
 * The actual photo browser is Immich (separate container on port 2283
 * or :8443 behind Tailscale). We DON'T re-implement the photos UI —
 * Immich does timeline / faces / places / albums far better than we
 * ever would. This file just keeps the user inside Yorik's chrome
 * (same dock, same auth context) instead of bouncing them out to the
 * vanilla shell when they click the Photos tile.
 *
 * URL derivation matches the vanilla _externalAppUrl(): same hostname,
 * different port. The Immich container's cookies are scoped to its own
 * origin, so the iframe handles its own session — no cross-origin
 * cookie surgery needed.
 */

import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Camera, ExternalLink, AlertCircle, Server } from "lucide-react";
import { Dock } from "@/components/Dock";
import { api } from "@/lib/api";

function defaultImmichUrl(): string {
  if (typeof window === "undefined") return "http://localhost:2283/";
  const proto = window.location.protocol;
  const host = window.location.hostname;
  // HTTP localhost → :2283, HTTPS (Tailscale) → :8443. For HTTPS on
  // a real domain (e.g. wir.winiecki.ai) the backend supplies
  // immich_public_url so the iframe gets a properly-served subdomain
  // with the same Let's Encrypt cert; this fallback only matters when
  // the operator hasn't set YORIK_IMMICH_PUBLIC_URL yet.
  return proto === "https:"
    ? `https://${host}:8443/`
    : `http://${host}:2283/`;
}

export function PhotosApp() {
  const [params] = useSearchParams();
  const asset = params.get("asset");
  // Backend-supplied URL wins; fall back to the host-derived guess only
  // until /api/health responds.
  const [serverImmichUrl, setServerImmichUrl] = useState<string | null>(null);
  const src = useMemo(() => {
    const base = (serverImmichUrl || defaultImmichUrl()).replace(/\/$/, "") + "/";
    // Deep-link: `/r/photos?asset=<id>` → iframe Immich at `/photos/<id>`.
    // Match Immich's own URL pattern (see backend/connectors/immich.py
    // _photo_dict's view_url). Validate the id so we don't smuggle a
    // path-traversal into the iframe URL.
    if (asset && /^[A-Za-z0-9._-]+$/.test(asset)) {
      return `${base.replace(/\/$/, "")}/photos/${encodeURIComponent(asset)}`;
    }
    return base;
  }, [asset, serverImmichUrl]);

  // Probe Immich reachability via Yorik's own /api/health (server-side
  // check, cached 30s). Without this, an Immich-down user sees a
  // browser-default blank/error iframe with no actionable message —
  // the worst empty state in the app per the alpha audit.
  const [immichDown, setImmichDown] = useState<null | boolean>(null);
  useEffect(() => {
    let cancelled = false;
    api.get<{ immich_reachable?: boolean; immich_public_url?: string }>("/api/health")
      .then(h => {
        if (cancelled) return;
        setImmichDown(h.immich_reachable === false);
        if (h.immich_public_url) setServerImmichUrl(h.immich_public_url);
      })
      .catch(() => { if (!cancelled) setImmichDown(true); });
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="h-screen flex flex-col bg-background text-foreground pb-16">
      <header className="h-12 px-4 border-b border-border bg-background/85 backdrop-blur flex items-center gap-3 shrink-0">
        <div className="w-7 h-7 rounded-md bg-emerald-500/15 flex items-center justify-center">
          <Camera className="w-3.5 h-3.5 text-emerald-500" />
        </div>
        <span className="text-sm font-medium">Photos</span>
        <span className="text-[10px] text-muted-foreground hidden sm:inline">
          via Immich
        </span>
        <div className="ml-auto">
          <a
            href={src}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[11px] inline-flex items-center gap-1 px-2 py-1 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition"
            title="Open Immich in a new tab"
          >
            <ExternalLink className="w-3 h-3" /> New tab
          </a>
        </div>
      </header>

      <div className="flex-1 min-h-0 relative">
        {immichDown === true ? (
          <div className="absolute inset-0 flex items-center justify-center p-8 text-center bg-background">
            <div className="max-w-md">
              <div className="w-14 h-14 mx-auto mb-3 rounded-full bg-amber-500/10 flex items-center justify-center">
                <Server className="w-6 h-6 text-amber-500" />
              </div>
              <div className="text-base font-semibold mb-1">Immich is not running</div>
              <div className="text-sm text-muted-foreground mb-4">
                The Photos app needs the Immich container to be up. Yorik's backend can't reach it at <code className="text-xs">{serverImmichUrl || defaultImmichUrl()}</code>.
              </div>
              <div className="text-xs text-left bg-muted/40 border border-border rounded-md p-3 font-mono leading-relaxed">
                # in the yorik-ai directory{"\n"}
                docker compose up -d immich-server immich-machine-learning immich-postgres immich-redis
              </div>
              <div className="text-[11px] text-muted-foreground mt-3">
                Already running on a different host? Update <code>HOMEOS_IMMICH_BASE_URL</code> in <code>config.env</code>.
              </div>
            </div>
          </div>
        ) : (
          <>
            <iframe
              src={src}
              title="Immich"
              // No sandbox — Immich needs first-party cookies, popups, downloads.
              // It's same-host different-port so the browser already isolates it.
              className="absolute inset-0 w-full h-full border-0 bg-white"
              allow="clipboard-read; clipboard-write; fullscreen; geolocation"
            />
            {/* Swipe rails: Immich runs in a cross-origin iframe, so its
                touch events can't reach the parent's SwipeNav. These
                transparent strips sit on top of the iframe along the
                left, right, and bottom edges, capturing touch start so
                SwipeNav at the window level picks them up.
                Only rendered on coarse-pointer devices (touchscreens) —
                desktop users navigate via the Dock and have no swipe
                gesture to begin with, and the rails were covering
                Immich's top-right profile + notifications buttons.
                Slimmed to 8 (32px) on touch: enough to catch an edge
                swipe, narrow enough to leave Immich's chrome clickable. */}
            <div
              aria-hidden
              className="hidden pointer-coarse:block absolute top-0 bottom-0 left-6 w-8 z-10"
              style={{ touchAction: "none" }}
            />
            <div
              aria-hidden
              className="hidden pointer-coarse:block absolute top-0 bottom-0 right-6 w-8 z-10"
              style={{ touchAction: "none" }}
            />
            <div
              aria-hidden
              className="hidden pointer-coarse:block absolute left-0 right-0 bottom-0 h-8 z-10"
              style={{ touchAction: "none" }}
            />
          </>
        )}
        <noscript>
          <div className="absolute inset-0 flex items-center justify-center p-8 text-center bg-background">
            <div>
              <AlertCircle className="w-10 h-10 mx-auto mb-3 text-muted-foreground" />
              <div className="font-medium">JavaScript is required to load Immich.</div>
            </div>
          </div>
        </noscript>
      </div>

      <Dock activeAppId="photos" />
    </div>
  );
}
