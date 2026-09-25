/**
 * PagePreview — the first page as it will be printed, scaled into
 * whatever room there is. The HTML comes from the layout code that also
 * makes the PDF; it brings its own styles and no scripts, and the frame
 * allows none.
 */
import { useLayoutEffect, useRef, useState } from "react";

const SHEET_PX = 794 + 60;      // 210 mm at 96 dpi plus the preview's grey edge

export function PagePreview({ html, note }: { html: string; note?: string }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(0.5);
  useLayoutEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    // measured at once as well: an observer reports only when the page is being painted
    const fit = () => { if (el.clientWidth) setScale(Math.min(1, el.clientWidth / SHEET_PX)); };
    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <>
      <div ref={boxRef} className="mx-auto max-w-[620px] rounded-lg overflow-hidden bg-[#d9d9de]" style={{ height: Math.round(1190 * scale) }}>
        <iframe title="Vorschau" sandbox="" srcDoc={html} tabIndex={-1}
                style={{ width: SHEET_PX, height: 1190, border: 0, transform: `scale(${scale})`, transformOrigin: "top left", pointerEvents: "none" }} />
      </div>
      <p className="mt-2 text-center text-xs text-muted-foreground">{note || "So wird die erste Seite gedruckt. Das Aussehen kommt aus deinem Briefpapier."}</p>
    </>
  );
}
