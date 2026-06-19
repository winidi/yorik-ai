/**
 * Yorik Documents — polished React port mirroring the chat/whatsapp/email
 * three-pane shell. Left: document list (filter + upload). Center: live
 * preview (PDF iframe, image, syntax-highlighted text, or download CTA).
 * Right: metadata + actions (download, reindex, delete, open-in-Paperless).
 *
 * Search switches the center pane to a list of matching chunks across all
 * docs; clicking a chunk opens the parent doc with the chunk pre-scrolled.
 *
 * Drag-and-drop upload: drop a file anywhere in the app to upload. The
 * overlay only appears once the user actually drags onto the window.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Loader2, Upload, Search, RefreshCw, RotateCw, Download, FileText,
  X, ExternalLink, Eye, FolderOpen, Sparkles, FileImage, FileCode,
  File as FileIcon, AlertCircle, Plus, Check, Bookmark, BookmarkCheck,
  Type, CheckCircle2, XCircle, Info, Mail,
  Tag as TagIcon, User as UserIcon2, Files as FilesIcon, Calendar as CalIcon, ChevronRight,
} from "lucide-react";
import { useDocBucket } from "./DocBucketContext";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Dock } from "@/components/Dock";
import {
  useTriPane, MobileTopBar, MobileBackdrop,
  mobileAsideLeft, mobileAsideRight,
} from "@/components/MobileShell";
import type { YorikDocument, DocumentSearchHit, DocVisibility, SearchResponse, SearchLegStatus } from "./types";

// Facet types — match the backend's /api/documents/facets response.
type FacetKind = "tag" | "correspondent" | "document_type" | "year";
interface FacetNode {
  kind: FacetKind;
  id: number;          // Paperless object id, or the year itself
  label: string;
  count: number;
}
interface FacetsResponse {
  tags:            Array<{ id: number; name: string; document_count: number; color?: string | null }>;
  correspondents:  Array<{ id: number; name: string; document_count: number }>;
  document_types:  Array<{ id: number; name: string; document_count: number }>;
  years:           Array<{ year: number; document_count: number }>;
}

interface DocsListResponse {
  results: YorikDocument[];
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
  has_prev: boolean;
}

export function DocumentsApp() {
  const { data: me } = useApi<{ user?: { id: number; name: string; role: string } }>("/api/auth/me", []);
  const role = me?.user?.role || "admin";

  // Browse-mode state.
  //   activeFacetKind = which "folder type" is showing in the center
  //                     grid (tag / correspondent / type / year). Drives
  //                     the folder grid when no specific folder picked.
  //   activeFacet     = the specific folder (tag X, correspondent Y…)
  //                     the user clicked into — drives the doc-card grid.
  //   selectedId      = a single doc the user picked from the card grid
  //                     or from search results — drives the preview.
  // Three modes derive from these: folder grid → doc-card grid → preview.
  const [activeFacetKind, setActiveFacetKind] = useState<FacetKind>("tag");
  const [activeFacet, setActiveFacet] = useState<FacetNode | null>(null);
  const [page, setPage] = useState(1);

  // Reset to page 1 whenever the facet filter changes — otherwise
  // navigating from "Tags > Rechnung (page 5)" back to "All" would
  // try to render page 5 of "All" which is rarely what the user wants.
  useEffect(() => { setPage(1); }, [activeFacet?.kind, activeFacet?.id]);

  // Doc-card grid page size. 24 fits a 4×6 / 3×8 grid nicely and keeps
  // the per-page thumbnail load reasonable (~700 KB total at 30 KB/thumb).
  const PAGE_SIZE = 24;

  // Build the list-fetch URL from the active facet + page. Docs always
  // load (no facet = "All documents" view). useApi keys on the path
  // string, so changing either filter or page re-fetches cleanly.
  const facetQuery = activeFacet
    ? `&${activeFacet.kind === "year" ? "year" : activeFacet.kind}=${activeFacet.id}`
    : "";
  const listApi = useApi<DocsListResponse>(
    `/api/documents?role=${encodeURIComponent(role)}&page=${page}&page_size=${PAGE_SIZE}${facetQuery}`,
    [role, activeFacet?.kind, activeFacet?.id, page],
  );
  const docsResp = listApi.data;
  const docs = docsResp?.results || [];

  // Facets re-poll every 30s so new autotagger output shows up as the
  // background job runs.
  const facetsApi = useApi<FacetsResponse>(
    `/api/documents/facets?role=${encodeURIComponent(role)}`,
    [role],
    30000,
  );

  const [selectedId, setSelectedId] = useState<number | null>(null);

  // Deep-link: /r/documents?doc=N&source=paperless|native — opens that
  // doc on mount so the chat's "In Documents öffnen" button (and any
  // LLM-driven navigate_to) lands on the right preview instead of the
  // default list view. Paperless docs use NEGATIVE id internally
  // (see backend list_documents); we apply that here.
  const [urlParams, setUrlParams] = useSearchParams();
  useEffect(() => {
    const docParam = urlParams.get("doc");
    if (!docParam) return;
    const n = parseInt(docParam, 10);
    if (Number.isNaN(n)) return;
    const isPaperless = (urlParams.get("source") || "paperless") === "paperless";
    setSelectedId(isPaperless ? -Math.abs(n) : Math.abs(n));
    // Strip the params so a reload doesn't keep snapping back to the
    // same doc after the user navigates around inside the app.
    const next = new URLSearchParams(urlParams);
    next.delete("doc");
    next.delete("source");
    setUrlParams(next, { replace: true });
    // Only run on mount — subsequent state changes mustn't re-trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [searchQuery, setSearchQuery] = useState("");
  const [searchHits, setSearchHits] = useState<DocumentSearchHit[] | null>(null);
  const [searchLegs, setSearchLegs] = useState<SearchResponse["legs"]>({});
  const [searching, setSearching] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [showUploadDialog, setShowUploadDialog] = useState(false);

  // When the user clicks a search result, the clicked doc is often NOT
  // in the current 50-row list — search covers the full corpus, the
  // list only covers the current page. Fetch the single doc directly
  // as a fallback so the preview pane actually renders something.
  // Direct fetch only works for NATIVE docs — Paperless docs (negative
  // ids) don't have a /api/documents/{id} endpoint. For those we fall
  // back to a synthetic stub so the preview pane still renders the
  // PDF + key actions; metadata is light but the document IS shown.
  const needsDirectFetch = selectedId !== null
    && selectedId > 0
    && !docs.find(d => d.id === selectedId);
  const directDocApi = useApi<YorikDocument>(
    needsDirectFetch
      ? `/api/documents/${selectedId}?role=${encodeURIComponent(role)}`
      : null,
    [selectedId, role, docs.length],
  );

  const selected = useMemo(() => {
    const fromList = docs.find(d => d.id === selectedId);
    if (fromList) return fromList;
    if (directDocApi.data) return directDocApi.data;
    // Paperless deep-link stub: enough to render the preview iframe + the
    // download / open-original buttons even when the doc isn't on the
    // current list page.
    if (selectedId !== null && selectedId < 0) {
      return {
        id: selectedId,
        source: "paperless",
        title: `Document ${Math.abs(selectedId)}`,
        mime_type: "application/pdf",
        bytes: null,
        created_at: null,
      } as any as YorikDocument;
    }
    return null;
  }, [docs, selectedId, directDocApi.data]);

  // No auto-select. Browse mode means: user lands on the card grid,
  // sees their docs as thumbnails, and chooses what to open. Auto-
  // opening a doc on every facet change fights against that.

  // ─── search ────────────────────────────────────────────────────────────
  const runSearch = useCallback(async (q: string) => {
    const query = q.trim();
    if (!query) {
      setSearchHits(null);
      setSearchLegs({});
      return;
    }
    setSearching(true);
    try {
      const resp = await api.post<SearchResponse>(
        `/api/documents/search?role=${encodeURIComponent(role)}`,
        { query, k: 12 },
      );
      setSearchHits(Array.isArray(resp?.hits) ? resp.hits : []);
      setSearchLegs(resp?.legs || {});
    } catch (e: any) {
      setSearchHits([]);
      setSearchLegs({});
      console.error(e);
    } finally {
      setSearching(false);
    }
  }, [role]);

  function clearSearch() {
    setSearchQuery("");
    setSearchHits(null);
    setSearchLegs({});
  }

  // Search-as-you-type. 300ms debounce strikes a balance between
  // feeling immediate and not hammering the semantic search endpoint
  // on every keystroke. Enter still fires immediately (handler clears
  // this timer first to avoid the duplicate call).
  const searchTimerRef = useRef<number | null>(null);
  useEffect(() => {
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    const q = searchQuery.trim();
    if (!q) return;
    searchTimerRef.current = window.setTimeout(() => { runSearch(q); }, 300);
    return () => {
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, [searchQuery, runSearch]);

  // ─── upload ────────────────────────────────────────────────────────────
  const uploadFiles = useCallback(async (files: FileList | File[]) => {
    if ((role !== "admin" && role !== "platform_admin")) {
      alert("Only admins can upload documents.");
      return;
    }
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const fd = new FormData();
        fd.append("file", file);
        const params = new URLSearchParams({
          role,
          title: file.name.replace(/\.[^.]+$/, ""),
          allowed_roles: "admin,member",
        });
        const r = await fetch(`/api/documents/upload?${params}`, {
          method: "POST",
          body: fd,
          credentials: "include",
        });
        if (!r.ok) {
          const t = await r.text().catch(() => "");
          throw new Error(`${file.name}: ${r.status} ${t}`);
        }
      }
      await listApi.refetch();
    } catch (err: any) {
      alert("Upload failed: " + err.message);
    } finally {
      setUploading(false);
    }
  }, [role, listApi]);

  // Force an immediate Paperless reconcile. The scheduled reconciler
  // runs only every 6 hours; a doc dropped into Paperless's consume
  // folder won't show up in Yorik's library until then unless the
  // user nudges it. POSTs to /api/documents/sync-paperless, then
  // refetches the list so newly-mirrored rows appear.
  const syncFromPaperless = useCallback(async () => {
    setSyncing(true);
    try {
      const r = await api.post<{
        ok?: boolean;
        checked?: number;
        missing?: number;
        ingested?: number;
        failed?: number;
        error?: string;
      }>("/api/documents/sync-paperless");
      if (r.error) {
        alert("Paperless sync error: " + r.error);
      } else if ((r.checked ?? 0) === 0) {
        // Paperless returned zero documents — almost always a token /
        // auth issue, NOT "Paperless is empty". Distinguish from the
        // legitimate "everything in sync" case below.
        alert(
          "Paperless returned 0 documents. If you have documents in Paperless, " +
          "this is usually a missing/wrong API token. Open Settings → Connectors " +
          "→ Paperless and paste a token from Paperless's Settings → API Tokens page."
        );
      } else if ((r.ingested ?? 0) === 0 && (r.missing ?? 0) === 0) {
        alert(`Already in sync — Paperless has ${r.checked} document(s), all mirrored locally.`);
      } else {
        alert(
          `Synced ${r.ingested ?? 0} new document(s) from Paperless ` +
          `(${r.checked} checked${r.failed ? `, ${r.failed} failed` : ""}).`
        );
      }
      await listApi.refetch();
    } catch (err: any) {
      alert("Paperless sync failed: " + (err?.message || "unknown"));
    } finally {
      setSyncing(false);
    }
  }, [listApi]);

  // Global drag overlay — desktop only. On touch-primary devices the
  // dragenter events fire inconsistently and the amber overlay would
  // flash mid-tap. We detect "real mobile" via a pointer/hover media
  // query rather than the presence of touch APIs (which many desktop
  // Chromiums expose even without a touchscreen, killing drop-anywhere).
  useEffect(() => {
    if (typeof window !== "undefined"
        && window.matchMedia?.("(pointer: coarse) and (hover: none)").matches) return;
    let depth = 0;
    function onEnter(e: DragEvent) {
      if (!e.dataTransfer?.types.includes("Files")) return;
      depth += 1;
      setDragOver(true);
    }
    function onLeave() {
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragOver(false);
    }
    function onDrop(e: DragEvent) {
      depth = 0;
      setDragOver(false);
      if (!e.dataTransfer?.files?.length) return;
      e.preventDefault();
      uploadFiles(e.dataTransfer.files);
    }
    function onOver(e: DragEvent) { e.preventDefault(); }
    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("dragover", onOver);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("drop", onDrop);
    };
  }, [uploadFiles]);

  const tri = useTriPane();

  return (
    <div className="flex h-screen bg-background text-foreground relative">
      <MobileBackdrop show={tri.leftOpen || tri.rightOpen} onClick={tri.closeAll} />
      {/* ── Document list ───────────────────────────────────── */}
      <aside className={cn(
        "w-[320px] border-r border-border flex flex-col bg-sidebar shrink-0",
        mobileAsideLeft(tri.leftOpen),
      )}>
        <header className="h-16 px-5 flex items-center justify-between border-b border-border">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-full bg-amber-500/15 flex items-center justify-center">
              <FolderOpen className="w-4 h-4 text-amber-500" />
            </div>
            <div>
              <div className="font-semibold leading-none">Documents</div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
                {(docsResp?.total ?? docs.length).toLocaleString()} file{(docsResp?.total ?? docs.length) === 1 ? "" : "s"}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={syncFromPaperless}
              disabled={(role !== "admin" && role !== "platform_admin") || syncing}
              title={(role === "admin" || role === "platform_admin")
                ? "Sync from Paperless now (picks up files dropped in the consume folder before the next 6h reconcile)"
                : "Admin only"}
              className={cn(
                "w-10 h-10 md:w-8 md:h-8 inline-flex items-center justify-center rounded-lg transition",
                (role === "admin" || role === "platform_admin")
                  ? "hover:bg-muted text-muted-foreground hover:text-foreground"
                  : "opacity-40 cursor-not-allowed",
              )}
            >
              <RefreshCw className={cn("w-5 h-5 md:w-4 md:h-4", syncing && "animate-spin")} />
            </button>
            <a
              href="/paperless/"
              target="_blank"
              rel="noopener noreferrer"
              title="Open Paperless (tags, correspondents, bulk operations)"
              className="w-10 h-10 md:w-8 md:h-8 inline-flex items-center justify-center rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition"
            >
              <ExternalLink className="w-5 h-5 md:w-4 md:h-4" />
            </a>
            <button
              onClick={() => setShowUploadDialog(true)}
              disabled={(role !== "admin" && role !== "platform_admin") || uploading}
              title={(role === "admin" || role === "platform_admin") ? "Upload documents" : "Admin only"}
              className={cn(
                "w-10 h-10 md:w-8 md:h-8 inline-flex items-center justify-center rounded-lg transition",
                (role === "admin" || role === "platform_admin")
                  ? "hover:bg-muted text-muted-foreground hover:text-foreground"
                  : "opacity-40 cursor-not-allowed",
              )}
            >
              {uploading
                ? <Loader2 className="w-5 h-5 md:w-4 md:h-4 animate-spin" />
                : <Plus className="w-5 h-5 md:w-4 md:h-4" />}
            </button>
          </div>
        </header>

        <div className="px-4 pt-3 pb-2">
          <div className="relative">
            {searching
              ? <Loader2 className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-violet-500 animate-spin" />
              : <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />}
            <input
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              onKeyDown={e => {
                if (e.key === "Enter") {
                  if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
                  runSearch(searchQuery);
                  // On mobile the search input lives inside the left drawer;
                  // keeping it open after submit obscures the result pane.
                  tri.closeAll();
                }
                if (e.key === "Escape") clearSearch();
              }}
              placeholder="Search your documents…"
              className="w-full h-9 pl-9 pr-8 rounded-full bg-muted/70 text-sm focus:outline-none focus:bg-muted focus:ring-2 focus:ring-ring/30 transition"
            />
            {searchQuery && (
              <button
                onClick={clearSearch}
                className="absolute right-2 top-1/2 -translate-y-1/2 w-5 h-5 rounded-full hover:bg-muted-foreground/20 text-muted-foreground flex items-center justify-center"
              >
                <X className="w-3 h-3" />
              </button>
            )}
            {searching && (
              <div className="absolute left-3 right-3 -bottom-1 h-px overflow-hidden rounded-full">
                <div className="docs-search-progress h-full w-1/3 bg-violet-500/70 rounded-full" />
              </div>
            )}
          </div>
        </div>

        <FacetTypeNav
          active={activeFacetKind}
          counts={{
            tag:            facetsApi.data?.tags?.length            ?? 0,
            correspondent:  facetsApi.data?.correspondents?.length  ?? 0,
            document_type:  facetsApi.data?.document_types?.length  ?? 0,
            year:           facetsApi.data?.years?.length           ?? 0,
          }}
          onPick={(kind) => {
            setActiveFacetKind(kind);
            setActiveFacet(null);
            setSelectedId(null);
            setSearchHits(null);
            setSearchQuery("");
          }}
        />

        <FacetSideList
          kind={activeFacetKind}
          facets={facetsApi.data}
          loading={facetsApi.loading}
          activeId={activeFacet?.id ?? null}
          onPick={(node) => {
            setActiveFacet(node);
            setSelectedId(null);
            setSearchHits(null);
            setSearchQuery("");
            tri.closeAll();
          }}
        />

        <footer className="border-t border-border px-4 py-3 text-xs text-muted-foreground flex items-center justify-between">
          <span>{me?.user?.name ? `Signed in · ${role}` : "Loading…"}</span>
          <button
            onClick={() => listApi.refetch()}
            title="Reload list"
            aria-label="Reload list"
            className="p-2 -m-2 text-muted-foreground hover:text-foreground transition"
          >
            <RotateCw className={cn("w-3.5 h-3.5", listApi.loading && "animate-spin")} />
          </button>
        </footer>
      </aside>

      {/* ── Center pane: preview or search results ──────────── */}
      <section className="flex-1 flex flex-col bg-background min-w-0 docs-bg">
        <MobileTopBar
          title={selected?.title || "Documents"}
          onMenuClick={() => tri.setLeftOpen(true)}
          onContextClick={() => tri.setRightOpen(true)}
          contextLabel="Details"
        />
        {searchHits !== null ? (
          <SearchResultsPane
            hits={searchHits}
            legs={searchLegs}
            searching={searching}
            query={searchQuery}
            onPickDoc={(docId) => {
              setSelectedId(docId);
              setSearchHits(null);
              setSearchQuery("");
            }}
            onClear={clearSearch}
          />
        ) : selected ? (
          <>
            <Breadcrumb
              facetKind={activeFacetKind}
              activeFacet={activeFacet}
              docTitle={selected.title}
              onAll={() => { setSelectedId(null); setActiveFacet(null); }}
              onFolder={() => { setSelectedId(null); }}
            />
            <PreviewPane doc={selected} role={role} />
          </>
        ) : (
          <DocCardGrid
            facet={activeFacet}
            facetKind={activeFacetKind}
            docs={docs}
            page={docsResp?.page ?? 1}
            total={docsResp?.total ?? 0}
            hasNext={docsResp?.has_next ?? false}
            hasPrev={docsResp?.has_prev ?? false}
            loading={listApi.loading}
            onPickDoc={(id) => setSelectedId(id)}
            onPrev={() => setPage(p => Math.max(1, p - 1))}
            onNext={() => setPage(p => p + 1)}
            onUp={() => setActiveFacet(null)}
            role={role}
            canUpload={(role === "admin" || role === "platform_admin")}
            onUpload={() => setShowUploadDialog(true)}
          />
        )}
      </section>

      {/* ── Metadata + actions ─────────────────────────────── */}
      <aside className={cn(
        "w-[320px] border-l border-border flex flex-col bg-card shrink-0",
        mobileAsideRight(tri.rightOpen),
      )}>
        {selected && searchHits === null ? (
          <MetadataPane
            doc={selected}
            role={role}
            onReindexed={() => listApi.refetch()}
          />
        ) : (
          <EmptyMetadata />
        )}
      </aside>

      {/* Drag-and-drop overlay */}
      {dragOver && (
        <div className="fixed inset-0 z-[900] flex items-center justify-center pointer-events-none">
          <div className="absolute inset-0 bg-amber-500/10 backdrop-blur-sm docs-drop-fade" />
          <div className="absolute inset-0 border-4 border-dashed border-amber-500 docs-drop-frame" />
          <div className="relative text-center docs-drop-card px-6">
            <div className="relative mx-auto mb-4 w-28 h-28 flex items-center justify-center">
              <span className="absolute inset-0 rounded-full bg-amber-500/20 docs-drop-ring" />
              <span className="absolute inset-0 rounded-full bg-amber-500/15 docs-drop-ring docs-drop-ring-delay" />
              <Upload className="w-16 h-16 text-amber-500 relative docs-drop-bounce" />
            </div>
            <div className="text-2xl font-semibold text-amber-500">Drop to upload</div>
            <div className="text-sm text-muted-foreground mt-1.5">PDFs, Word docs, markdown, images — anything you'd file away.</div>
          </div>
        </div>
      )}

      {showUploadDialog && (
        <UploadDialog
          onClose={() => setShowUploadDialog(false)}
          onUpload={async (files) => {
            await uploadFiles(files);
            setShowUploadDialog(false);
          }}
          uploading={uploading}
        />
      )}

      {/* Mobile FAB — primary upload surface on touch. The sidebar +
       * button needs three taps (menu → drawer → +); this is one.
       * Bottom-LEFT to match the Yorik FAB convention (right side is
       * reserved for VoiceFab). Admin-only mirrors the sidebar button. */}
      {(role === "admin" || role === "platform_admin") && (
        <button
          onClick={() => setShowUploadDialog(true)}
          disabled={uploading}
          aria-label="Upload documents"
          className="md:hidden fixed left-4 bottom-[calc(env(safe-area-inset-bottom)+5rem)] z-40 w-14 h-14 rounded-full bg-amber-500 hover:bg-amber-600 text-white shadow-lg shadow-amber-500/30 flex items-center justify-center transition disabled:opacity-60"
        >
          {uploading ? <Loader2 className="w-6 h-6 animate-spin" /> : <Upload className="w-6 h-6" />}
        </button>
      )}

      <Dock activeAppId="docs" />

      <style>{`
        .docs-bg {
          background-image:
            radial-gradient(circle at 30% 15%, hsl(38 90% 60% / 0.05), transparent 50%),
            radial-gradient(circle at 70% 85%, hsl(263 50% 60% / 0.04), transparent 50%);
        }
        @keyframes docs-drop-fade-in {
          from { opacity: 0; }
          to   { opacity: 1; }
        }
        @keyframes docs-drop-card-in {
          from { opacity: 0; transform: scale(0.9) translateY(10px); }
          to   { opacity: 1; transform: scale(1) translateY(0); }
        }
        @keyframes docs-drop-bounce {
          0%, 100% { transform: translateY(0); }
          50%      { transform: translateY(-8px); }
        }
        @keyframes docs-drop-ring {
          0%   { transform: scale(0.7); opacity: 0.75; }
          100% { transform: scale(1.6); opacity: 0; }
        }
        @keyframes docs-drop-frame-glow {
          0%, 100% { box-shadow: inset 0 0 0 0 hsl(38 90% 55% / 0.0); }
          50%      { box-shadow: inset 0 0 28px 2px hsl(38 90% 55% / 0.35); }
        }
        .docs-drop-fade   { animation: docs-drop-fade-in 180ms ease-out both; }
        .docs-drop-frame  { animation: docs-drop-fade-in 180ms ease-out both,
                                       docs-drop-frame-glow 1.8s ease-in-out infinite; }
        .docs-drop-card   { animation: docs-drop-card-in 260ms cubic-bezier(.2,.9,.3,1.2) both; }
        .docs-drop-bounce { animation: docs-drop-bounce 1.4s ease-in-out infinite; }
        .docs-drop-ring   { animation: docs-drop-ring 1.8s ease-out infinite; }
        .docs-drop-ring-delay { animation-delay: 0.9s; }
        @keyframes docs-search-progress {
          0%   { transform: translateX(-110%); }
          100% { transform: translateX(410%); }
        }
        .docs-search-progress {
          animation: docs-search-progress 1.1s cubic-bezier(.4,0,.2,1) infinite;
        }
        @media (prefers-reduced-motion: reduce) {
          .docs-drop-fade, .docs-drop-frame, .docs-drop-card,
          .docs-drop-bounce, .docs-drop-ring,
          .docs-search-progress { animation: none; }
        }
      `}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Document list
