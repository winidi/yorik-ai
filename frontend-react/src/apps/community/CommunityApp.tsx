// CommunityApp — generic React-shell host for any community-installed app.
//
// Loads the app's entry_ui.js inside a sandboxed iframe that gets:
//   - the modern Yorik Dock + chrome around it
//   - a `window.yorik` global wired through postMessage:
//       yorik.callOperation(name, params) -> Promise<result>
//       yorik.openChat({ prefill })       -> Promise<{ ok }>
//       yorik.notify(message, kind)       -> Promise<{ ok }>
//   - theme CSS variables (--yorik-bg, --yorik-fg, --yorik-accent, etc.)
//     read from the host's computed style at mount time
//
// Sandbox model (tightened): `sandbox="allow-scripts"` ONLY — the iframe
// gets a unique opaque origin. Cookies do NOT forward. The app cannot
// fetch /api/* directly (no credentials + CSP connect-src 'none').
// Everything the app does must go through window.yorik.* postMessage,
// which the parent gates by app-id namespace. The app's entry_ui.js is
// fetched by the parent (authenticated) and inlined into the srcdoc so
// no cross-origin script load is needed.

import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Dock } from "@/components/Dock";
import { api } from "@/lib/api";
import { Loader2, AlertTriangle, RefreshCw } from "lucide-react";

interface AppManifest {
  id: string;
  name: string;
  icon?: string;
  description?: string;
  chrome?: "embedded" | "fullscreen";
}

// Build a CSS block of --yorik-* variables from the parent's computed
// style. Runs once at mount; theme toggles after mount require a reload
// of the iframe to pick up (acceptable for v1; postMessage refresh hook
// is a future tightening).
function readThemeTokens(): Record<string, string> {
  const cs = window.getComputedStyle(document.documentElement);
  const pull = (prop: string) => cs.getPropertyValue(prop).trim();
  return {
    "--yorik-bg":         `hsl(${pull("--background")  || "0 0% 100%"})`,
    "--yorik-fg":         `hsl(${pull("--foreground")  || "0 0% 0%"})`,
    "--yorik-fg-muted":   `hsl(${pull("--muted-foreground") || "240 4% 46%"})`,
    "--yorik-card":       `hsl(${pull("--card")        || "0 0% 100%"})`,
    "--yorik-border":     `hsl(${pull("--border")      || "240 6% 90%"})`,
    "--yorik-accent":     `hsl(${pull("--primary")     || "240 100% 50%"})`,
    "--yorik-accent-fg":  `hsl(${pull("--primary-foreground") || "0 0% 100%"})`,
    "--yorik-radius":     pull("--radius") || "0.5rem",
  };
}

// HTML-escape a JS string so it's safe to embed inside <script>...</script>
// inside the srcdoc. The only sequence that can break out is </script;
// neutralize by inserting a backslash. (HTML parser stops at first
// case-insensitive </script>; the bypass is well-known.)
function escapeForInlineScript(js: string): string {
  return js.replace(/<\/(script)/gi, "<\\/$1");
}

