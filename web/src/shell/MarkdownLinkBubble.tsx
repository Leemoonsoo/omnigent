// Link popup for the Markdown editor. In edit mode a plain click on a link
// places the caret (the Link extension runs with openOnClick:false), so while
// the caret rests in a link this popup shows its target and an Open action —
// otherwise the only way to follow it is an undiscoverable ⌘/Ctrl-click.
// Alt+Enter in the editor moves focus to Open; Escape returns to the editor.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { getMarkRange } from "@tiptap/core";
import type { Editor } from "@tiptap/react";
import { ExternalLinkIcon } from "lucide-react";
import { ALT_KEY } from "@/components/KeyboardShortcut";
import { getEmbedRoot } from "@/lib/host";
import { ExternalPreviewLink } from "./ExternalPreviewLink";
import { useFileViewer } from "./FileViewerContext";
import { isWorkspaceRelativeSrc, resolveWorkspacePath } from "./TipTapWorkspaceImage";

interface Point {
  left: number;
  top: number;
  bottom: number;
}

interface LinkBubble {
  href: string;
  top: number;
  left: number;
}

// Minimum gap between the popup and the viewport's right edge.
const VIEWPORT_MARGIN_PX = 8;

const OPEN_ACTION_TITLE = `Open link (${ALT_KEY}+Enter from the editor)`;

const OPEN_ACTION_CLASS_NAME =
  "flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 font-medium text-foreground hover:bg-muted transition-colors";

function isOutside(point: Point, el: Element): boolean {
  const box = el.getBoundingClientRect();
  return (
    point.left < box.left ||
    point.left > box.right ||
    point.bottom < box.top ||
    point.top > box.bottom
  );
}

/**
 * Whether `point` is scrolled out of view: outside `container`, the editor's
 * scroll viewport, or outside a scrolling element between it and `node`, such
 * as a wide table's horizontal scroller.
 */
function isClipped(point: Point, node: Node, container: HTMLElement | null): boolean {
  for (
    let el = node instanceof Element ? node : node.parentElement;
    el && el !== container;
    el = el.parentElement
  ) {
    const { overflowX, overflowY } = getComputedStyle(el);
    if ((overflowX !== "visible" || overflowY !== "visible") && isOutside(point, el)) return true;
  }
  return container !== null && isOutside(point, container);
}

/**
 * The link under a collapsed caret, positioned below the link's start, or
 * below the caret when the link wraps or its start is scrolled out of view.
 * Null when the selection is a range — that shows the Add comment button
 * instead — or when the link is scrolled out of view.
 */
function linkBubbleAt(
  editor: Editor,
  container: HTMLElement | null,
  hasFocus: boolean,
): LinkBubble | null {
  if (!editor.isEditable || !hasFocus) return null;
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
  let caret: Point;
  let start: Point;
  let node: Node;
  try {
    caret = view.coordsAtPos(selection.from);
    start = range ? view.coordsAtPos(range.from) : caret;
    node = view.domAtPos(selection.from).node;
  } catch {
    return null;
  }
  const candidates = Math.abs(start.top - caret.top) < 1 ? [start, caret] : [caret];
  const anchor = candidates.find((point) => !isClipped(point, node, container));
  if (!anchor) return null;
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
    const popupHasFocus = () => popupRef.current?.contains(document.activeElement) ?? false;
    const update = () =>
      setBubble(
        linkBubbleAt(editor, scrollContainerRef.current, editor.isFocused || popupHasFocus()),
      );
    // Focus moving from the editor into the popup keeps it open.
    const onBlur = (props?: { event?: FocusEvent }) => {
      const next = (props?.event?.relatedTarget ?? null) as Node | null;
      if (!popupRef.current?.contains(next)) setBubble(null);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      const altEnter =
        event.key === "Enter" &&
        event.altKey &&
        !event.ctrlKey &&
        !event.metaKey &&
        !event.shiftKey;
      if (!altEnter || !editor.view.dom.contains(event.target as Node)) return;
      const open = popupRef.current?.querySelector<HTMLElement>("[data-link-bubble-open]");
      if (!open) return;
      event.preventDefault();
      open.focus();
    };
    editor.on("selectionUpdate", update);
    editor.on("focus", update);
    editor.on("blur", onBlur);
    const scrollEl = scrollContainerRef.current;
    // Keydown bubbles up from the editor. Scroll doesn't, so capture it to
    // also see nested scrollers such as a wide table's.
    scrollEl?.addEventListener("keydown", onKeyDown);
    scrollEl?.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      editor.off("selectionUpdate", update);
      editor.off("focus", update);
      editor.off("blur", onBlur);
      scrollEl?.removeEventListener("keydown", onKeyDown);
      scrollEl?.removeEventListener("scroll", update, true);
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
      onBlur={(e) => {
        const next = e.relatedTarget;
        if (!popupRef.current?.contains(next) && !editor.view.dom.contains(next)) setBubble(null);
      }}
      onKeyDown={(e) => {
        if (e.key !== "Escape") return;
        e.preventDefault();
        editor.view.focus();
      }}
      className="fixed z-50 flex max-w-[min(28rem,calc(100vw-1rem))] items-center gap-2 rounded-lg border border-border bg-popover py-1 pr-1 pl-2.5 text-sm shadow-menu"
      style={{ top: bubble.top, left: bubble.left }}
    >
      <span className="min-w-0 truncate text-muted-foreground" title={href}>
        {href}
      </span>
      {filePath === null ? (
        <ExternalPreviewLink
          href={href}
          data-link-bubble-open
          title={OPEN_ACTION_TITLE}
          aria-keyshortcuts="Alt+Enter"
          className={OPEN_ACTION_CLASS_NAME}
        >
          <ExternalLinkIcon className="size-3.5" />
          Open
        </ExternalPreviewLink>
      ) : (
        <button
          type="button"
          data-link-bubble-open
          title={OPEN_ACTION_TITLE}
          aria-keyshortcuts="Alt+Enter"
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
