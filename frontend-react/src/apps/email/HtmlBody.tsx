/**
 * Sandboxed HTML email renderer.
 *
 * Why an iframe with srcdoc: real-world HTML emails carry inline
 * styles, MSO conditionals, table-based layouts, and sometimes
 * <script>/<form>. Rendering them directly into our DOM would
 * inherit Tailwind styles (breaking the original design) and could
 * execute scripts.
 *
 * `sandbox=""` (empty) is the maximum sandbox — disables JS, forms,
 * popups, top-nav, same-origin. Images STILL load (they don't need
 * scripts). External resources are allowed but isolated; we'd block
 * tracking pixels via a future remote-content toggle.
 */

import { useEffect, useRef, useState } from "react";

interface Props {
  html: string;
  className?: string;
  /** When true, ignore content-derived height and fill the parent
   *  container instead. Long emails scroll inside the iframe.
   *  Used by the Reader so short emails don't leave blank space
   *  between the body and the AI drafts panel. */
  fill?: boolean;
  /** Message id, required when allowImages is true — the proxy
   *  endpoint scopes requests by message ownership. */
  messageId?: number;
  /** When false (default), remote <img> tags have their src stripped
   *  so trackers and remote content can't load — matches Gmail / Apple
   *  Mail default. Set true after the user clicks "Show images" on
   *  the message. CID-referenced images (inline attachments) are
   *  rewritten to their attachment-inline URL regardless of this
   *  flag — those came from the email itself, not from a remote
   *  host, and don't leak anything. */
  allowImages?: boolean;
  /** Map of content-id (without angle brackets) → attachment id, so
   *  cid:foo@bar refs can be resolved to /api/email/attachments/{id}/inline.
   *  Missing/empty leaves cid: refs broken (renderer will show alt). */
  cidMap?: Record<string, number>;
}

const BASE_STYLE = `
  <style>
    html, body {
      margin: 0; padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
      font-size: 14px; line-height: 1.55; color: inherit;
      word-wrap: break-word;
    }
    img { max-width: 100%; height: auto; }
    table { max-width: 100%; }
    a { color: #6366f1; }
    blockquote { border-left: 3px solid #6366f1; margin-left: 0; padding-left: 12px; opacity: 0.8; }
    pre { background: #f0f0f0; padding: 8px; border-radius: 4px; overflow-x: auto; }
  </style>
`;

/** Rewrite image sources in the email body so they comply with the
 *  parent page's CSP (img-src 'self' …) AND respect the remote-image
 *  block toggle. Handles:
 *   - <img src>
 *   - <img srcset>           ← bonprix etc. use this for retina; HiDPI
 *                              browsers prefer srcset over src, so missing
 *                              this single attribute means "Show images"
 *                              appears to do nothing.
 *   - <source src> / <source srcset> (inside <picture>)
 *
 *  URL handling rules per src:
 *   - cid:foo@bar  → /api/email/attachments/<id>/inline (via cidMap)
 *   - data:/blob:/app-paths → passthrough
 *   - http(s)://   → proxied via /api/email/messages/<id>/proxy-image
 *                    when allowImages, blocked placeholder otherwise
 *   - anything else → blocked placeholder (defensive)
 */
