/** The glyph for a drafted document's kind (invoice, email, memo,
 *  letter), used on the chat's draft and template cards. */
import { Receipt, Mail, StickyNote, FileText } from "lucide-react";
import { cn } from "@/lib/utils";

export function DocKindIcon({ kind, className }: { kind?: string; className?: string }) {
  const Icon = kind === "invoice" || kind === "offer" ? Receipt
    : kind === "email" ? Mail
    : kind === "memo" ? StickyNote
    : FileText;
  return <Icon className={cn("w-4 h-4 text-muted-foreground shrink-0", className)} />;
}
