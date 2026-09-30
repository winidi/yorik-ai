/**
 * Dates, times, numbers and money in the person's language. Use these
 * instead of `toLocaleDateString("de-DE")` or hand-built German dates:
 * an English household gets "Tuesday, September 29", a German one
 * "Dienstag, 29. September".
 */
import { locale } from "./index";

type D = Date | string | number;
const asDate = (d: D) => (d instanceof Date ? d : new Date(d));

export function formatDate(d: D, opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "short", year: "numeric" }): string {
  return asDate(d).toLocaleDateString(locale(), opts);
}

export function formatWeekdayDate(d: D): string {
  return asDate(d).toLocaleDateString(locale(), { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

export function formatTime(d: D): string {
  return asDate(d).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
}

export function formatDateTime(d: D): string {
  return `${formatDate(d)} ${formatTime(d)}`;
}

export function formatNumber(n: number, opts?: Intl.NumberFormatOptions): string {
  return n.toLocaleString(locale(), opts);
}

let householdCurrency = "EUR";

/** The household's money (from /api/auth/me), set once signed in. */
export function setHouseholdCurrency(code: string | null | undefined): void {
  const c = (code || "").trim().toUpperCase();
  if (c.length === 3) householdCurrency = c;
}

export function currency(): string {
  return householdCurrency;
}

/** Money: the currency comes with the amount (bank rows, invoices); an
 *  amount without one is in the household's currency. */
export function formatMoney(amount: number, currency?: string | null): string {
  return amount.toLocaleString(locale(), { style: "currency", currency: currency || householdCurrency });
}
