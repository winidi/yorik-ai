import type { Attention, PipelineState } from "./types";

const ATTENTION: Record<Attention, string> = {
  vielleicht: "Ist das die Antwort?",
  kann_nicht_pruefen: "Yorik kann gerade nicht sicher prüfen",
  schritt_faellig: "Keine Antwort — Erinnerung senden?",
  uebergabe: "Keine Antwort nach allen Erinnerungen",
  selbst_geantwortet: "Du hast selbst geschrieben",
  versand_unklar: "Unklar, ob die Erinnerung rausging",
  person_aus: "Pipelines sind für dich ausgeschaltet",
};

export function attentionLabel(a: Attention | null): string {
  return a ? ATTENTION[a] || a : "";
}

export function stateLabel(s: PipelineState): string {
  return {
    entwurf: "Entwurf — noch nicht gestartet",
    laeuft: "Läuft",
    pausiert: "Pausiert",
    erledigt: "Erledigt",
    abgebrochen: "Abgebrochen",
  }[s] || s;
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** "heute", "morgen", "in 3 Tagen", "vor 2 Tagen", else a date. */
export function relDay(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const days = Math.round((startOfDay(d) - startOfDay(new Date())) / 86400000);
  if (days === 0) return "heute";
  if (days === 1) return "morgen";
  if (days === -1) return "gestern";
  if (days > 1 && days < 14) return `in ${days} Tagen`;
  if (days < -1 && days > -14) return `vor ${-days} Tagen`;
  return "am " + d.toLocaleDateString("de-DE", { day: "numeric", month: "short" });
}

export function dateShort(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString("de-DE", { weekday: "short", day: "numeric", month: "short" });
}

export function timeShort(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString("de-DE", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