// ---------------------------------------------------------------------------

function DocumentList({
  docs, selectedId, loading, searchActive, onSelect, onVisibilityChanged,
}: {
  docs: YorikDocument[];
  selectedId: number | null;
  loading: boolean;
  searchActive: boolean;
  onSelect: (id: number) => void;
  onVisibilityChanged: () => void;
}) {
  return (
    <div className="flex-1 overflow-y-auto px-2 pb-2">
      {loading && docs.length === 0 && (
        <div className="px-2 space-y-3 pt-2">
          {[1,2,3,4,5].map(i => (
            <div key={i} className="flex gap-3 p-2 animate-pulse">
              <div className="w-10 h-12 rounded-md bg-muted/60 shrink-0" />
              <div className="flex-1 space-y-2 pt-1">
                <div className="h-3 bg-muted/60 rounded w-2/3" />
                <div className="h-3 bg-muted/40 rounded w-1/2" />
              </div>
            </div>
          ))}
        </div>
      )}
      {!loading && docs.length === 0 && (
        <div className="px-4 py-12 text-center text-xs text-muted-foreground">
          <FolderOpen className="w-8 h-8 mx-auto mb-3 opacity-30" />
          No documents yet.<br/>
          <span className="hidden md:inline">Drop a file anywhere — or use the + above.</span>
          <span className="md:hidden">Tap the upload button to add one.</span>
          <a
            href="/paperless/"
            target="_blank"
            rel="noopener noreferrer"
            className="mt-3 inline-flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-md bg-muted/50 hover:bg-muted text-muted-foreground hover:text-foreground transition"
          >
            <ExternalLink className="w-3 h-3" /> Open Paperless
          </a>
        </div>
      )}
      {docs.map(d => (
        <DocumentRow
          key={d.id}
          doc={d}
          active={!searchActive && selectedId === d.id}
          onClick={() => onSelect(d.id)}
          onVisibilityChanged={onVisibilityChanged}
        />
      ))}
    </div>
  );
}

