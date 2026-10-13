// Tests for MarkdownLinkBubble — the Open popup shown while the editor caret
// rests in a link. A real headless TipTap editor drives the selection; jsdom
// has no layout, so Range rects are stubbed to give coordsAtPos a position.

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
// The editor's scroll viewport, which comfortably contains RECT.
const VIEWPORT_RECT = { left: 0, right: 800, top: 0, bottom: 600 };

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
let containerElement: HTMLElement | null = null;

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
  containerElement?.remove();
  editor = null;
  containerElement = null;
});

function renderBubble({ editable = true, containerRect = VIEWPORT_RECT } = {}) {
  const container = document.createElement("div");
  container.getBoundingClientRect = () => containerRect as DOMRect;
  const editorElement = document.createElement("div");
  container.appendChild(editorElement);
  document.body.appendChild(container);
  containerElement = container;
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

  it("sits below the caret when the link wraps onto another line", () => {
    const line2 = { ...RECT, left: 24, right: 64, top: 60, bottom: 76, x: 24, y: 60 };
    // The link starts on line 1; from its third character on, it is on line 2.
    const rectFor = (range: Range) =>
      range.startContainer.textContent === "ES-1" && range.startOffset >= 2 ? line2 : RECT;
    Range.prototype.getClientRects = function (this: Range) {
      return [rectFor(this)] as unknown as DOMRectList;
    };
    Range.prototype.getBoundingClientRect = function (this: Range) {
      return rectFor(this) as DOMRect;
    };
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1", 3));
    const el = bubble() as HTMLElement;
    expect(el.style.top).toBe(`${line2.bottom + 4}px`);
    expect(el.style.left).toBe(`${line2.left}px`);
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
    const ed = renderBubble({ containerRect: { ...VIEWPORT_RECT, top: 200 } });
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).toBeNull();
  });

  it("hides when a nested scroller, like a wide table's, scrolls the link out of view", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    expect(bubble()).not.toBeNull();
    const scroller = ed.view.dom.querySelector("p") as HTMLElement;
    scroller.style.overflowX = "auto";
    scroller.getBoundingClientRect = () => ({ ...VIEWPORT_RECT, left: 200 }) as DOMRect;
    act(() => {
      // Scroll events don't bubble.
      scroller.dispatchEvent(new Event("scroll"));
    });
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

  it("moves focus to Open on Alt+Enter, where Enter activates it", async () => {
    const user = userEvent.setup();
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "the plan"));
    await user.keyboard("{Alt>}{Enter}{/Alt}");
    const open = screen.getByRole("button", { name: "Open" });
    expect(document.activeElement).toBe(open);
    expect(open.getAttribute("aria-keyshortcuts")).toBe("Alt+Enter");
    await user.keyboard("{Enter}");
    expect(openFile).toHaveBeenCalledWith("notes/plan.md");
  });

  it("returns focus to the editor on Escape and stays open", async () => {
    const user = userEvent.setup();
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    await user.keyboard("{Alt>}{Enter}{/Alt}");
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Open" }));
    await user.keyboard("{Escape}");
    expect(document.activeElement).toBe(ed.view.dom);
    expect(bubble()).not.toBeNull();
  });

  it("hides when focus leaves both the editor and the popup", async () => {
    const user = userEvent.setup();
    const ed = renderBubble();
    const elsewhere = document.createElement("button");
    containerElement?.after(elsewhere);
    placeCaret(ed, posInText(ed, "ES-1"));
    await user.keyboard("{Alt>}{Enter}{/Alt}");
    expect(bubble()).not.toBeNull();
    act(() => {
      elsewhere.focus();
    });
    expect(bubble()).toBeNull();
    elsewhere.remove();
  });

  it("keeps the editor focused when the popup is pressed", () => {
    const ed = renderBubble();
    placeCaret(ed, posInText(ed, "ES-1"));
    const pressed = fireEvent.mouseDown(bubble() as HTMLElement);
    expect(pressed).toBe(false);
  });
});
