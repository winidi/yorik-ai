/**
 * Handoff banner for "Send via WhatsApp / Email" from the chat photo
 * lightbox. The lightbox stashes a photo URL in sessionStorage and
 * navigates to /r/whatsapp or /r/compose. The target app mounts this
 * banner; if a stashed photo is present, it shows up bottom-right with
 * "open image" + "dismiss" actions.
 *
 * Why a banner and not auto-attach: the WhatsApp bridge is currently
 * text-only (media-send needs a bridge change) and Compose can't yet
 * embed the image as a real SMTP attachment (needs backend support to
 * fetch + MIME-encode on send). Auto-inserting an <img> with a
 * Yorik-internal URL into the draft would look correct in-app but
 * silently break for external recipients who can't reach the server.
 * The honest UX is: "we received the handoff, open the image to drag
 * or paste it in manually." When the proper attach paths land, this
 * banner can grow an Attach button.
 */

import { useState } from "react";

interface SharedPhoto { url: string; name: string; from: string }

export function SharedPhotoBanner({ appLabel }: { appLabel: string }) {
  const [shared, setShared] = useState<SharedPhoto | null>(() => {
    try {
      const raw = sessionStorage.getItem("yorik_share_photo");
      return raw ? JSON.parse(raw) as SharedPhoto : null;
    } catch { return null; }
  });

  if (!shared) return null;

  function dismiss() {
    try { sessionStorage.removeItem("yorik_share_photo"); } catch {}
    setShared(null);
  }

  return (
    <div className="fixed bottom-20 right-4 z-[70] max-w-sm bg-card border border-border rounded-xl shadow-2xl p-3 flex items-start gap-3">
      <img src={shared.url} alt="" className="w-14 h-14 rounded-md object-cover shrink-0" />
      <div className="flex-1 min-w-0 text-sm">
        <div className="font-medium leading-tight">Photo from chat ready</div>
        <div className="text-xs text-muted-foreground mt-0.5 leading-relaxed">
          Auto-attach to {appLabel} isn't wired yet. Open the image and
          drag it in, or paste from clipboard if you copied first.
        </div>
        <div className="mt-2 flex gap-2">
          <a
            href={shared.url}
            target="_blank"
            rel="noopener"
            className="text-xs px-2.5 py-1 rounded-md bg-primary text-primary-foreground hover:opacity-90 transition"
          >
            Open image
          </a>
          <button
            onClick={dismiss}
            className="text-xs px-2.5 py-1 rounded-md hover:bg-muted transition"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