function DocumentRow({ doc, active, onClick, onVisibilityChanged }:
  {
    doc: YorikDocument;
    active: boolean;
    onClick: () => void;
    onVisibilityChanged: () => void;
  }) {
  const Icon = pickDocIcon(doc.mime_type, doc.title);
  const { color, bg } = pickDocPalette(doc.mime_type, doc.title);
  const bucket = useDocBucket();
  const inBucket = bucket.has(doc.id);
  return (
    <div
      onClick={onClick}
      className={cn(
        "w-full text-left px-3 py-2.5 mb-0.5 rounded-lg flex items-start gap-3 transition group cursor-pointer relative",
        active ? "bg-sidebar-accent shadow-sm" : "hover:bg-sidebar-accent/50",
      )}
    >
      <div className={cn(
        "w-10 h-12 rounded-md flex items-center justify-center shrink-0 border",
        bg, color,
      )}>
        <Icon className="w-4 h-4" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm truncate font-medium" title={doc.title}>
          {doc.title}
        </div>
        {/* On desktop: hidden until row hover (avoids visual noise in the
         * resting list). On mobile: always visible — hover doesn't exist
         * on touch, and bookmarking IS the path to "chat about these". */}
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); bucket.toggle(doc.id); }}
          title={inBucket ? "Aus Bucket entfernen" : "In Bucket — dann „Chat about this“"}
          aria-label={inBucket ? "Remove from chat bucket" : "Add to chat bucket"}
          className={cn(
            "absolute right-2 top-2.5 p-2 md:p-1 rounded transition",
            inBucket
              ? "text-violet-500 opacity-100"
              : "text-muted-foreground opacity-100 md:opacity-0 md:group-hover:opacity-100 hover:text-violet-500",
          )}
        >
          {inBucket
            ? <BookmarkCheck className="w-4 h-4 md:w-3.5 md:h-3.5" />
            : <Bookmark className="w-4 h-4 md:w-3.5 md:h-3.5" />}
        </button>
        <div className="text-[11px] text-muted-foreground mt-0.5 flex items-center gap-1.5 flex-wrap">
          {doc.bytes > 0 && (
            <>
              <span>{fmtBytes(doc.bytes)}</span>
              <span className="w-0.5 h-0.5 rounded-full bg-current opacity-50" />
            </>
          )}
          {doc.source === "paperless"
            ? <span className="opacity-70">in Paperless</span>
            : <span>{doc.chunk_count} chunk{doc.chunk_count === 1 ? "" : "s"}</span>}
          {doc.source !== "paperless" && !doc.indexed_at && (
            <>
              <span className="w-0.5 h-0.5 rounded-full bg-current opacity-50" />
              <span className="text-amber-500 inline-flex items-center gap-1">
                <AlertCircle className="w-2.5 h-2.5" /> pending
              </span>
            </>
          )}
          {doc.source === "paperless" && doc.visibility && (
            <VisibilityChip
              doc={doc}
              onChanged={onVisibilityChanged}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Visibility chip ─────────────────────────────────────────────────
// Three styles for the three levels. Clicking the chip opens a tiny
// popover with the three options + a brief description so the user
// learns the model in one read instead of having to dig into docs.

const VISIBILITY_META: Record<DocVisibility, {
  label: string; emoji: string; color: string; desc: string;
}> = {
  private:  { label: "Private",  emoji: "🔒", color: "bg-red-500/15 text-red-500",
              desc: "Only you and admin see this." },
  business: { label: "Business", emoji: "💼", color: "bg-blue-500/15 text-blue-500",
              desc: "Visible to everyone in the business group." },
  shared:   { label: "Shared",   emoji: "👥", color: "bg-emerald-500/15 text-emerald-500",
              desc: "Visible to the whole household / team." },
};

function VisibilityChip({
  doc, onChanged,
}: {
  doc: YorikDocument;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const popRef = useRef<HTMLDivElement | null>(null);
  const current = doc.visibility || "private";
  const meta = VISIBILITY_META[current];

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (popRef.current && !popRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  async function change(v: DocVisibility) {
    if (v === current) { setOpen(false); return; }
    setBusy(true);
    try {
      const realId = Math.abs(doc.id);
      await api.post(`/api/documents/-${realId}/visibility`, { visibility: v });
      onChanged();
      setOpen(false);
    } catch (e: any) {
      alert(`Visibility change failed: ${e.message || e}`);
    } finally { setBusy(false); }
  }

  return (
    <div className="relative shrink-0" ref={popRef}>
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen(o => !o); }}
        className={cn(
          "px-2 py-1 md:px-1.5 md:py-0.5 rounded text-[11px] md:text-[9px] font-medium leading-none flex items-center gap-1 transition",
          meta.color,
          open && "ring-1 ring-foreground/30",
        )}
        title={`${meta.label} — ${meta.desc} (click to change)`}
      >
        <span>{meta.emoji}</span>
        <span className="uppercase tracking-wider">{meta.label}</span>
      </button>
      {open && (
        <div
          className="absolute z-30 right-0 top-full mt-1 w-56 rounded-md border border-border bg-popover shadow-lg p-1"
          onClick={(e) => e.stopPropagation()}
        >
          {(["private", "business", "shared"] as DocVisibility[]).map(v => {
            const m = VISIBILITY_META[v];
            return (
              <button
                key={v}
                onClick={() => change(v)}
                disabled={busy}
                className={cn(
                  "w-full text-left p-2 rounded hover:bg-muted/60 flex items-start gap-2 text-xs transition disabled:opacity-50",
                  current === v && "bg-muted/40",
                )}
              >
                <span className="text-sm leading-none mt-0.5">{m.emoji}</span>
                <div className="flex-1 min-w-0">
                  <div className="font-medium">{m.label}</div>
                  <div className="text-[10px] text-muted-foreground">{m.desc}</div>
                </div>
                {current === v && <Check className="w-3 h-3 text-foreground/60 shrink-0 mt-1" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Preview pane (center)
// ---------------------------------------------------------------------------

// Build the preview / download URL for a doc. Local docs hit Yorik's
// own /api/documents/{id}/raw; Paperless mirror rows tunnel through
// the Paperless reverse proxy which forwards to /api/documents/{id}/
// preview/ with the user's session cookie.
function rawUrl(doc: YorikDocument, role: string, download = false): string {
  if (doc.source === "paperless") {
    const id = Math.abs(doc.id);
    // /preview/ returns the inline-renderable file (PDF or image);
    // /download/ forces an attachment. Both are auth-checked by the
    // proxy via the Yorik session cookie.
    return `/paperless/api/documents/${id}/${download ? "download" : "preview"}/`;
  }
  return `/api/documents/${doc.id}/raw?role=${encodeURIComponent(role)}${download ? "&download=1" : ""}`;
}

function PreviewPane({ doc, role }: { doc: YorikDocument; role: string }) {
  return (
    <>
      <header className="h-16 px-4 md:px-6 border-b border-border flex items-center justify-between bg-background/80 backdrop-blur shrink-0 gap-2">
        <div className="flex items-center gap-3 min-w-0">
          <div className="hidden md:flex w-9 h-9 rounded-full bg-gradient-to-br from-amber-500/30 to-orange-500/30 items-center justify-center">
            <FileText className="w-4 h-4 text-amber-500" />
          </div>
          <div className="min-w-0">
            <div className="font-semibold truncate">{doc.title}</div>
            <div className="text-[11px] text-muted-foreground flex items-center gap-1.5 flex-wrap">
              <span className="truncate max-w-[60vw] md:max-w-none">{doc.mime_type || "unknown"}</span>
              {doc.bytes > 0 && <span className="opacity-60">· {fmtBytes(doc.bytes)}</span>}
              {doc.source === "paperless" && (
                <span className="px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-500 text-[10px] font-medium">
                  Paperless
                </span>
              )}
            </div>
          </div>
        </div>
        <a
          href={rawUrl(doc, role, true)}
          className="inline-flex items-center gap-1.5 text-sm md:text-xs h-10 md:h-auto px-4 md:px-3 md:py-1.5 rounded-md bg-amber-500/10 hover:bg-amber-500/20 text-amber-500 transition shrink-0"
        >
          <Download className="w-4 h-4 md:w-3.5 md:h-3.5" /> Download
        </a>
      </header>

      <div className="flex-1 overflow-hidden p-4">
        <PreviewBody doc={doc} role={role} />
      </div>
    </>
  );
}

function PreviewBody({ doc, role }: { doc: YorikDocument; role: string }) {
  const src = rawUrl(doc, role, false);
  const mime = (doc.mime_type || "").toLowerCase();
  const isPdf   = mime.includes("pdf");
  const isImage = mime.startsWith("image/");
  const isText  = mime.startsWith("text/") || mime.includes("json") || mime.includes("markdown");

  if (isPdf) {
    return (
      <iframe
        src={src}
        title={doc.title}
        className="w-full h-full rounded-xl bg-white border border-border shadow-inner"
      />
    );
  }
  if (isImage) {
    return (
      <div className="w-full h-full rounded-xl bg-muted/40 border border-border flex items-center justify-center overflow-auto p-4">
        <img
          src={src}
          alt={doc.title}
          className="max-w-full max-h-full object-contain rounded-lg shadow-md"
        />
      </div>
    );
  }
  if (isText) {
    return <TextPreview src={src} title={doc.title} />;
  }
  return (
    <div className="w-full h-full rounded-xl bg-muted/40 border border-border flex flex-col items-center justify-center gap-3 text-center px-6">
      <FileIcon className="w-14 h-14 text-muted-foreground/40" />
      <div className="text-sm text-muted-foreground">
        No inline preview for <code className="text-foreground/80">{mime || "this file type"}</code>.
      </div>
      <a
        href={rawUrl(doc, role, true)}
        className="inline-flex items-center gap-2 text-sm px-4 py-2 rounded-lg bg-amber-500 text-white hover:bg-amber-600 transition shadow-md"
      >
        <Download className="w-4 h-4" /> Download
      </a>
    </div>
  );
}

function TextPreview({ src, title }: { src: string; title: string }) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setText(null);
    setErr(null);
    fetch(src, { credentials: "include" })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then(t => { if (alive) setText(t); })
      .catch(e => { if (alive) setErr(e.message); });
    return () => { alive = false; };
  }, [src]);

  if (err) {
    return (
      <div className="w-full h-full rounded-xl bg-muted/40 border border-border flex items-center justify-center text-sm text-muted-foreground">
        Could not load preview: {err}
      </div>
    );
  }
  if (text === null) {
    return (
      <div className="w-full h-full rounded-xl bg-muted/40 border border-border flex items-center justify-center text-muted-foreground">
        <Loader2 className="w-5 h-5 animate-spin" />
      </div>
    );
  }
  return (
    <pre className="w-full h-full rounded-xl bg-card border border-border p-5 overflow-auto font-mono text-[12.5px] leading-relaxed whitespace-pre-wrap break-words" title={title}>
      {text}
    </pre>
  );
}

// ---------------------------------------------------------------------------
// Search results
// ---------------------------------------------------------------------------

/**
 * Per-category color for taxonomy folder cards. Keyed by Paperless
 * tag color (set by the autotagger from tag_taxonomy.yaml). Returns
 * Tailwind-ready bg + text classes — Tailwind needs full strings to
 * include them in the build, so the colors used are listed verbatim.
 */
function hexToTone(hex?: string | null): { bg: string; text: string; ring: string } {
  const fallback = { bg: "bg-amber-500/15", text: "text-amber-600", ring: "ring-amber-400/30" };
  if (!hex) return fallback;
  const map: Record<string, { bg: string; text: string; ring: string }> = {
    "#3b82f6": { bg: "bg-blue-500/15",    text: "text-blue-600",    ring: "ring-blue-400/30" },
    "#6366f1": { bg: "bg-indigo-500/15",  text: "text-indigo-600",  ring: "ring-indigo-400/30" },
    "#f59e0b": { bg: "bg-amber-500/15",   text: "text-amber-600",   ring: "ring-amber-400/30" },
    "#ef4444": { bg: "bg-red-500/15",     text: "text-red-600",     ring: "ring-red-400/30" },
    "#dc2626": { bg: "bg-red-600/15",     text: "text-red-700",     ring: "ring-red-500/30" },
    "#10b981": { bg: "bg-emerald-500/15", text: "text-emerald-600", ring: "ring-emerald-400/30" },
    "#64748b": { bg: "bg-slate-500/15",   text: "text-slate-600",   ring: "ring-slate-400/30" },
    "#8b5cf6": { bg: "bg-violet-500/15",  text: "text-violet-600",  ring: "ring-violet-400/30" },
    "#d946ef": { bg: "bg-fuchsia-500/15", text: "text-fuchsia-600", ring: "ring-fuchsia-400/30" },
    "#0ea5e9": { bg: "bg-sky-500/15",     text: "text-sky-600",     ring: "ring-sky-400/30" },
    "#71717a": { bg: "bg-zinc-500/15",    text: "text-zinc-600",    ring: "ring-zinc-400/30" },
  };
  return map[hex.toLowerCase()] || fallback;
}


/**
 * Slim icon nav for the left sidebar — switches the folder grid in the
 * center between facet types (Tags / Correspondents / Types / Years).
 * Counts shown next to each so empty groups read as "nothing here yet".
 */
function FacetTypeNav({
  active, counts, onPick,
}: {
  active: FacetKind;
  counts: Record<FacetKind, number>;
  onPick: (k: FacetKind) => void;
}) {
  const row = (kind: FacetKind, label: string, Icon: typeof TagIcon) => {
    const isActive = active === kind;
    const n = counts[kind] || 0;
    return (
      <button
        key={kind}
        onClick={() => onPick(kind)}
        className={cn(
          "w-full px-3 py-1.5 rounded-md text-xs font-medium flex items-center gap-2 transition",
          isActive
            ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
            : "hover:bg-muted text-foreground/80",
        )}
      >
        <Icon className="w-3.5 h-3.5 opacity-70" />
        <span className="flex-1 text-left">{label}</span>
        <span className="tabular-nums text-[10px] opacity-60">{n}</span>
      </button>
    );
  };
  return (
    <div className="px-2 py-2 space-y-0.5 border-b border-border/60">
      <div className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
        Browse by
      </div>
      {row("tag",           "Tags",           TagIcon)}
      {row("correspondent", "Correspondents", UserIcon2)}
      {row("document_type", "Types",          FilesIcon)}
      {row("year",          "Years",          CalIcon)}
    </div>
  );
}


/**
 * Compact scrollable list of folders for the currently-selected facet
 * type. Living in the left sidebar; clicking an item drives the center
 * pane's DocCardGrid. Highlights the active folder so the user always
 * knows where they are.
 */
function FacetSideList({
  kind, facets, loading, activeId, onPick,
}: {
  kind: FacetKind;
  facets: FacetsResponse | null;
  loading: boolean;
  activeId: number | null;
  onPick: (node: FacetNode) => void;
}) {
  const items: Array<{ id: number; name: string; count: number }> = (() => {
    if (!facets) return [];
    if (kind === "tag")            return (facets.tags || []).map(x => ({ id: x.id, name: x.name, count: x.document_count }));
    if (kind === "correspondent")  return (facets.correspondents || []).map(x => ({ id: x.id, name: x.name, count: x.document_count }));
    if (kind === "document_type")  return (facets.document_types || []).map(x => ({ id: x.id, name: x.name, count: x.document_count }));
    return (facets.years || []).map(x => ({ id: x.year, name: String(x.year), count: x.document_count }));
  })();

  if (loading && items.length === 0) {
    return (
      <div className="flex-1 px-4 py-4 text-center">
        <Loader2 className="w-4 h-4 animate-spin text-muted-foreground inline" />
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="flex-1 px-4 py-6 text-center text-[11px] text-muted-foreground leading-relaxed">
        No {labelForKind(kind).toLowerCase()}s yet.
        {kind === "tag" && (
          <> Run the autotagger from <strong>Settings → Embeddings</strong> to populate this list.</>
        )}
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-2 py-1 space-y-px">
      {items.map(it => {
        const isActive = activeId === it.id;
        return (
          <button
            key={`${kind}-${it.id}`}
            onClick={() => onPick({ kind, id: it.id, label: it.name, count: it.count })}
            className={cn(
              "w-full text-left px-3 py-1.5 rounded-md text-xs flex items-center gap-2 transition",
              isActive
                ? "bg-amber-500/15 text-amber-700 dark:text-amber-300 font-medium"
                : "hover:bg-muted text-foreground/85",
            )}
          >
            <span className="truncate flex-1">{it.name}</span>
            <span className="tabular-nums text-muted-foreground text-[10px]">{it.count}</span>
          </button>
        );
      })}
    </div>
  );
}


/**
 * Top-of-center breadcrumb. Renders "All ▸ <facet-kind> ▸ <facet-name>
 * ▸ <doc-title>"; each segment clickable to navigate up the hierarchy.
 * Shown in both the doc-card grid and the preview pane.
 */
function Breadcrumb({
  activeFacet, facetKind, docTitle, onAll, onFolder, total,
}: {
  activeFacet: FacetNode | null;
  facetKind: FacetKind;
  docTitle?: string | null;
  onAll: () => void;
  onFolder?: () => void;
  total?: number;
}) {
  // Three rendering modes:
  //   - No facet, no doc → "All documents · N"
  //   - Facet set, no doc → "All ▸ Tags: Rechnung"
  //   - Facet set, doc opened → "All ▸ Tags: Rechnung ▸ <doc title>"
  return (
    <div className="h-12 px-6 border-b border-border bg-background/85 backdrop-blur flex items-center gap-1.5 text-sm shrink-0 overflow-x-auto">
      {!activeFacet && !docTitle ? (
        <>
          <span className="text-foreground font-medium">All documents</span>
          {typeof total === "number" && (
            <span className="text-muted-foreground text-xs">· {total.toLocaleString()}</span>
          )}
        </>
      ) : (
        <>
          <button
            onClick={onAll}
            className="text-muted-foreground hover:text-foreground transition"
          >
            All
          </button>
          {activeFacet && (
            <>
              <ChevronRight className="w-3 h-3 text-muted-foreground/50" />
              <button
                onClick={onFolder || onAll}
                className={cn(
                  docTitle
                    ? "text-muted-foreground hover:text-foreground transition"
                    : "text-foreground font-medium",
                )}
              >
                {labelForKind(facetKind)}: {activeFacet.label}
              </button>
            </>
          )}
          {docTitle && (
            <>
              <ChevronRight className="w-3 h-3 text-muted-foreground/50" />
              <span className="text-foreground font-medium truncate">{docTitle}</span>
            </>
          )}
        </>
      )}
    </div>
  );
}


/**
 * The default browse view — grid of folder cards for the active facet
 * type. Tag cards inherit their taxonomy color (set by the autotagger
 * from tag_taxonomy.yaml) so the categories read at a glance. Other
 * facet kinds use a neutral tone.
 */
function FolderCardGrid({
  kind, facets, loading, onPick, canUpload, onUpload,
}: {
  kind: FacetKind;
  facets: FacetsResponse | null;
  loading: boolean;
  onPick: (node: FacetNode) => void;
  canUpload: boolean;
  onUpload: () => void;
}) {
  const items: Array<{ id: number; name: string; count: number; color?: string | null }> = (() => {
    if (!facets) return [];
    if (kind === "tag")           return (facets.tags || []).map(x => ({ id: x.id, name: x.name, count: x.document_count, color: x.color }));
    if (kind === "correspondent") return (facets.correspondents || []).map(x => ({ id: x.id, name: x.name, count: x.document_count }));
    if (kind === "document_type") return (facets.document_types || []).map(x => ({ id: x.id, name: x.name, count: x.document_count }));
    return (facets.years || []).map(x => ({ id: x.year, name: String(x.year), count: x.document_count }));
  })();

  return (
    <>
      <div className="h-12 px-6 border-b border-border bg-background/85 backdrop-blur flex items-center justify-between shrink-0">
        <div className="font-semibold text-sm flex items-center gap-2">
          <FolderOpen className="w-4 h-4 text-amber-500" />
          {labelForKind(kind)}s
          {items.length > 0 && (
            <span className="text-muted-foreground text-xs font-normal">· {items.length}</span>
          )}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-6">
        {loading && items.length === 0 && (
          <div className="py-16 flex justify-center text-muted-foreground">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
        )}
        {!loading && items.length === 0 && (
          <div className="py-16 text-center text-muted-foreground">
            <FolderOpen className="w-10 h-10 mx-auto mb-3 opacity-30" />
            <div className="font-medium text-foreground/80">No {labelForKind(kind).toLowerCase()}s yet</div>
            <div className="text-xs mt-1.5 max-w-md mx-auto leading-relaxed">
              {kind === "tag"
                ? "Tags appear after you run the autotagger from Settings → Embeddings, or once you add tags directly in Paperless."
                : `Paperless has no ${labelForKind(kind).toLowerCase()}s yet — they appear as you ingest documents with that metadata.`}
            </div>
            {canUpload && kind === "tag" && (
              <button
                onClick={onUpload}
                className="mt-4 inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-amber-500/10 hover:bg-amber-500/20 text-amber-700 dark:text-amber-300 transition"
              >
                <Upload className="w-3 h-3" /> Upload a document
              </button>
            )}
          </div>
        )}
        {items.length > 0 && (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
            {items.map(it => {
              const tone = kind === "tag" ? hexToTone(it.color) : { bg: "bg-card", text: "text-foreground", ring: "ring-border" };
              return (
                <button
                  key={`${kind}-${it.id}`}
                  onClick={() => onPick({ kind, id: it.id, label: it.name, count: it.count })}
                  className={cn(
                    "group relative aspect-[5/3] p-4 rounded-2xl ring-1 transition-all text-left",
                    "hover:-translate-y-0.5 hover:shadow-lg",
                    tone.bg, tone.ring,
                  )}
                >
                  <FolderOpen className={cn("w-7 h-7 mb-3", tone.text)} strokeWidth={2.25} />
                  <div className="font-semibold text-sm leading-snug line-clamp-2">{it.name}</div>
                  <div className="absolute bottom-3 left-4 text-[11px] text-muted-foreground tabular-nums">
                    {it.count} doc{it.count === 1 ? "" : "s"}
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </>
  );
}


/**
 * Card grid of documents for a chosen folder. Thumbnails come from
 * Paperless via the reverse proxy at /paperless/api/documents/{id}/thumb/
 * (loads lazily as cards scroll in); native uploads get a placeholder.
 * Paginated by the backend's page/page_size; pager at the bottom.
 */
function DocCardGrid({
  facet, facetKind, docs, page, total, hasNext, hasPrev,
  loading, onPickDoc, onPrev, onNext, onUp, role, canUpload, onUpload,
}: {
  facet: FacetNode | null;
  facetKind: FacetKind;
  docs: YorikDocument[];
  page: number; total: number; hasNext: boolean; hasPrev: boolean;
  loading: boolean;
  onPickDoc: (id: number) => void;
  onPrev: () => void;
  onNext: () => void;
  onUp: () => void;
  role: string;
  canUpload: boolean;
  onUpload: () => void;
}) {
  return (
    <>
      <Breadcrumb
        facetKind={facetKind}
        activeFacet={facet}
        onAll={onUp}
        total={facet ? undefined : total}
      />
      <div className="flex-1 overflow-y-auto p-6">
        {loading && docs.length === 0 && (
          <div className="py-16 flex justify-center text-muted-foreground">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
        )}
        {!loading && docs.length === 0 && (
          <div className="py-16 text-center text-muted-foreground">
            <FileIcon className="w-10 h-10 mx-auto mb-3 opacity-30" />
            <div>
              {facet
                ? `No documents in this ${labelForKind(facetKind).toLowerCase()}.`
                : "No documents in your library yet."}
            </div>
            <div className="mt-4 inline-flex items-center gap-2 flex-wrap justify-center">
              {!facet && canUpload && (
                <button
                  onClick={onUpload}
                  className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-amber-500/10 hover:bg-amber-500/20 text-amber-700 dark:text-amber-300 transition"
                >
                  <Upload className="w-3 h-3" /> Upload a document
                </button>
              )}
              <a
                href="/paperless/"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-muted/60 hover:bg-muted text-muted-foreground hover:text-foreground transition"
              >
                <ExternalLink className="w-3 h-3" /> Open Paperless
              </a>
            </div>
          </div>
        )}
        {docs.length > 0 && (
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-4">
            {docs.map(d => (
              <DocCard
                key={d.id}
                doc={d}
                role={role}
                onClick={() => onPickDoc(d.id)}
              />
            ))}
          </div>
        )}
      </div>
      {(hasPrev || hasNext) && (
        <div className="border-t border-border px-6 pt-3 pb-[max(5rem,calc(env(safe-area-inset-bottom)+1rem))] md:pb-20 flex items-center justify-between text-sm text-muted-foreground shrink-0 bg-background/40">
          <span className="tabular-nums text-xs">
            Page {page} · {total.toLocaleString()} doc{total === 1 ? "" : "s"}
            {facet ? " in this folder" : " total"}
          </span>
          <div className="flex items-center gap-1">
            <button
              onClick={onPrev}
              disabled={!hasPrev}
              className={cn(
                "px-3 py-1.5 rounded-md transition text-xs inline-flex items-center gap-1",
                hasPrev ? "hover:bg-muted text-foreground/80" : "opacity-30 cursor-not-allowed",
              )}
            >
              ‹ Prev
            </button>
            <button
              onClick={onNext}
              disabled={!hasNext}
              className={cn(
                "px-3 py-1.5 rounded-md transition text-xs inline-flex items-center gap-1",
                hasNext ? "hover:bg-muted text-foreground/80" : "opacity-30 cursor-not-allowed",
              )}
            >
              Next ›
            </button>
          </div>
        </div>
      )}
    </>
  );
}


/**
 * Single doc card with Paperless thumbnail. Native uploads (positive id)
 * fall back to a mime-based placeholder since Yorik doesn't render
 * thumbnails for those itself.
 */
function DocCard({
  doc, role, onClick,
}: {
  doc: YorikDocument;
  role: string;
  onClick: () => void;
}) {
  const isPaperless = doc.id < 0;
  const thumbUrl = isPaperless
    ? `/paperless/api/documents/${Math.abs(doc.id)}/thumb/`
    : null;
  const dateStr = (doc.created_at || "").slice(0, 10);
  return (
    <button
      onClick={onClick}
      className="group flex flex-col text-left rounded-xl bg-card border border-border hover:border-amber-500/40 hover:shadow-md transition overflow-hidden"
    >
      <div className="aspect-[3/4] bg-muted/40 overflow-hidden flex items-center justify-center relative">
        {thumbUrl ? (
          <img
            src={thumbUrl}
            alt=""
            loading="lazy"
            className="w-full h-full object-cover object-top group-hover:scale-[1.02] transition-transform"
            onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
          />
        ) : (
          <FileText className="w-10 h-10 text-muted-foreground/40" />
        )}
      </div>
      <div className="p-2.5 min-w-0">
        <div className="text-xs font-medium line-clamp-2 leading-snug">{doc.title}</div>
        {dateStr && (
          <div className="text-[10px] text-muted-foreground mt-1 tabular-nums">{dateStr}</div>
        )}
      </div>
    </button>
  );
}


/**
 * Pager strip for the left sidebar. Shown only when there's more than
 * one page of results (suppressed for tiny libraries where pagination
 * is just visual noise). Range label is computed client-side off the
 * authoritative `total` from the backend.
 */
function PaginationBar({
  page, pageSize, total, hasNext, hasPrev, onPrev, onNext,
}: {
  page: number; pageSize: number; total: number;
  hasNext: boolean; hasPrev: boolean;
  onPrev: () => void; onNext: () => void;
}) {
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end   = Math.min(page * pageSize, total);
  return (
    <div className="border-t border-border/60 px-3 py-2 flex items-center justify-between text-xs text-muted-foreground bg-background/40">
      <span className="tabular-nums">
        {start.toLocaleString()}–{end.toLocaleString()} of {total.toLocaleString()}
      </span>
      <div className="flex items-center gap-1">
        <button
          onClick={onPrev}
          disabled={!hasPrev}
          className={cn(
            "px-2 py-1 rounded-md transition",
            hasPrev ? "hover:bg-muted text-foreground/80" : "opacity-30 cursor-not-allowed",
          )}
          aria-label="Previous page"
        >
          ‹
        </button>
        <span className="tabular-nums px-1">{page}</span>
        <button
          onClick={onNext}
          disabled={!hasNext}
          className={cn(
            "px-2 py-1 rounded-md transition",
            hasNext ? "hover:bg-muted text-foreground/80" : "opacity-30 cursor-not-allowed",
          )}
          aria-label="Next page"
        >
          ›
        </button>
      </div>
    </div>
  );
}


/**
 * Folder-tree-style facet browser in the left sidebar. Four collapsible
 * groups — Tags · Correspondents · Types · Years — each with item counts
 * pulled from Paperless's own facet endpoints. Click a leaf to filter
 * the doc list below; click "All documents" or the X to clear.
 *
 * Why folders over a flat tag chip cloud: Yorik's audience is non-
 * technical households + small businesses; "folders" is the familiar
 * mental model for filing docs, even though the underlying storage is
 * faceted (one doc can carry many tags). The tree just renders the
 * existing Paperless metadata — no new storage, no migration.
 */
function BrowseTree({
  facets, loading, active, onPick, onClear,
}: {
  facets: FacetsResponse | null;
  loading: boolean;
  active: FacetNode | null;
  onPick: (node: FacetNode) => void;
  onClear: () => void;
}) {
  // Default: Tags expanded (the most useful after running the autotagger),
  // others collapsed. If something IS actively filtered, also expand that
  // group so the active item is visible without an extra click.
  const [open, setOpen] = useState<Record<FacetKind, boolean>>({
    tag: true, correspondent: active?.kind === "correspondent",
    document_type: active?.kind === "document_type", year: active?.kind === "year",
  });

  useEffect(() => {
    if (active) setOpen(o => ({ ...o, [active.kind]: true }));
  }, [active?.kind, active?.id]);

  const hasAny =
    (facets?.tags?.length ?? 0) > 0 ||
    (facets?.correspondents?.length ?? 0) > 0 ||
    (facets?.document_types?.length ?? 0) > 0 ||
    (facets?.years?.length ?? 0) > 0;

  const groupRow = (
    kind: FacetKind, label: string,
    Icon: typeof TagIcon,
    items: Array<{ id: number; name: string; document_count: number } | { year: number; document_count: number }>,
  ) => {
    const isOpen = !!open[kind];
    if (!items?.length) return null;
    return (
      <div className="select-none">
        <button
          onClick={() => setOpen(o => ({ ...o, [kind]: !o[kind] }))}
          className="w-full flex items-center gap-1.5 px-2 py-1 rounded-md hover:bg-muted/60 text-xs font-medium text-muted-foreground transition"
        >
          <ChevronRight className={cn("w-3 h-3 transition-transform", isOpen && "rotate-90")} />
          <Icon className="w-3 h-3 opacity-70" />
          <span className="flex-1 text-left">{label}</span>
          <span className="tabular-nums opacity-60">{items.length}</span>
        </button>
        {isOpen && (
          <ul className="pl-6 py-0.5 space-y-px">
            {items.map((it: any) => {
              const itemId    = it.id ?? it.year;
              const itemLabel = it.name ?? String(it.year);
              const isActive = active?.kind === kind && active?.id === itemId;
              return (
                <li key={`${kind}-${itemId}`}>
                  <button
                    onClick={() => onPick({
                      kind, id: itemId, label: itemLabel, count: it.document_count,
                    })}
                    className={cn(
                      "w-full text-left px-2 py-1 rounded-md text-xs flex items-center gap-2 transition",
                      isActive
                        ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
                        : "hover:bg-muted/60 text-foreground/85",
                    )}
                  >
                    <span className="truncate flex-1">{itemLabel}</span>
                    <span className="tabular-nums text-muted-foreground text-[10px]">
                      {it.document_count}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    );
  };

  return (
    <div className="px-2 pt-1 pb-2 border-b border-border/60 max-h-[40vh] overflow-y-auto">
      {active ? (
        <div className="mx-1 mb-1.5 inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-amber-500/15 text-[11px] text-amber-700 dark:text-amber-300 font-medium">
          <span>{labelForKind(active.kind)}: {active.label}</span>
          <button onClick={onClear} title="Clear filter" className="opacity-70 hover:opacity-100">
            <X className="w-3 h-3" />
          </button>
        </div>
      ) : (
        <div className="px-2 py-1 flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Browse</span>
          {loading && <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />}
        </div>
      )}
      <div className="space-y-0.5">
        {groupRow("tag",           "Tags",           TagIcon,    facets?.tags || [])}
        {groupRow("correspondent", "Correspondents", UserIcon2,  facets?.correspondents || [])}
        {groupRow("document_type", "Types",          FilesIcon,  facets?.document_types || [])}
        {groupRow("year",          "Years",          CalIcon,    facets?.years || [])}
      </div>
      {!loading && !hasAny && (
        <p className="px-2 py-2 text-[11px] text-muted-foreground leading-relaxed">
          No tags or correspondents in Paperless yet. Run the autotagger from{" "}
          <strong>Settings → Embeddings</strong> to populate this tree.
        </p>
      )}
    </div>
  );
}

function labelForKind(k: FacetKind): string {
  switch (k) {
    case "tag":           return "Tag";
    case "correspondent": return "From";
    case "document_type": return "Type";
    case "year":          return "Year";
  }
}


/**
 * Per-engine status row shown above search results. One pill per engine
 * (semantic + keyword) showing whether it fired, how many hits it
 * returned, and an inline reason when it didn't. Hover/tap the pill for
 * the full error message — keeps the row visually quiet when both legs
 * are healthy, but always tells the user WHY a leg didn't run.
 */
function SearchEngineBadges({
  legs, searching,
}: {
  legs: SearchResponse["legs"];
  searching: boolean;
}) {
  if (searching) return null;
  const sem = legs?.semantic;
  const fts = legs?.fts;
  if (!sem && !fts) return null;

  const pill = (
    label: string,
    Icon: typeof Sparkles,
    status: SearchLegStatus | undefined,
  ) => {
    if (!status) return null;
    const isError = !!status.error;
    const isEmpty = !isError && status.count === 0;
    const tone = isError
      ? "bg-amber-500/10 text-amber-600 border-amber-500/30"
      : isEmpty
        ? "bg-muted text-muted-foreground border-border"
        : "bg-emerald-500/10 text-emerald-600 border-emerald-500/30";
    const StatusIcon = isError ? AlertCircle : isEmpty ? Info : CheckCircle2;
    return (
      <div
        className={cn(
          "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[11px] font-medium",
          tone,
        )}
        title={status.error || (isEmpty ? "Ran successfully but returned 0 hits" : `${status.count} hits`)}
      >
        <Icon className="w-3 h-3 opacity-70" />
        <span>{label}</span>
        <span className="opacity-60">·</span>
        <span>{isError ? "unavailable" : `${status.count} hit${status.count === 1 ? "" : "s"}`}</span>
        <StatusIcon className="w-3 h-3 opacity-70" />
      </div>
    );
  };

  // Surface the first error message in a one-line strip below the pills
  // — title-tooltips don't read on mobile, and a failed leg is the kind
  // of thing the user wants to ACT on, not just hover over.
  const firstError = sem?.error || fts?.error;
  const failingLeg = sem?.error ? "Semantic" : fts?.error ? "Keyword" : null;

  return (
    <div className="px-6 py-2 border-b border-border bg-background/60">
      <div className="flex items-center gap-2 flex-wrap">
        {pill("Semantic", Sparkles, sem)}
        {pill("Keyword", Type, fts)}
      </div>
      {firstError && failingLeg && (
        <div className="mt-1.5 text-[11px] text-amber-700/90 dark:text-amber-300/90 flex items-start gap-1.5">
          <AlertCircle className="w-3 h-3 mt-0.5 shrink-0" />
          <span>
            <strong>{failingLeg} search:</strong> {firstError}
          </span>
        </div>
      )}
    </div>
  );
}


function SearchResultsPane({
  hits, legs, searching, query, onPickDoc, onClear,
}: {
  hits: DocumentSearchHit[];
  legs: SearchResponse["legs"];
  searching: boolean;
  query: string;
  onPickDoc: (docId: number) => void;
  onClear: () => void;
}) {
  // Group hits by document so the user sees one card per file with the
  // best-scoring chunk on top — matches semantic-search UX from Notion,
  // Obsidian, etc. Hits arrive sorted best-first by the backend, so the
  // first chunk we see for a given doc is also its best.
  const grouped = useMemo(() => {
    const map = new Map<number, DocumentSearchHit[]>();
    for (const h of hits) {
      const list = map.get(h.doc_id);
      if (list) list.push(h);
      else map.set(h.doc_id, [h]);
    }
    return Array.from(map.values());
  }, [hits]);

  const docCount = grouped.length;
  const passageCount = hits.length;

  return (
    <>
      <header className="h-16 px-6 border-b border-border flex items-center justify-between bg-background/80 backdrop-blur shrink-0">
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-9 h-9 rounded-full bg-violet-500/15 flex items-center justify-center">
            <Sparkles className="w-4 h-4 text-violet-500" />
          </div>
          <div className="min-w-0">
            <div className="font-semibold truncate">
              {searching
                ? "Searching…"
                : passageCount === 0
                  ? "No matches"
                  : `${passageCount} passage${passageCount === 1 ? "" : "s"} across ${docCount} document${docCount === 1 ? "" : "s"}`}
            </div>
            <div className="text-[11px] text-muted-foreground truncate">
              for "{query}"
            </div>
          </div>
        </div>
        <button
          onClick={onClear}
          className="inline-flex items-center gap-1 text-xs px-3 py-1.5 rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition"
        >
          <X className="w-3 h-3" /> Clear
        </button>
      </header>
      <SearchEngineBadges legs={legs} searching={searching} />
      <div className="flex-1 overflow-y-auto p-6 space-y-3">
        {searching && hits.length === 0 && (
          <div className="flex items-center justify-center py-16 text-muted-foreground">
            <Loader2 className="w-5 h-5 animate-spin" />
          </div>
        )}
        {!searching && hits.length === 0 && (
          <div className="text-center py-16 text-muted-foreground">
            <Search className="w-10 h-10 mx-auto mb-3 opacity-30" />
            <div>No matches for <strong className="text-foreground/80">{query}</strong>.</div>
            <div className="text-xs mt-1">Try paraphrasing — search is semantic.</div>
          </div>
        )}
        {grouped.map(group => (
          <DocSearchGroup
            key={group[0].doc_id}
            hits={group}
            onPickDoc={onPickDoc}
          />
        ))}
      </div>
    </>
  );
}

function DocSearchGroup({ hits, onPickDoc }:
  { hits: DocumentSearchHit[]; onPickDoc: (docId: number) => void }) {
  const [expanded, setExpanded] = useState(false);
  const best = hits[0];
  const extras = hits.slice(1);

  return (
    <div className="bg-card border border-border rounded-xl overflow-hidden hover:border-amber-500/40 hover:shadow-md transition">
      <button
        onClick={() => onPickDoc(best.doc_id)}
        className="w-full text-left p-4 group"
      >
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <div className="flex items-center gap-2 min-w-0">
            <FileText className="w-3.5 h-3.5 text-amber-500 shrink-0" />
            <span className="font-medium text-sm truncate">{best.doc_title}</span>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <span className="text-[10px] text-muted-foreground">chunk {best.chunk_index}</span>
            <span
              className="text-[10px] px-1.5 py-0.5 rounded-full bg-violet-500/10 text-violet-500 font-medium"
              title="Relative match score across both search engines — 1.00 is the top hit"
            >
              {(best.match_score ?? Math.max(0, 1 - best.distance)).toFixed(2)} match
            </span>
          </div>
        </div>
        <div className="text-sm text-muted-foreground leading-relaxed line-clamp-4">
          {best.chunk_text}
        </div>
      </button>
      {extras.length > 0 && (
        <div className="border-t border-border/60">
          <button
            onClick={() => setExpanded(v => !v)}
            className="w-full px-4 py-2.5 md:py-2 text-xs md:text-[11px] text-muted-foreground hover:text-foreground hover:bg-muted/40 transition text-left flex items-center gap-1.5"
          >
            <span className={cn("inline-block w-2 transition-transform", expanded && "rotate-90")}>›</span>
            {expanded
              ? `Hide ${extras.length} more passage${extras.length === 1 ? "" : "s"}`
              : `Show ${extras.length} more passage${extras.length === 1 ? "" : "s"} from this document`}
          </button>
          {expanded && (
            <div className="px-4 pb-3 space-y-2.5">
              {extras.map(h => (
                <button
                  key={h.chunk_id}
                  onClick={() => onPickDoc(h.doc_id)}
                  className="w-full text-left bg-muted/30 hover:bg-muted/50 border border-border/40 rounded-lg p-3 transition"
                >
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="text-[10px] text-muted-foreground">chunk {h.chunk_index}</span>
                    <span
                      className="text-[10px] px-1.5 py-0.5 rounded-full bg-violet-500/10 text-violet-500 font-medium"
                      title="Relative match score across both search engines — 1.00 is the top hit"
                    >
                      {(h.match_score ?? Math.max(0, 1 - h.distance)).toFixed(2)} match
                    </span>
                  </div>
                  <div className="text-xs text-muted-foreground leading-relaxed line-clamp-3">
                    {h.chunk_text}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Metadata + actions pane (right)
// ---------------------------------------------------------------------------

function MetadataPane({
  doc, role, onReindexed,
}: {
  doc: YorikDocument;
  role: string;
  onReindexed: () => void;
}) {
  const [reindexing, setReindexing] = useState(false);

  async function reindex() {
    setReindexing(true);
    try {
      await api.post(`/api/documents/${doc.id}/reindex?role=${encodeURIComponent(role)}`);
      onReindexed();
    } catch (e: any) {
      alert("Reindex failed: " + e.message);
    } finally {
      setReindexing(false);
    }
  }

  // Delete intentionally lives only in the Paperless UI (Open in Paperless
  // below). Yorik's local-table delete endpoint at /api/documents/{id}
  // never matched Paperless doc ids (negative-prefix), AND a member-level
  // delete via the Paperless API risks bulk-deletion of ownerless docs
  // until owner backfill lands. Until then, sending users to the
  // Paperless UI keeps the destructive action behind Paperless's own
  // ownership check.

  const indexed = !!doc.indexed_at;
  const paperlessUrl = paperlessUrlFromHost();

  return (
    <>
      <header className="h-16 px-5 flex items-center gap-2 border-b border-border">
        <div className="w-8 h-8 rounded-full bg-amber-500/15 flex items-center justify-center">
          <FileText className="w-4 h-4 text-amber-500" />
        </div>
        <div>
          <div className="font-semibold leading-none text-sm">Details</div>
          <div className="text-[10px] text-muted-foreground uppercase tracking-wider mt-0.5">
            File · index · access
          </div>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-5 space-y-5">
        <Section label="File">
          <Row label="Type">
            <span className="font-mono text-[11px] bg-muted px-1.5 py-0.5 rounded">
              {doc.mime_type || "unknown"}
            </span>
          </Row>
          <Row label="Size">{fmtBytes(doc.bytes)}</Row>
          <Row label="Uploaded">{formatDate(doc.created_at)}</Row>
        </Section>

        <Section label="Index">
          <Row label="Status">
            {indexed
              ? <span className="text-emerald-500 inline-flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" /> Indexed
                </span>
              : <span className="text-amber-500 inline-flex items-center gap-1">
                  <AlertCircle className="w-3 h-3" /> Pending
                </span>
            }
          </Row>
          <Row label="Chunks">{doc.chunk_count}</Row>
          {indexed && <Row label="Indexed at">{formatDate(doc.indexed_at!)}</Row>}
        </Section>

        <Section label="Access">
          <Row label="Visible to">
            <div className="flex flex-wrap gap-1">
              {(doc.allowed_roles || "").split(",").map(r => r.trim()).filter(Boolean).map(r => (
                <span key={r} className="text-[11px] px-2 py-0.5 rounded-full bg-muted text-foreground/80">
                  {r}
                </span>
              ))}
            </div>
          </Row>
          {doc.tags && (
            <Row label="Tags">{doc.tags}</Row>
          )}
        </Section>

        <div className="pt-1">
          <a
            href={`/api/documents/${doc.id}/raw?role=${encodeURIComponent(role)}&download=1`}
            className="w-full inline-flex items-center justify-center gap-2 px-3 py-2.5 md:py-2 rounded-lg bg-amber-500 hover:bg-amber-600 text-white text-sm font-medium shadow-sm transition"
          >
            <Download className="w-4 h-4" /> Download
          </a>
          <button
            onClick={() => sendDocViaEmail(doc, role)}
            className="w-full mt-2 inline-flex items-center justify-center gap-2 px-3 py-2.5 md:py-2 rounded-lg bg-muted hover:bg-muted/70 text-foreground text-sm transition"
            title="Open the email composer with this document pre-attached"
          >
            <Mail className="w-4 h-4" /> Send via email
          </button>
          <button
            onClick={reindex}
            disabled={reindexing}
            className="w-full mt-2 inline-flex items-center justify-center gap-2 px-3 py-2.5 md:py-2 rounded-lg bg-muted hover:bg-muted/70 text-foreground text-sm transition disabled:opacity-50"
          >
            {reindexing
              ? <><Loader2 className="w-4 h-4 animate-spin" /> Reindexing…</>
              : <><RefreshCw className="w-4 h-4" /> Reindex</>}
          </button>
          {paperlessUrl && (
            <a
              href={paperlessUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="w-full mt-2 inline-flex items-center justify-center gap-2 px-3 py-2.5 md:py-2 rounded-lg bg-muted/60 hover:bg-muted text-muted-foreground hover:text-foreground text-sm transition"
              title="Power-user features: tags, correspondents, bulk operations — and deletion"
            >
              <ExternalLink className="w-4 h-4" /> Open in Paperless
            </a>
          )}
        </div>

        <div className="pt-3 mt-3 border-t border-border text-[11px] text-muted-foreground leading-relaxed">
          Ask Yorik about this document in the <strong className="text-foreground/70">chat</strong> —
          the answer will cite it.
        </div>
      </div>
    </>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section>
      <h4 className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold mb-2">
        {label}
      </h4>
      <div className="space-y-1.5">{children}</div>
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  // On mobile, stack label above value — long mime types
  // (application/vnd.openxmlformats-…) would otherwise crash into the label.
  return (
    <div className="flex flex-col items-start sm:flex-row sm:items-baseline sm:justify-between gap-0.5 sm:gap-2 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-foreground sm:text-right min-w-0 break-words sm:break-normal">{children}</span>
    </div>
  );
}

function EmptyMetadata() {
  return (
    <div className="flex-1 flex items-center justify-center text-center text-xs text-muted-foreground p-8">
      <div>
        <FileText className="w-8 h-8 mx-auto mb-3 opacity-30" />
        Select a document to see its details, or run a search.
      </div>
    </div>
  );
}

function EmptyPane({ onUpload, canUpload }: { onUpload: () => void; canUpload: boolean }) {
  const paperlessUrl = paperlessUrlFromHost();
  return (
    <div className="flex-1 flex items-center justify-center px-6">
      <div className="text-center max-w-md">
        <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-amber-500/30 to-orange-500/30 flex items-center justify-center mx-auto mb-5">
          <FolderOpen className="w-8 h-8 text-amber-500" />
        </div>
        <div className="font-semibold text-lg">Your filing cabinet</div>
        <div className="text-sm text-muted-foreground mt-2">
          PDFs, contracts, invoices, lab results, manuals — anything you'd want to find again
          by asking. Yorik indexes them locally so the chat can cite them.
          <span className="hidden md:inline"> Drop a file anywhere on this page.</span>
        </div>
        {canUpload && (
          <div className="mt-6 flex flex-col items-center gap-3">
            <button
              onClick={onUpload}
              className="inline-flex items-center gap-2 px-4 py-2 rounded-full bg-amber-500 hover:bg-amber-600 text-white text-sm shadow-md transition"
            >
              <Upload className="w-4 h-4" /> Upload a document
            </button>
            {paperlessUrl && (
              <a
                href={paperlessUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-muted-foreground hover:text-foreground inline-flex items-center gap-1 transition"
              >
                Or open Paperless to bulk-import <ExternalLink className="w-3 h-3" />
              </a>
            )}
          </div>
        )}
        <div className="mt-6 text-[11px] text-muted-foreground italic">
          Tip — PDFs sent to you on WhatsApp file themselves automatically.
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Upload dialog
// ---------------------------------------------------------------------------

function UploadDialog({
  onClose, onUpload, uploading,
}: {
  onClose: () => void;
  onUpload: (files: File[]) => Promise<void> | void;
  uploading: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [picked, setPicked] = useState<File[]>([]);

  useEffect(() => {
    function esc(e: KeyboardEvent) { if (e.key === "Escape" && !uploading) onClose(); }
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose, uploading]);

  return (
    <div
      className="fixed inset-0 z-[1000] bg-black/60 backdrop-blur-sm flex items-center justify-center p-6"
      onClick={() => { if (!uploading) onClose(); }}
    >
      <div
        className="bg-card border border-border rounded-2xl shadow-2xl w-full max-w-lg overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <header className="px-5 py-4 border-b border-border flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Upload className="w-4 h-4 text-amber-500" />
            <span className="font-semibold">Upload documents</span>
          </div>
          <button
            onClick={onClose}
            disabled={uploading}
            aria-label="Close"
            className="w-10 h-10 md:w-7 md:h-7 rounded-md hover:bg-muted text-muted-foreground flex items-center justify-center disabled:opacity-50"
          >
            <X className="w-5 h-5 md:w-4 md:h-4" />
          </button>
        </header>
        <div className="p-5">
          <button
            onClick={() => inputRef.current?.click()}
            disabled={uploading}
            className="w-full border-2 border-dashed border-border hover:border-amber-500/50 rounded-xl py-10 px-6 text-center transition disabled:opacity-50"
          >
            <Upload className="w-10 h-10 text-muted-foreground/60 mx-auto mb-2" />
            <div className="text-sm font-medium">
              <span className="md:hidden">Tap to choose files</span>
              <span className="hidden md:inline">Click to choose files</span>
            </div>
            <div className="hidden md:block text-xs text-muted-foreground mt-1">
              or drop them anywhere on this page
            </div>
          </button>
          <input
            ref={inputRef}
            type="file"
            multiple
            className="hidden"
            onChange={e => {
              const files = Array.from(e.target.files || []);
              if (files.length) setPicked(files);
            }}
          />
          {picked.length > 0 && (
            <div className="mt-4 space-y-1">
              {picked.map((f, i) => (
                <div key={i} className="text-xs flex items-center justify-between bg-muted/50 rounded-md px-3 py-1.5">
                  <span className="truncate">{f.name}</span>
                  <span className="text-muted-foreground shrink-0 ml-2">{fmtBytes(f.size)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
        <footer className="px-5 py-3 border-t border-border flex items-center justify-end gap-2 bg-muted/20">
          <button
            onClick={onClose}
            disabled={uploading}
            className="h-10 md:h-auto px-4 md:px-3 md:py-1.5 text-sm md:text-xs rounded-md hover:bg-muted text-muted-foreground hover:text-foreground transition disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={() => onUpload(picked)}
            disabled={uploading || picked.length === 0}
            className={cn(
              "h-10 md:h-auto px-4 md:px-3 md:py-1.5 text-sm md:text-xs rounded-md font-medium transition inline-flex items-center gap-1.5",
              picked.length > 0 && !uploading
                ? "bg-amber-500 text-white hover:bg-amber-600 shadow-sm"
                : "bg-muted text-muted-foreground cursor-not-allowed",
            )}
          >
            {uploading
              ? <><Loader2 className="w-4 h-4 md:w-3.5 md:h-3.5 animate-spin" /> Uploading…</>
              : <>Upload {picked.length || ""}</>}
          </button>
        </footer>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtBytes(n: number): string {
  if (!n && n !== 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso.replace(" ", "T") + (iso.includes("T") ? "" : "Z"));
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) +
         " · " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function pickDocIcon(mime?: string | null, title?: string) {
  const m = (mime || "").toLowerCase();
  if (m.startsWith("image/")) return FileImage;
  if (m.includes("pdf")) return FileText;
  if (m.startsWith("text/") || m.includes("json") || m.includes("markdown") || m.includes("xml")) return FileCode;
  if (m.includes("word") || m.includes("officedocument")) return FileText;
  // Title-based fallback
  const ext = ((title || "").split(".").pop() || "").toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp"].includes(ext)) return FileImage;
  if (["pdf"].includes(ext)) return FileText;
  if (["md", "txt", "json", "yml", "yaml"].includes(ext)) return FileCode;
  return FileIcon;
}

function pickDocPalette(mime?: string | null, title?: string): { color: string; bg: string } {
  const m = (mime || "").toLowerCase();
  const ext = ((title || "").split(".").pop() || "").toLowerCase();
  if (m.includes("pdf") || ext === "pdf") {
    return { color: "text-red-500", bg: "bg-red-500/5 border-red-500/20" };
  }
  if (m.startsWith("image/") || ["png", "jpg", "jpeg", "gif", "webp"].includes(ext)) {
    return { color: "text-emerald-500", bg: "bg-emerald-500/5 border-emerald-500/20" };
  }
  if (m.includes("markdown") || ext === "md") {
    return { color: "text-violet-500", bg: "bg-violet-500/5 border-violet-500/20" };
  }
  if (m.startsWith("text/") || m.includes("json") || ["txt", "json", "yml", "yaml"].includes(ext)) {
    return { color: "text-blue-500", bg: "bg-blue-500/5 border-blue-500/20" };
  }
  if (m.includes("word") || m.includes("officedocument") || ["doc", "docx"].includes(ext)) {
    return { color: "text-sky-500", bg: "bg-sky-500/5 border-sky-500/20" };
  }
  return { color: "text-muted-foreground", bg: "bg-muted/40 border-border" };
}

function paperlessUrlFromHost(): string | null {
  // Same-origin via Yorik's reverse proxy (backend/paperless_proxy.py).
  // The proxy authenticates via the Yorik session cookie and injects
  // `Remote-User` so Paperless logs the user in automatically — no
  // second password prompt, no cross-port cookie surgery.
  return "/paperless/";
}

function sendDocViaEmail(doc: YorikDocument, role: string) {
  // Stash a pointer to the doc — the Email app reads yorik_pending_email
  // on mount, the Composer fetches the URL and MIME-attaches it on send.
  // Reuses rawUrl() so Paperless-mirrored docs (negative-id, served by
  // the /paperless/* proxy) work the same as locally-uploaded ones.
  const mime = doc.mime_type || "application/octet-stream";
  // doc.title is the user-facing name; add an extension if missing so
  // recipient mail clients pick the right preview icon.
  const titleHasExt = /\.[A-Za-z0-9]{2,5}$/.test(doc.title || "");
  const ext =
    !titleHasExt && mime === "application/pdf"        ? ".pdf"
    : !titleHasExt && mime.startsWith("image/jpeg")   ? ".jpg"
    : !titleHasExt && mime.startsWith("image/png")    ? ".png"
    : !titleHasExt && mime.startsWith("text/plain")   ? ".txt"
    : "";
  const filename = (doc.title || `document-${doc.id}`) + ext;
  try {
    sessionStorage.setItem("yorik_pending_email", JSON.stringify({
      attachments: [{
        url:      rawUrl(doc, role, true),
        filename,
        mimetype: mime,
      }],
    }));
  } catch {}
  window.location.href = "/r/email";
}
