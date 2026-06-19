/**
 * Listens globally for `ui_action.type === 'navigate'` events and calls
 * React Router's navigate(path) so the user lands wherever the agent
 * routed them.
 *
 * Mounted once near the top of the tree (under <BrowserRouter>) so it
 * works from any page — voice, chat, or skill mutations that emit a
 * navigation as a side effect.
 *
 * Also drains queued navigate actions on mount in case the dispatch
 * fired before this component existed (typical voice flow: VoiceFab
 * dispatches → route changes → bridge mounts → would otherwise miss
 * the event).
 */

import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { drainUiActions } from "@/lib/uiActions";

export function NavigationBridge() {
  const navigate = useNavigate();

  useEffect(() => {
    // The router's basename is "/r", so we strip that prefix from
    // skill-emitted paths like "/r/contacts" before handing to navigate.
    function handle(detail: any) {
      if (!detail || detail.type !== "navigate") return;
      const raw = String(detail.path || "");
      if (!raw) return;
      const path = raw.startsWith("/r") ? raw.slice(2) || "/" : raw;
      navigate(path);
    }
    function onEvt(e: Event) { handle((e as CustomEvent).detail); }
    window.addEventListener("yorik-ui-action", onEvt);
    // Drain anything queued before mount (race with route change).
    for (const a of drainUiActions(["navigate"])) handle(a);
    return () => window.removeEventListener("yorik-ui-action", onEvt);
  }, [navigate]);

  return null;
}
