/**
 * /r/join?t=<token> — where an invite's QR code lands.
 * Name and colour (prefilled by whoever invited), a 4-digit PIN twice,
 * then "Yorik on your home screen" + reminders. No email, no password:
 * this phone becomes a trusted device that opens Yorik with the PIN.
 * Backend: backend/member_invites.py.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { AlertCircle, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { ScrollPage } from "@/components/ScrollPage";
import { PinPad } from "@/components/PinPad";
import { PhoneSetup } from "@/components/PhoneSetup";

interface Preview { name: string; color: string | null; role: string; palette: string[] }
type Step = "loading" | "problem" | "name" | "pin" | "confirm" | "phone";

export function JoinScreen() {
  const { t } = useTranslation();
  const token = new URLSearchParams(window.location.search).get("t") || "";
  const [step, setStep] = useState<Step>("loading");
  const [problem, setProblem] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [name, setName] = useState("");
  const [color, setColor] = useState<string | null>(null);
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [pinError, setPinError] = useState<string | undefined>();

  useEffect(() => {
    if (!token) { setProblem(t("onboarding.join.missingCode")); setStep("problem"); return; }
    api.get<Preview>(`/api/auth/invite/${encodeURIComponent(token)}`)
      .then(p => { setPreview(p); setName(p.name); setColor(p.color || p.palette[0]); setStep("name"); })
      .catch(e => { setProblem(e?.message || t("onboarding.join.cantOpen")); setStep("problem"); });
  }, [token]);  // eslint-disable-line react-hooks/exhaustive-deps

  async function finish(confirmPin: string) {
    if (confirmPin !== pin) { setPinError(t("onboarding.join.pinMismatch")); setPin(""); setStep("pin"); return false; }
    setBusy(true);
    try {
      await api.post(`/api/auth/invite/${encodeURIComponent(token)}/accept`, { name: name.trim(), color, pin });
      setStep("phone");
      return true;
    } catch (e: any) {
      setProblem(e?.message || t("onboarding.join.failed")); setStep("problem");
      return true;
    } finally { setBusy(false); }
  }

  const initial = (name.trim()[0] || "?").toUpperCase();

  return (
    <ScrollPage className="bg-background text-foreground" innerClassName="items-start sm:items-center px-5 py-10">
      <div className="w-full max-w-sm">
        <div className="flex items-center gap-3 mb-8">
          <img src="/r/butler-mark.png" alt="" className="w-10 h-10 object-contain dark:invert" />
          <span className="font-semibold">Yorik</span>
        </div>

        {step === "loading" && <div className="flex justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-muted-foreground" /></div>}

        {step === "problem" && (
          <div className="space-y-4">
            <div className="flex items-start gap-3 p-4 rounded-xl bg-amber-500/10 border border-amber-500/20">
              <AlertCircle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
              <p className="text-sm">{problem}</p>
            </div>
            <a href="/r/home" className="block text-center text-sm text-muted-foreground underline">{t("onboarding.join.openYorik")}</a>
          </div>
        )}

        {step === "name" && preview && (
          <div className="space-y-6">
            <div>
              <h1 className="text-3xl font-semibold leading-tight">{t("onboarding.join.title")}</h1>
              <p className="text-muted-foreground mt-2">{t("onboarding.join.subtitle")}</p>
            </div>
            <div className="flex justify-center">
              <span className="w-20 h-20 rounded-full flex items-center justify-center text-3xl font-semibold text-white"
                    style={{ background: color || "#6d5bd0" }}>{initial}</span>
            </div>
            <label className="block">
              <span className="text-sm font-medium">{t("onboarding.join.nameLabel")}</span>
              <input value={name} onChange={e => setName(e.target.value)} maxLength={60} autoComplete="given-name"
                     className="mt-1.5 w-full h-12 px-4 rounded-xl bg-card border border-border text-lg focus:outline-none focus:ring-2 focus:ring-ring/40" />
            </label>
            <div>
              <span className="text-sm font-medium">{t("onboarding.join.colourLabel")}</span>
              <div className="mt-2 flex flex-wrap gap-3">
                {preview.palette.map(c => (
                  <button key={c} onClick={() => setColor(c)} aria-label={t("onboarding.join.colourAria", { color: c })}
                          className={cn("w-10 h-10 rounded-full transition", color === c && "ring-4 ring-offset-2 ring-offset-background ring-foreground/60")}
                          style={{ background: c }} />
                ))}
              </div>
            </div>
            <button onClick={() => setStep("pin")} disabled={!name.trim()}
                    className="w-full h-12 rounded-xl bg-primary text-primary-foreground font-medium disabled:opacity-50">
              {t("onboarding.join.next")}
            </button>
          </div>
        )}

        {step === "pin" && (
          <div className="space-y-4">
            <p className="text-muted-foreground text-sm text-center">{t("onboarding.join.pinExplain")}</p>
            <PinPad prompt={t("onboarding.join.pickPin")} errorText={pinError}
                    onSubmit={(p) => { setPin(p); setPinError(undefined); setStep("confirm"); return true; }} />
          </div>
        )}

        {step === "confirm" && (
          <PinPad prompt={t("onboarding.join.confirmPin")} busy={busy} onSubmit={finish} onCancel={() => { setPin(""); setStep("pin"); }} />
        )}

        {step === "phone" && (
          <div className="space-y-6">
            <div>
              <h1 className="text-3xl font-semibold leading-tight">{t("onboarding.join.youreIn", { name: name.trim().split(" ")[0] })}</h1>
              <p className="text-muted-foreground mt-2">{t("onboarding.join.lastThings")}</p>
            </div>
            <PhoneSetup doneLabel={t("onboarding.join.openYorik")} onDone={() => { window.location.href = "/r/home"; }} />
          </div>
        )}
      </div>
    </ScrollPage>
  );
}
