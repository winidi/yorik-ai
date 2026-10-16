/**
 * Backups in three taps: pick the drive (a USB stick or disk Yorik sees),
 * Yorik makes the passphrase itself and shows a recovery sheet to print,
 * and backups run every night at 3:00. The full form stays below for
 * everything else. Backend: /api/storage/volumes, PATCH /api/backup/config.
 */
import { useEffect, useState } from "react";
import { Check, HardDrive, Loader2, Printer, Usb } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { locale } from "@/i18n";
import { cn } from "@/lib/utils";

interface Volume { name: string; mountpoint: string; size?: string; label?: string; hotplug: boolean; suggested_target: string }

// 6 groups of 4 from an alphabet without look-alikes (no 0/O, 1/l/I).
function makePassphrase(): string {
  const abc = "abcdefghjkmnpqrstuvwxyz23456789";
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  const chars = Array.from(bytes, b => abc[b % abc.length]).join("");
  return chars.match(/.{4}/g)!.join("-");
}

export function EasyBackup({ onDone }: { onDone?: () => void }) {
  const { t } = useTranslation();
  const [vols, setVols] = useState<Volume[] | null>(null);
  const [pick, setPick] = useState<Volume | null>(null);
  const [phrase] = useState(makePassphrase);
  const [step, setStep] = useState<"drive" | "sheet" | "done">("drive");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.get<Volume[]>("/api/storage/volumes").then(v => setVols(v.filter(x => x.hotplug || x.suggested_target))).catch(() => setVols([]));
  }, []);

  async function save() {
    if (!pick) return;
    setBusy(true); setErr(null);
    try {
      await api.patch("/api/backup/config", { target_path: pick.suggested_target, schedule: "03:00", passphrase: phrase });
      setStep("done"); onDone?.();
    } catch (e: any) { setErr(e?.message || t("settings.easyBackup.saveFailed")); }
    finally { setBusy(false); }
  }

  function printSheet() {
    const w = window.open("", "_blank", "width=700,height=800");
    if (!w) return;
    const today = new Date().toLocaleDateString(locale());
    w.document.write(`<!doctype html><title>${t("settings.easyBackup.sheet.title")}</title>
      <body style="font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;line-height:1.5">
      <h1 style="margin-bottom:4px">${t("settings.easyBackup.sheet.title")}</h1><p style="color:#555">${t("settings.easyBackup.sheet.made", { date: today })}</p>
      <p>${t("settings.easyBackup.sheet.saves", { drive: `<b>${pick?.label || pick?.name || t("settings.easyBackup.sheet.theDrive")}</b>` })}
      ${t("settings.easyBackup.sheet.needPhrase")}</p>
      <p style="font:600 26px ui-monospace,monospace;letter-spacing:2px;border:2px solid #000;padding:16px;text-align:center">${phrase}</p>
      <p style="color:#555">${t("settings.easyBackup.sheet.howTo")}</p>
      <script>window.print()</script></body>`);
    w.document.close();
  }

  if (step === "done") {
    return (
      <div className="flex items-start gap-3 p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/20">
        <Check className="w-5 h-5 text-emerald-500 shrink-0" />
        <p className="text-sm">{t("settings.easyBackup.doneBefore")}<b>{pick?.label || pick?.name}</b>{t("settings.easyBackup.doneAfter")}</p>
      </div>
    );
  }

  return (
    <div className="p-4 rounded-xl border border-primary/30 bg-primary/5 space-y-4">
      <div>
        <div className="font-semibold">{t("settings.easyBackup.title")}</div>
        <p className="text-sm text-muted-foreground">{t("settings.easyBackup.intro")}</p>
      </div>
      {err && <p className="text-sm text-destructive">{err}</p>}
      {step === "drive" && (
        <>
          {vols === null ? <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
            : vols.length === 0 ? <p className="text-sm text-muted-foreground">{t("settings.easyBackup.noDriveBefore")}<button className="underline" onClick={() => { setVols(null); api.get<Volume[]>("/api/storage/volumes").then(v => setVols(v.filter(x => x.hotplug || x.suggested_target))).catch(() => setVols([])); }}>{t("settings.easyBackup.lookAgain")}</button>{t("settings.easyBackup.noDriveAfter")}</p>
            : (
              <div className="grid gap-2">
                {vols.map(v => (
                  <button key={v.mountpoint} onClick={() => setPick(v)}
                          className={cn("flex items-center gap-3 p-3 rounded-xl border text-left transition",
                                        pick?.mountpoint === v.mountpoint ? "border-primary/60 bg-primary/10" : "border-border bg-card hover:bg-muted/50")}>
                    {v.hotplug ? <Usb className="w-5 h-5 text-muted-foreground" /> : <HardDrive className="w-5 h-5 text-muted-foreground" />}
                    <span className="flex-1 min-w-0">
                      <span className="block text-sm font-medium truncate">{v.label || v.name}</span>
                      <span className="block text-xs text-muted-foreground">{v.size ? `${v.size} · ` : ""}{v.hotplug ? t("settings.easyBackup.removable") : t("settings.easyBackup.builtIn")}</span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          <button onClick={() => setStep("sheet")} disabled={!pick}
                  className="w-full h-10 rounded-xl bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50">{t("settings.easyBackup.next")}</button>
        </>
      )}
      {step === "sheet" && (
        <>
          <p className="text-sm">{t("settings.easyBackup.phraseBefore")}<b>{t("settings.easyBackup.phraseStrong")}</b>{t("settings.easyBackup.phraseAfter")}</p>
          <div className="text-center font-mono text-lg font-semibold tracking-wider p-3 rounded-xl bg-card border border-border break-all">{phrase}</div>
          <button onClick={printSheet} className="w-full h-10 rounded-xl border border-border bg-card text-sm font-medium flex items-center justify-center gap-2">
            <Printer className="w-4 h-4" /> {t("settings.easyBackup.printSheet")}
          </button>
          <button onClick={save} disabled={busy}
                  className="w-full h-10 rounded-xl bg-primary text-primary-foreground text-sm font-medium disabled:opacity-60 flex items-center justify-center gap-2">
            {busy && <Loader2 className="w-4 h-4 animate-spin" />} {t("settings.easyBackup.keptSafe")}
          </button>
        </>
      )}
    </div>
  );
}
