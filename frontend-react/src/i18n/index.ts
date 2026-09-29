/**
 * Translations. English is the source language: every text in the UI
 * is written in English and goes through `t()`; other languages are
 * catalogues next to it. One JSON file per app and language under
 * `locales/<lang>/<app>.json` (keys are prefixed with the app, e.g.
 * `pipelines.approve`), so screens can be translated independently.
 *
 * The language is the signed-in person's (`user.language`, set in
 * Settings → Profile), before sign-in the browser's. A missing key in
 * a language falls back to English, never to the key itself.
 */
import i18n from "i18next";
import { initReactI18next } from "react-i18next";

type Catalogue = Record<string, unknown>;

const files = import.meta.glob<{ default: Catalogue }>("./locales/*/*.json", { eager: true });

const resources: Record<string, { translation: Catalogue }> = {};
for (const [path, mod] of Object.entries(files)) {
  const m = path.match(/\.\/locales\/([^/]+)\/([^/]+)\.json$/);
  if (!m) continue;
  const [, lang, app] = m;
  resources[lang] ??= { translation: {} };
  resources[lang].translation[app] = mod.default;
}

export const LANGUAGES = Object.keys(resources);

function browserLanguage(): string {
  const l = (navigator.language || "en").split("-")[0].toLowerCase();
  return LANGUAGES.includes(l) ? l : "en";
}

void i18n.use(initReactI18next).init({
  resources,
  lng: browserLanguage(),
  fallbackLng: "en",
  interpolation: { escapeValue: false },   // React escapes already
  returnNull: false,
});

/** Switch to the person's language (unknown ones stay English). */
export function applyLanguage(language: string | null | undefined): void {
  const l = (language || "").split("-")[0].toLowerCase();
  const next = LANGUAGES.includes(l) ? l : "en";
  if (i18n.language !== next) void i18n.changeLanguage(next);
  document.documentElement.lang = next;
}

/** The locale for dates, numbers and money in the current language. */
export function locale(): string {
  return i18n.language === "de" ? "de-DE" : "en-US";
}

export default i18n;
