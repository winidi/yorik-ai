import i18n, { locale } from "@/i18n";
import { formatDate } from "@/i18n/format";
import type { Attention, PipelineState } from "./types";

export function attentionLabel(a: Attention | null): string {
  if (!a) return "";
  return i18n.t(`pipelines.attention.${a}`, { defaultValue: a });
}

export function stateLabel(s: PipelineState): string {
  return i18n.t(`pipelines.state.${s}`, { defaultValue: s });
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
  if (days === 0) return i18n.t("pipelines.rel.today");
  if (days === 1) return i18n.t("pipelines.rel.tomorrow");
  if (days === -1) return i18n.t("pipelines.rel.yesterday");
  if (days > 1 && days < 14) return i18n.t("pipelines.rel.inDays", { count: days });
  if (days < -1 && days > -14) return i18n.t("pipelines.rel.daysAgo", { count: -days });
  return i18n.t("pipelines.rel.onDate", { date: formatDate(d, { day: "numeric", month: "short" }) });
}

export function dateShort(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return formatDate(d, { weekday: "short", day: "numeric", month: "short" });
}

export function timeShort(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString(locale(), { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
