// Link popup for the Markdown editor. In edit mode a plain click on a link
// places the caret (the Link extension runs with openOnClick:false), so while
// the caret rests in a link this popup shows its target and an Open action —
// otherwise the only way to follow it is an undiscoverable ⌘/Ctrl-click.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { getMarkRange } from "@tiptap/core";
import type { Editor } from "@tiptap/react";
import { ExternalLinkIcon } from "lucide-react";
import { getEmbedRoot } from "@/lib/host";
import { ExternalPreviewLink } from "./ExternalPreviewLink";
import { useFileViewer } from "./FileViewerContext";
import { isWorkspaceRelativeSrc, resolveWorkspacePath } from "./TipTapWorkspaceImage";

interface LinkBubble {
  href: string;
  top: number;
  left: number;
}

// Minimum gap between the popup and the viewport's right edge.
const VIEWPORT_MARGIN_PX = 8;

const OPEN_ACTION_CLASS_NAME =
  "flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 font-medium text-foreground hover:bg-muted transition-colors";

/**
 * The link under a collapsed caret, positioned below the link's start (or
 * below the caret when the link wraps onto another line). Null when the
 * selection is a range — that shows the Add comment button instead — or when
 * the link has scrolled out of `container`.
 */
function linkBubbleAt(editor: Editor, container: HTMLElement | null): LinkBubble | null {
  if (!editor.isEditable || !editor.isFocused) return null;
  const { state, view } = editor;
  const { selection } = state;
  const linkType = state.schema.marks.link;
  if (!selection.empty || !linkType) return null;
  const { $from } = selection;
  const mark = [...($from.nodeAfter?.marks ?? []), ...($from.nodeBefore?.marks ?? [])].find(
    (m) => m.type === linkType,
  );
  const href = typeof mark?.attrs.href === "string" ? mark.attrs.href : "";
  // An in-page `#` link has nothing to open: editor headings carry no ids.
  if (!mark || !href || href.startsWith("#")) return null;
  const range = getMarkRange($from, linkType, mark.attrs);
  let caret: { left: number; top: number; bottom: number };
  let start: { left: number; top: number; bottom: number };
  try {
    caret = view.coordsAtPos(selection.from);
    start = range ? view.coordsAtPos(range.from) : caret;
  } catch {
    return null;
  }
  const anchor = Math.abs(start.top - caret.top) < 1 ? start : caret;
  const bounds = container?.getBoundingClientRect();
  if (bounds && (anchor.bottom < bounds.top || anchor.top > bounds.bottom)) return null;
  return { href, top: anchor.bottom + 4, left: anchor.left };
}

export function MarkdownLinkBubble({
  editor,
  path,
  scrollContainerRef,
}: {
  editor: Editor;
  /** Workspace path of the open file; relative links resolve against it. */
  path: string;
  scrollContainerRef: React.RefObject<HTMLDivElement | null>;
}) {
  const openFile = useFileViewer();
  const [bubble, setBubble] = useState<LinkBubble | null>(null);
  const popupRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const update = () => setBubble(linkBubbleAt(editor, scrollContainerRef.current));
    const hide = () => setBubble(null);
    editor.on("selectionUpdate", update);
    editor.on("focus", update);
    editor.on("blur", hide);
    const scrollEl = scrollContainerRef.current;
    scrollEl?.addEventListener("scroll", update);
    window.addEventListener("resize", update);
    return () => {
      editor.off("selectionUpdate", update);
      editor.off("focus", update);
      editor.off("blur", hide);
      scrollEl?.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [editor, scrollContainerRef]);

  // The FileViewer is usually the rightmost panel, so a link near its edge
  // would push the popup off-screen: shift it left until it fits.
  useLayoutEffect(() => {
    const el = popupRef.current;
    if (!el || !bubble) return;
    const overflow = el.getBoundingClientRect().right - (window.innerWidth - VIEWPORT_MARGIN_PX);
    if (overflow > 0) {
      el.style.left = `${Math.max(VIEWPORT_MARGIN_PX, bubble.left - overflow)}px`;
    }
  }, [bubble]);

  if (!bubble) return null;
  const { href } = bubble;
  const filePath = isWorkspaceRelativeSrc(href) ? resolveWorkspacePath(path, href) : null;
  // A workspace-relative link opens in the FileViewer, so it needs one.
  if (filePath !== null && (!filePath || !openFile)) return null;

  return createPortal(
    <div
      ref={popupRef}
      data-link-bubble
      // Keep the editor focused so the caret, and this popup, stay in the link.
      onMouseDown={(e) => e.preventDefault()}
      className="fixed z-50 flex max-w-[min(28rem,calc(100vw-1rem))] items-center gap-2 rounded-lg border border-border bg-popover py-1 pr-1 pl-2.5 text-sm shadow-menu"
      style={{ top: bubble.top, left: bubble.left }}
    >
      <span className="min-w-0 truncate text-muted-foreground" title={href}>
        {href}
      </span>
      {filePath === null ? (
        <ExternalPreviewLink href={href} className={OPEN_ACTION_CLASS_NAME}>
          <ExternalLinkIcon className="size-3.5" />
          Open
        </ExternalPreviewLink>
      ) : (
        <button
          type="button"
          className={OPEN_ACTION_CLASS_NAME}
          onClick={() => openFile?.(filePath)}
        >
          <ExternalLinkIcon className="size-3.5" />
          Open
        </button>
      )}
    </div>,
    getEmbedRoot() ?? document.body,
  );
}
