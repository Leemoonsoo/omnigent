// Tests for MarkdownLinkBubble — the Open popup shown while the editor caret
// rests in a link. A real headless TipTap editor drives the selection; jsdom
// has no layout, so Range rects are stubbed to give coordsAtPos a position.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { Link } from "@tiptap/extension-link";
import { Markdown } from "@tiptap/markdown";
import { StarterKit } from "@tiptap/starter-kit";
import { FileViewerContext } from "./FileViewerContext";
import { MarkdownLinkBubble } from "./MarkdownLinkBubble";

const CONTENT =
  "See [ES-1](https://tracker.example.com/browse/ES-1), " +
  "[the plan](../notes/plan.md#steps), and [top](#top).";

const RECT = { left: 120, right: 160, top: 40, bottom: 56, width: 40, height: 16, x: 120, y: 40 };

const openFile = vi.fn();
const FILE_VIEWER = {
  openFile,
  openGithubTab: () => {},
  isChangedPath: () => false,
  conversationId: "conv_1",
  workspaceRoot: "/ws",
  workspaceHome: null,
};

let editor: Editor | null = null;
let editorElement: HTMLElement | null = null;

beforeEach(() => {
  openFile.mockClear();
  // jsdom implements neither method; coordsAtPos reads one of them.
  Range.prototype.getClientRects = () => [RECT] as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => RECT as DOMRect;
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  editor?.destroy();
  editorElement?.remove();
  editor = null;
  editorElement = null;
});

function renderBubble({ editable = true, container = null as HTMLDivElement | null } = {}) {
  editorElement = document.createElement("div");
  document.body.appendChild(editorElement);
  editor = new Editor({
    element: editorElement,
    editable,
    extensions: [
      StarterKit.configure({ link: false }),
      Link.configure({ openOnClick: false, autolink: false }),
      Markdown,
    ],
    content: CONTENT,
    contentType: "markdown",
  });
  render(
    <FileViewerContext.Provider value={FILE_VIEWER}>
      <MarkdownLinkBubble
        editor={editor}
        path="docs/guide.md"
        scrollContainerRef={{ current: container }}
      />
    </FileViewerContext.Provider>,
  );
  return editor;
}

/** Position `offset` characters into the first text node containing `text`. */
function posInText(ed: Editor, text: string, offset = 1): number {
  let found = -1;
  ed.state.doc.descendants((node, pos) => {
    if (found === -1 && node.isText && node.text?.includes(text)) {
      found = pos + node.text.indexOf(text) + offset;
    }
  });
  if (found === -1) throw new Error(`text not found: ${text}`);
  return found;
}

function placeCaret(ed: Editor, pos: number, to = pos) {
  act(() => {
    // view.focus() directly: the focus command defers to requestAnimationFrame.
    ed.view.focus();
    ed.commands.setTextSelection({ from: pos, to });
  });
}

const bubble = () => document.querySelector("[data-link-bubble]");

describe("MarkdownLinkBubble", () => {
  it("shows a web link's URL and an Open link that leaves the app window", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).not.toBeNull();
    expect(screen.getByText("https://tracker.example.com/browse/ES-1")).toBeDefined();
    const open = screen.getByRole("link", { name: "Open" });
    expect(open.getAttribute("href")).toBe("https://tracker.example.com/browse/ES-1");
    expect(open.getAttribute("target")).toBe("_blank");
    expect(open.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("sits below the link", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1", 2));
    const el = bubble() as HTMLElement;
    expect(el.style.top).toBe(`${RECT.bottom + 4}px`);
    expect(el.style.left).toBe(`${RECT.left}px`);
  });

  it("shifts left to stay inside the viewport", () => {
    vi.spyOn(window, "innerWidth", "get").mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
      this: HTMLElement,
    ) {
      return (
        this.hasAttribute("data-link-bubble") ? { left: RECT.left, right: RECT.left + 340 } : {}
      ) as DOMRect;
    });
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    // right edge 460 overflows 400 - 8 by 68px.
    expect((bubble() as HTMLElement).style.left).toBe(`${RECT.left - 68}px`);
  });

  it("shows while the caret is at the end of the link", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1", "ES-1".length));
    expect(screen.getByRole("link", { name: "Open" })).toBeDefined();
  });

  it("opens a workspace-relative link in the FileViewer", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "the plan"));
    fireEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(openFile).toHaveBeenCalledWith("notes/plan.md");
  });

  it("does not show outside a link", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "See"));
    expect(bubble()).toBeNull();
  });

  it("does not show for a range selection, which offers Add comment instead", () => {
    const ed = renderBubble();
    const from = posInText(ed, "ES-1", 0);
    placeCaret(ed, from, from + 3);
    expect(bubble()).toBeNull();
  });

  it("does not show for an in-page # link", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "top"));
    expect(bubble()).toBeNull();
  });

  it("hides when the link is outside the scroll container", () => {
    const container = document.createElement("div");
    container.getBoundingClientRect = () =>
      ({ top: 200, bottom: 600, left: 0, right: 800 }) as DOMRect;
    const ed = renderBubble({ container });
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).toBeNull();
  });

  it("hides when the editor loses focus", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).not.toBeNull();
    act(() => {
      (ed.view.dom as HTMLElement).blur();
    });
    expect(bubble()).toBeNull();
  });

  it("does not show in a read-only editor, where a plain click follows the link", () => {
    const ed = renderBubble({ editable: false });
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).toBeNull();
  });

  it("keeps the editor focused when the popup is pressed", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    const pressed = fireEvent.mouseDown(bubble() as HTMLElement);
    expect(pressed).toBe(false);
  });
});
