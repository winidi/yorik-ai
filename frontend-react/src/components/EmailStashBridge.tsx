/**
 * Listens globally for `ui_action.type === 'stash_pending_email'` and
 * writes it into the same `yorik_pending_email` sessionStorage key the
 * AttachmentStashTray's "Send via email" button already uses — the
 * Email app's Composer picks it up next time it mounts, pre-filled
 * with recipient, subject, body and the attachment already attached.
 *
 * Deliberately does NOT navigate: a skill (prepare_email) stages the
 * draft so it's waiting whenever the user opens the Email app
 * themselves. It never sends anything — sending is always a manual
 * click in the Composer, same as every other email in Yorik.
 *
 * Mirrors NavigationBridge's mount-drain + live-listen pattern exactly.
 */

import { useEffect } from "react";
import { drainUiActions } from "@/lib/uiActions";

interface StashPendingEmailAction {
  type: "stash_pending_email";
  to?: string;
  subject?: string;
  body?: string;
  account_id?: number;
  attachments?: Array<{ url: string; filename: string; mimetype?: string }>;
}

export function EmailStashBridge() {
  useEffect(() => {
    function handle(detail: any) {
      if (!detail || detail.type !== "stash_pending_email") return;
      const a = detail as StashPendingEmailAction;
      try {
        sessionStorage.setItem("yorik_pending_email", JSON.stringify({
          to: a.to || "",
          subject: a.subject || "",
          body: a.body || "",
          accountId: typeof a.account_id === "number" ? a.account_id : undefined,
          attachments: a.attachments || [],
        }));
      } catch {}
    }
    function onEvt(e: Event) { handle((e as CustomEvent).detail); }
    window.addEventListener("yorik-ui-action", onEvt);
    // Drain anything queued before mount (same race NavigationBridge guards against).
    for (const a of drainUiActions(["stash_pending_email"])) handle(a);
    return () => window.removeEventListener("yorik-ui-action", onEvt);
  }, []);

  return null;
}
