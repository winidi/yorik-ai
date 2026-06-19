/**
 * Markdown renderer for assistant messages.
 *
 * Only used on the assistant side — user messages render as plain
 * text (the user typed `**` literally, they didn't mean bold).
 *
 * Subset enabled: GFM (tables, strikethrough, autolinks, task lists)
 * via remark-gfm. No raw HTML passes through; react-markdown's
 * default behaviour drops anything that isn't markdown — good for
 * safety, and the LLM rarely emits raw HTML anyway.
 *
 * Styling is inlined here so the chat bubble's parent doesn't need
 * a global `.prose` class. Keeps the visual identical across the
 * main chat bubble and the Compose side-pane bubble.
 */

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

export function AssistantMarkdown({
  children,
  className,
}: {
  children: string;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2 break-words", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Paragraphs — slim default margin so consecutive lines
          // don't get cavernous gaps inside the bubble.
          p: ({ children }) => (
            <p className="leading-relaxed">{children}</p>
          ),
          // Lists — tighter than browser defaults.
          ul: ({ children }) => (
            <ul className="list-disc pl-5 space-y-1">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="list-decimal pl-5 space-y-1">{children}</ol>
          ),
          li: ({ children }) => (
            <li className="leading-relaxed">{children}</li>
          ),
          // Strong / em — let the LLM's emphasis come through.
          strong: ({ children }) => (
            <strong className="font-semibold">{children}</strong>
          ),
          em: ({ children }) => <em className="italic">{children}</em>,
          // Headings inside chat bubbles read as bigger, bolder lines —
          // not full document headings.
          h1: ({ children }) => (
            <div className="text-base font-semibold mt-1">{children}</div>
          ),
          h2: ({ children }) => (
            <div className="text-base font-semibold mt-1">{children}</div>
          ),
          h3: ({ children }) => (
            <div className="text-sm font-semibold mt-1">{children}</div>
          ),
          // Links open in a new tab; the chat is a single-page app and
          // we don't want assistant suggestions to nav the user away.
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-violet-600 dark:text-violet-400 underline underline-offset-2 hover:opacity-80"
            >
              {children}
            </a>
          ),
          // Inline code + code blocks.
          code: ({ className, children, ...rest }) => {
            const isBlock = /language-/.test(className || "");
            if (isBlock) {
              return (
                <pre className="bg-muted/60 border border-border rounded-md p-2 overflow-x-auto text-[12px] my-1">
                  <code className={className} {...rest}>{children}</code>
                </pre>
              );
            }
            return (
              <code className="bg-muted/60 rounded px-1 py-0.5 text-[0.85em] font-mono">
                {children}
              </code>
            );
          },
          // Blockquote — a soft left border, no fancy background.
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-muted-foreground/30 pl-2.5 text-muted-foreground italic">
              {children}
            </blockquote>
          ),
          // Tables — horizontal scroll on overflow so a wide table
          // doesn't break the bubble's max-width.
          table: ({ children }) => (
            <div className="overflow-x-auto -mx-1">
              <table className="text-xs border-collapse">
                {children}
              </table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border border-border px-2 py-1 text-left font-semibold bg-muted/40">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-border px-2 py-1 align-top">
              {children}
            </td>
          ),
          // Horizontal rule — slim.
          hr: () => <hr className="my-2 border-border" />,
          // Suppress markdown images in assistant content. Photos
          // surfaced by find_photo are rendered by PhotoResultGrid
          // from message.photos; documents from search_documents by
          // DocumentResultCard from message.documents. The LLM
          // occasionally also enumerates them inline as
          // ![alt](url) — that doubles every result on screen. There's
          // no legitimate case where an assistant message should embed
          // a raw markdown image, so hide them all.
          img: () => null,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
