/**
 * "Back up this phone's photos": the Immich app, the address it needs,
 * the login, and the one switch to flip. Everything copyable, nothing to
 * look up. Backend: GET /api/setup/photos-phone.
 */
import { useEffect, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { Check, Copy, ExternalLink, Loader2, X } from "lucide-react";
import { api } from "@/lib/api";
import { detectPlatform } from "@/components/PhoneSetup";

interface Info { server_url: string | null; email: string | null; password: string | null; uses_yorik_password: boolean }

function Copyable({ label, value, secret }: { label: string; value: string; secret?: boolean }) {
  const { t } = useTranslation();
  const [done, setDone] = useState(false);
  const [show, setShow] = useState(!secret);
  return (
    <div>
      <div className="text-xs text-muted-foreground mb-1">{label}</div>
      <div className="flex items-center gap-2">
        <code className="flex-1 min-w-0 text-sm bg-muted/50 border border-border rounded-lg px-3 py-2 break-all">
          {show ? value : "••••••••••"}
        </code>
        {secret && <button onClick={() => setShow(s => !s)} className="text-xs text-muted-foreground underline">{show ? t("photos.phoneSetup.hide") : t("photos.phoneSetup.show")}</button>}
        <button onClick={async () => { try { await navigator.clipboard.writeText(value); setDone(true); setTimeout(() => setDone(false), 1500); } catch { setShow(true); } }}
                className="w-9 h-9 rounded-lg border border-border flex items-center justify-center shrink-0" aria-label={t("photos.phoneSetup.copy", { label })}>
          {done ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
        </button>
      </div>
    </div>
  );
}

export function PhotosPhoneSetup({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const [info, setInfo] = useState<Info | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.get<Info>("/api/setup/photos-phone").then(setInfo).catch(e => setErr(e?.message || t("photos.phoneSetup.loadFailed"))); }, []);
  const p = detectPlatform();
  const store = p === "ios" ? "https://apps.apple.com/app/immich/id1613945652"
    : p === "android" ? "https://play.google.com/store/apps/details?id=app.alextran.immich"
    : "https://immich.app/docs/features/mobile-app";

  return (
    <div className="fixed inset-0 z-[850] flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <div className="w-full max-w-md max-h-[90vh] overflow-y-auto bg-background border border-border rounded-2xl shadow-2xl p-6 relative space-y-5" onClick={e => e.stopPropagation()}>
        <button onClick={onClose} className="absolute top-3 right-3 w-9 h-9 rounded-md hover:bg-muted flex items-center justify-center" aria-label={t("photos.phoneSetup.close")}><X className="w-4 h-4" /></button>
        <div>
          <h2 className="text-xl font-semibold">{t("photos.phoneSetup.title")}</h2>
          <p className="text-sm text-muted-foreground mt-1">{t("photos.phoneSetup.subtitle")}</p>
        </div>
        {err && <p className="text-sm text-destructive">{err}</p>}
        {!info && !err && <Loader2 className="w-5 h-5 animate-spin text-muted-foreground" />}
        {info && (
          <ol className="space-y-5">
            <li className="space-y-2">
              <div className="font-medium">{t("photos.phoneSetup.step1")}</div>
              <a href={store} target="_blank" rel="noopener noreferrer"
                 className="flex items-center justify-center gap-1.5 h-10 rounded-xl border border-border bg-card font-medium text-sm">
                {p === "ios" ? t("photos.phoneSetup.appStore") : p === "android" ? t("photos.phoneSetup.googlePlay") : t("photos.phoneSetup.otherStore")} <ExternalLink className="w-3.5 h-3.5" />
              </a>
            </li>
            <li className="space-y-2">
              <div className="font-medium">{t("photos.phoneSetup.step2")}</div>
              {info.server_url
                ? <Copyable label={t("photos.phoneSetup.serverAddress")} value={info.server_url} />
                : <p className="text-sm text-muted-foreground">{t("photos.phoneSetup.needsPublish")}</p>}
            </li>
            <li className="space-y-2">
              <div className="font-medium">{t("photos.phoneSetup.step3")}</div>
              <p className="text-xs text-muted-foreground"><Trans i18nKey="photos.phoneSetup.signInHint" components={{ b: <b /> }} /></p>
              {info.email && <Copyable label={t("photos.phoneSetup.email")} value={info.email} />}
              {info.password
                ? <Copyable label={t("photos.phoneSetup.password")} value={info.password} secret />
                : <p className="text-sm text-muted-foreground">{t("photos.phoneSetup.samePassword")}</p>}
            </li>
            <li className="space-y-1">
              <div className="font-medium">{t("photos.phoneSetup.step4")}</div>
              <p className="text-sm text-muted-foreground"><Trans i18nKey="photos.phoneSetup.backupHint" components={{ b: <b /> }} /></p>
            </li>
          </ol>
        )}
        <button onClick={onClose} className="w-full h-11 rounded-xl bg-primary text-primary-foreground font-medium">{t("photos.phoneSetup.done")}</button>
      </div>
    </div>
  );
}