export function CommunityApp() {
  const { appId } = useParams<{ appId: string }>();
  const navigate = useNavigate();
  const [manifest, setManifest] = useState<AppManifest | null>(null);
  const [uiJs, setUiJs] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped by the Retry button to force a re-fetch.
  const [loadNonce, setLoadNonce] = useState(0);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);

  // Fetch manifest + UI JS in parallel. The parent is in Yorik's real
  // origin, so cookies forward and /api/apps/{id}/ui returns the JS body.
  useEffect(() => {
    if (!appId) return;
    let cancelled = false;
    setError(null);
    setManifest(null);
    setUiJs(null);
    (async () => {
      try {
        const [m, jsResp] = await Promise.all([
          api.get<AppManifest>(`/api/apps/${appId}/manifest`),
          fetch(`/api/apps/${appId}/ui`, { credentials: "same-origin" }),
        ]);
        if (!jsResp.ok) throw new Error(`could not load app code (HTTP ${jsResp.status})`);
        const jsText = await jsResp.text();
        if (!jsText || jsText.length < 10) throw new Error("app code is empty");
        if (cancelled) return;
        setManifest(m);
        setUiJs(jsText);
      } catch (e: any) {
        if (!cancelled) setError(e?.message || "Could not load app");
      }
    })();
    return () => { cancelled = true; };
  }, [appId, loadNonce]);

  // postMessage bridge: parent side. Listens for messages from the
  // app's iframe, dispatches to API / navigation / notifications,
  // posts the response back keyed by the call id.
  //
  // Even with an opaque-origin iframe, postMessage works — the messages
  // are tagged with __yorik so we ignore unrelated chatter, and we check
  // e.source against our own iframe so other windows can't inject calls.
  useEffect(() => {
    if (!appId) return;

    const handler = async (e: MessageEvent) => {
      if (e.source !== iframeRef.current?.contentWindow) return;
      const m: any = e.data;
      if (!m || m.__yorik !== true || typeof m.id !== "number") return;

      const reply = (payload: any) => {
        iframeRef.current?.contentWindow?.postMessage(
          { __yorik: true, id: m.id, ...payload },
          "*",
        );
      };

      try {
        if (m.method === "callOperation") {
          const name = String(m.params?.name || "");
          if (!name.startsWith(`${appId}.`)) {
            throw new Error(`app '${appId}' cannot call '${name}' (must be in its own namespace)`);
          }
          const opName = name.slice(appId.length + 1);
          const result = await api.post(
            `/api/apps/${appId}/op/${opName}`,
            { params: m.params?.params || {} },
          );
          reply({ ok: true, result });
        } else if (m.method === "openChat") {
          const prefill = String(m.params?.prefill || "");
          navigate("/chat", { state: { prefill } });
          reply({ ok: true, result: { ok: true } });
        } else if (m.method === "notify") {
          // v1: console only. A proper toast hook is a follow-up.
          console.info(`[${appId}] ${m.params?.message || ""}`);
          reply({ ok: true, result: { ok: true } });
        } else {
          throw new Error(`unknown method: ${m.method}`);
        }
      } catch (err: any) {
        reply({ ok: false, error: err?.message || String(err) });
      }
    };

    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [appId, navigate]);

  if (!appId) {
    navigate("/home", { replace: true });
    return null;
  }

  // Build srcdoc only once we have BOTH manifest and JS. Theme tokens +
  // CSP + bridge bootstrap + inlined app code. CSP is the belt; the
  // sandbox attribute is the braces.
  let srcdoc = "";
  if (uiJs) {
    const tokens = readThemeTokens();
    const tokenCss = Object.entries(tokens)
      .map(([k, v]) => `${k}: ${v};`)
      .join("\n  ");
    const safeAppJs = escapeForInlineScript(uiJs);

    // CSP rationale:
    //   default-src 'none'    — deny-by-default for every fetch type
    //   script-src 'unsafe-inline' — bridge + app code, both inlined
    //   style-src  'unsafe-inline' — apps inject their own <style>
    //   img-src    data: blob: — local images only; no external URLs
    //   connect-src 'none'    — no fetch/XHR/WebSocket of any kind
    //   frame-src  'none'     — no nested iframes
    //   base-uri   'none'     — no <base> tag re-routing
    //   form-action 'none'    — no form submissions to anywhere
    const csp = [
      "default-src 'none'",
      "script-src 'unsafe-inline'",
      "style-src 'unsafe-inline'",
      "img-src data: blob:",
      "connect-src 'none'",
      "frame-src 'none'",
      "base-uri 'none'",
      "form-action 'none'",
    ].join("; ");

    srcdoc = `<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>
  :root {
    ${tokenCss}
    color-scheme: light dark;
  }
  html, body { margin: 0; padding: 0; min-height: 100%; }
  body {
    font: 14px/1.5 system-ui, -apple-system, sans-serif;
    background: var(--yorik-bg);
    color: var(--yorik-fg);
  }
  *, *::before, *::after { box-sizing: border-box; }
</style>
</head><body>
<div id="app"></div>
<script>
(function(){
  var pending = new Map();
  var nextId = 1;
  window.addEventListener("message", function(e) {
    var m = e.data;
    if (!m || m.__yorik !== true || typeof m.id !== "number") return;
    var p = pending.get(m.id);
    if (!p) return;
    pending.delete(m.id);
    if (m.ok) p.resolve(m.result); else p.reject(new Error(m.error || "rejected"));
  });
  function send(method, params) {
    var id = nextId++;
    window.parent.postMessage({ __yorik: true, id: id, method: method, params: params }, "*");
    return new Promise(function(resolve, reject) {
      pending.set(id, { resolve: resolve, reject: reject });
      setTimeout(function() {
        if (pending.has(id)) {
          pending.delete(id);
          reject(new Error("yorik bridge timeout"));
        }
      }, 30000);
    });
  }
  window.yorik = {
    callOperation: function(name, params) { return send("callOperation", { name: name, params: params || {} }); },
    openChat:      function(opts)         { return send("openChat", opts || {}); },
    notify:        function(message, kind){ return send("notify", { message: message, kind: kind || "info" }); },
  };
})();
</script>
<script>
${safeAppJs}
</script>
</body></html>`;
  }

  return (
    <div className="flex flex-col h-screen bg-background text-foreground">
      <header className="h-12 px-4 border-b border-border flex items-center gap-3 bg-background/85 backdrop-blur shrink-0">
        {manifest?.icon && <span className="text-lg">{manifest.icon}</span>}
        <div className="font-semibold text-sm">
          {manifest?.name || appId}
        </div>
        {manifest?.description && (
          <div className="text-xs text-muted-foreground truncate hidden sm:block">
            {manifest.description}
          </div>
        )}
      </header>

      <section className="flex-1 overflow-hidden pb-16 relative">
        {error ? (
          <div className="h-full flex items-center justify-center p-6">
            <div className="max-w-md text-center space-y-3">
              <div className="inline-flex items-center justify-center w-10 h-10 rounded-full bg-destructive/10 text-destructive">
                <AlertTriangle className="w-5 h-5" />
              </div>
              <div className="text-base font-semibold">
                {appId} couldn't load
              </div>
              <div className="text-sm text-muted-foreground leading-relaxed">
                {error}
              </div>
              <div className="text-xs text-muted-foreground">
                The app's source may be missing, its server-side install may have
                failed, or your network blocked the request.
              </div>
              <div className="flex justify-center gap-2 pt-2">
                <button
                  onClick={() => setLoadNonce((n) => n + 1)}
                  className="px-3 py-1.5 rounded-md text-sm font-medium bg-primary text-primary-foreground hover:opacity-90 transition inline-flex items-center gap-1.5"
                >
                  <RefreshCw className="w-3.5 h-3.5" />
                  Try again
                </button>
                <button
                  onClick={() => navigate("/home")}
                  className="px-3 py-1.5 rounded-md text-sm font-medium border border-border hover:bg-muted text-muted-foreground hover:text-foreground transition"
                >
                  Back to home
                </button>
              </div>
            </div>
          </div>
        ) : !manifest || !uiJs ? (
          <div className="flex items-center justify-center h-full text-muted-foreground">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
        ) : (
          <iframe
            ref={iframeRef}
            title={manifest.name}
            srcDoc={srcdoc}
            sandbox="allow-scripts"
            className="w-full h-full border-0 bg-background"
          />
        )}
      </section>

      <Dock activeAppId={appId} />
    </div>
  );
}