function rewriteHtmlImages(
  raw: string,
  opts: { allowImages: boolean; messageId?: number; cidMap?: Record<string, number> },
): { head: string; body: string } {
  // 1×1 transparent gif — keeps layout sensible when src is stripped.
  const PLACEHOLDER =
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

  function rewriteOne(url: string): string {
    if (!url) return PLACEHOLDER;
    const lower = url.toLowerCase();
    if (lower.startsWith("cid:")) {
      const cid = url.slice(4).trim().replace(/^<|>$/g, "");
      const attId = opts.cidMap?.[cid];
      return attId != null
        ? `/api/email/attachments/${attId}/inline`
        : PLACEHOLDER;
    }
    if (lower.startsWith("data:") || lower.startsWith("blob:") || lower.startsWith("/")) {
      return url;
    }
    if (lower.startsWith("http://") || lower.startsWith("https://")) {
      if (opts.allowImages && opts.messageId != null) {
        return `/api/email/messages/${opts.messageId}/proxy-image?url=${encodeURIComponent(url)}`;
      }
      return PLACEHOLDER;
    }
    return PLACEHOLDER;
  }

  // srcset format: "url1 [descriptor], url2 [descriptor], …"
  // descriptors are "Nx" or "Nw"; we don't touch them, only the URLs.
  // Real-world emails never put commas inside the URL part (would
  // require escaping that templating engines don't emit) so a naive
  // split on /,\s+/ is safe enough — and we trim defensively.
  function rewriteSrcset(value: string): string {
    return value.split(/,\s+/).map((entry) => {
      const trimmed = entry.trim();
      if (!trimmed) return "";
      // Split at first whitespace — URL is everything before it, descriptor is what follows.
      const m = trimmed.match(/^(\S+)(\s+(.+))?$/);
      if (!m) return trimmed;
      const newUrl = rewriteOne(m[1]);
      return m[3] ? `${newUrl} ${m[3]}` : newUrl;
    }).filter(Boolean).join(", ");
  }

  // Parse with DOMParser so we don't write a regex-based HTML mangler.
  // 'text/html' is forgiving — works for the malformed soup that real
  // email clients emit (MS Word, mailchimp templates, etc.).
  const doc = new DOMParser().parseFromString(raw, "text/html");

  // <img>: rewrite src AND srcset (latter wins on HiDPI in modern browsers).
  doc.querySelectorAll("img").forEach((img) => {
    const src = img.getAttribute("src");
    if (src) img.setAttribute("src", rewriteOne(src));
    const srcset = img.getAttribute("srcset");
    if (srcset) img.setAttribute("srcset", rewriteSrcset(srcset));
  });

  // <source> children of <picture> — same treatment.
  doc.querySelectorAll("source").forEach((s) => {
    const src = s.getAttribute("src");
    if (src) s.setAttribute("src", rewriteOne(src));
    const srcset = s.getAttribute("srcset");
    if (srcset) s.setAttribute("srcset", rewriteSrcset(srcset));
  });

  // Preserve the email's own <head> content (retailers put <style>
  // blocks there for layout). Earlier this function returned only
  // doc.body.innerHTML, which silently broke responsive emails
  // (bonprix newsletters in particular) because the desktop-vs-mobile
  // CSS lived in <style> blocks the rewriter was throwing away.
  return { head: doc.head.innerHTML, body: doc.body.innerHTML };
}


export function HtmlBody({
  html, className = "", fill = false,
  messageId, allowImages = false, cidMap,
}: Props) {
  const ref = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(fill ? 0 : 400);

  useEffect(() => {
    if (fill) return; // fixed-fill height, no measurement needed
    // Wait for the iframe's load event so srcdoc has finished parsing,
    // then measure the rendered content's scrollHeight. srcdoc is
    // same-origin so we CAN read the content's body height (unlike
    // cross-origin iframes). The sandbox attribute is still applied.
    const iframe = ref.current;
    if (!iframe) return;
    const handler = () => {
      try {
        const doc = iframe.contentDocument;
        if (doc) {
          const h = Math.min(
            doc.documentElement.scrollHeight,
            doc.body?.scrollHeight ?? 800,
            2000  // cap — runaway docs scroll inside the iframe
          );
          setHeight(h + 20);
        }
      } catch {}
    };
    iframe.addEventListener("load", handler);
    return () => iframe.removeEventListener("load", handler);
  }, [html, fill]);

  const { head: emailHead, body: emailBody } = rewriteHtmlImages(html, {
    allowImages, messageId, cidMap,
  });
  // Order matters: BASE_STYLE first so our defaults are at the bottom of
  // the cascade and the email's own <style> rules win where they're
  // specified (otherwise generic <a> color etc. would override the
  // retailer's brand colors).
  const fullSrc = `<!doctype html><html><head>${BASE_STYLE}<base target="_blank">${emailHead}</head><body>${emailBody}</body></html>`;

  return (
    <iframe
      ref={ref}
      // allow-same-origin: required so the proxied image fetches inside
      //   the iframe send our SameSite=Lax session cookie. Safe BECAUSE
      //   we don't grant allow-scripts — no JS can run, so same-origin
      //   status only enables passive same-origin loads (img, link href).
      // allow-popups: link-target=_blank works (the <base target="_blank">
      //   default we set above).
      // NOT allowed: scripts, forms, top-navigation, modals, downloads.
      sandbox="allow-popups allow-same-origin"
      title="email body"
      srcDoc={fullSrc}
      className={`w-full border-0 ${className}`}
      style={fill ? { height: "100%" } : { height: `${height}px` }}
    />
  );
}
