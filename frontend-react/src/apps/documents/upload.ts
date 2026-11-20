/**
 * Uploading several documents at once, so that one bad file never costs
 * the others (2026-11-20: a member picked 13 PDFs and one HTML page and
 * nothing at all arrived).
 *
 * Every file is tried on its own and gets its own result; a dropped
 * connection or a busy server is tried once more. Files the server cannot
 * take are not sent, and say why.
 */

/** What the server files (backend/documents.py SUPPORTED_MIME); photos become PDFs. */
export const UPLOAD_EXTENSIONS = [".pdf", ".docx", ".txt", ".md"];
export const UPLOAD_ACCEPT = UPLOAD_EXTENSIONS.join(",") + ",image/*";

export type UploadReason = "type" | "size" | "network" | "server";
export type UploadResult = { name: string; ok: boolean; reason?: UploadReason; detail?: string };

export function acceptable(file: File): boolean {
  const name = file.name.toLowerCase();
  return file.type.startsWith("image/") || UPLOAD_EXTENSIONS.some(ext => name.endsWith(ext));
}

const RETRY_AFTER_MS = 1500;

async function sendOne(file: File, role: string, fetchImpl: typeof fetch): Promise<UploadResult> {
  const fd = new FormData();
  fd.append("file", file);
  const params = new URLSearchParams({
    role,
    title: file.name.replace(/\.[^.]+$/, ""),
    allowed_roles: "admin,member",
  });
  let r: Response;
  try {
    r = await fetchImpl(`/api/documents/upload?${params}`, { method: "POST", body: fd, credentials: "include" });
  } catch {
    return { name: file.name, ok: false, reason: "network" };
  }
  if (r.ok) return { name: file.name, ok: true };
  const detail = await r.text().then(t => {
    try { return String(JSON.parse(t).detail ?? t); } catch { return t; }
  }).catch(() => "");
  if (r.status === 415) return { name: file.name, ok: false, reason: "type", detail };
  if (r.status === 413) return { name: file.name, ok: false, reason: "size", detail };
  return { name: file.name, ok: false, reason: r.status >= 500 ? "network" : "server", detail: `${r.status} ${detail}`.trim() };
}

/** Uploads one after the other; onProgress(done, total) after each. */
export async function uploadAll(
  files: File[],
  role: string,
  onProgress?: (done: number, total: number) => void,
  fetchImpl: typeof fetch = fetch,
  wait: (ms: number) => Promise<void> = ms => new Promise(res => setTimeout(res, ms)),
): Promise<UploadResult[]> {
  const results: UploadResult[] = [];
  onProgress?.(0, files.length);
  for (const file of files) {
    let res: UploadResult;
    if (!acceptable(file)) {
      res = { name: file.name, ok: false, reason: "type" };
    } else {
      res = await sendOne(file, role, fetchImpl);
      if (!res.ok && res.reason === "network") {
        await wait(RETRY_AFTER_MS);
        res = await sendOne(file, role, fetchImpl);
      }
    }
    results.push(res);
    onProgress?.(results.length, files.length);
  }
  return results;
}
