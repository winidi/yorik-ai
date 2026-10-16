/**
 * ProfileLookCard — Settings → You. Your colour and your photo: what the
 * calendar, the task columns, the family board and the kiosk sign-in
 * show for you. Backend: /api/profile/look, /api/profile/avatar.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Camera, Loader2, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";
import { useAuth } from "@/components/AuthGate";
import { PersonAvatar } from "@/components/PersonAvatar";
import { invalidatePeople } from "@/lib/people";
import { cn } from "@/lib/utils";

type Look = { color: string; avatar_url: string | null; palette: string[] };

export function ProfileLookCard({ toast }: { toast: (text: string, kind?: "info" | "success" | "error") => void }) {
  const { t } = useTranslation();
  const auth = useAuth();
  const name = (auth.user as any)?.name || "";
  const [look, setLook] = useState<Look | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [custom, setCustom] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try { setLook(await api.get<Look>("/api/profile/look")); }
    catch (e: any) { toast(t("settings.look.loadFailed", { msg: e.message }), "error"); }
  }, [toast, t]);
  useEffect(() => { load(); }, [load]);

  async function setColor(color: string) {
    setBusy("color");
    try {
      setLook(await api.patch<Look>("/api/profile/look", { color }));
      invalidatePeople();
      toast(t("settings.look.colourSaved"), "success");
    } catch (e: any) { toast(t("settings.look.saveFailed", { msg: e?.message || e }), "error"); }
    finally { setBusy(null); }
  }

  async function upload(file: File) {
    setBusy("photo");
    try {
      const form = new FormData();
      form.append("image", file, file.name);
      setLook(await api.postForm<Look>("/api/profile/avatar", form));
      invalidatePeople();
      toast(t("settings.look.photoSaved"), "success");
    } catch (e: any) { toast(t("settings.look.uploadFailed", { msg: e?.message || e }), "error"); }
    finally { setBusy(null); if (fileRef.current) fileRef.current.value = ""; }
  }

  async function removePhoto() {
    setBusy("photo");
    try {
      setLook(await api.delete<Look>("/api/profile/avatar"));
      invalidatePeople();
      toast(t("settings.look.photoRemoved"), "success");
    } catch (e: any) { toast(t("settings.look.removeFailed", { msg: e?.message || e }), "error"); }
    finally { setBusy(null); }
  }

  if (!look) return null;
  return (
    <div className="bg-card border border-border rounded-xl p-5">
      <h3 className="text-xs font-semibold text-muted-foreground mb-3">{t("settings.look.title")}</h3>
      <div className="flex items-start gap-4">
        <PersonAvatar name={name} color={look.color} avatarUrl={look.avatar_url} size={72} className="ring-4 ring-background shadow" />
        <div className="flex-1 min-w-0">
          <p className="text-xs text-muted-foreground">
            {t("settings.look.intro")}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {look.palette.map(c => (
              <button key={c} onClick={() => setColor(c)} disabled={busy === "color"} title={c}
                      className={cn("w-8 h-8 rounded-full ring-offset-2 ring-offset-background transition",
                                    look.color === c ? "ring-2 ring-foreground scale-110" : "hover:scale-105")}
                      style={{ background: c }} aria-label={t("settings.look.colour", { color: c })} />
            ))}
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground ml-1">
              <input type="color" value={custom || look.color} onChange={e => setCustom(e.target.value)}
                     onBlur={() => custom && custom !== look.color && setColor(custom)}
                     className="w-8 h-8 rounded-full border-0 bg-transparent cursor-pointer" aria-label={t("settings.look.customColour")} />
              {t("settings.look.custom")}
            </label>
          </div>
          <div className="mt-3 flex items-center gap-2">
            <input ref={fileRef} type="file" accept="image/*" hidden id="profile-photo-input"
                   onChange={e => { const f = e.target.files?.[0]; if (f) void upload(f); }} />
            <button onClick={() => fileRef.current?.click()} disabled={busy === "photo"}
                    className="flex items-center gap-2 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-50">
              {busy === "photo" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Camera className="w-4 h-4" />}
              {look.avatar_url ? t("settings.look.changePhoto") : t("settings.look.addPhoto")}
            </button>
            {look.avatar_url && (
              <button onClick={removePhoto} disabled={busy === "photo"}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted disabled:opacity-50">
                <Trash2 className="w-4 h-4" /> {t("settings.look.remove")}
              </button>
            )}
          </div>
          <p className="mt-2 text-xs text-muted-foreground">{t("settings.look.cropNote")}</p>
        </div>
      </div>
    </div>
  );
}
