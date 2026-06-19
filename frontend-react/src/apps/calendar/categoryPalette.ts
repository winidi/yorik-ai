/**
 * Event-category colour palette.
 *
 * Mirror of backend/event_categories.py. Subtle on purpose — calendar
 * is dense, vibrant fills wash out the text and make the day grid
 * look like a circus. We use the Tailwind 500 hue as accent + a
 * very light fill (~12% opacity in light mode, ~25% in dark) + a
 * 2px left stripe for fast category recognition.
 *
 * To add a category: add the slug here AND in backend/event_categories.py.
 * To re-skin: change only this file.
 */

export type EventCategory =
  | "family"
  | "business"
  | "drive"
  | "health"
  | "personal"
  | "social";

export interface CategorySwatch {
  /** Display label for picker chips */
  label: string;
  /** Stripe + text colour (Tailwind 500-ish) */
  accent: string;
  /** Background fill (CSS rgba string, kept subtle) */
  fill: string;
}

export const CATEGORY_PALETTE: Record<EventCategory, CategorySwatch> = {
  family:   { label: "Familie",  accent: "#10b981", fill: "rgba(16,185,129,0.12)" },  // emerald-500
  business: { label: "Arbeit",   accent: "#64748b", fill: "rgba(100,116,139,0.12)" }, // slate-500
  drive:    { label: "Anfahrt",  accent: "#f59e0b", fill: "rgba(245,158,11,0.16)" },  // amber-500 (slightly stronger so it pops as a transit-warning hue)
  health:   { label: "Gesundheit", accent: "#f43f5e", fill: "rgba(244,63,94,0.12)" }, // rose-500
  personal: { label: "Persönlich", accent: "#8b5cf6", fill: "rgba(139,92,246,0.12)" },// violet-500
  social:   { label: "Sozial",   accent: "#0ea5e9", fill: "rgba(14,165,233,0.12)" },  // sky-500
};

/** Render order for category-picker UIs. */
export const CATEGORY_ORDER: EventCategory[] = [
  "family", "business", "drive", "health", "personal", "social",
];

const ALL = new Set<string>(CATEGORY_ORDER);

export function isEventCategory(s: string | null | undefined): s is EventCategory {
  return !!s && ALL.has(s);
}

/** Convenience: given an event row, return its swatch or null. */
export function swatchFor(category: string | null | undefined): CategorySwatch | null {
  if (!isEventCategory(category)) return null;
  return CATEGORY_PALETTE[category];
}
