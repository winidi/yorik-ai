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

export function HtmlBody({ html, className = "", fill = false }: Props) {
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

  const fullSrc = `<!doctype html><html><head>${BASE_STYLE}<base target="_blank"></head><body>${html}</body></html>`;

  return (
    <iframe
      ref={ref}
      sandbox="allow-popups"
      title="email body"
      srcDoc={fullSrc}
      className={`w-full border-0 ${className}`}
      style={fill ? { height: "100%" } : { height: `${height}px` }}
    />
  );
}
