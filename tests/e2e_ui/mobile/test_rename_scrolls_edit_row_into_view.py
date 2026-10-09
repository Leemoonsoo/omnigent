"""iOS sidebar rename must keep the edited row visible above the soft keyboard.

Reported journey: open the iOS app -> open the sidebar drawer -> long-press a
session row in the lower half of the screen -> Rename. The inline edit field
focuses and the soft keyboard rises. The drawer pads its bottom by the keyboard
inset, so the list's visible area shrinks to the region above the keyboard; the
field being typed into must be scrolled into that region.

Driven on the web lane with the same iOS bridge stub and WebKit-faithful
``visualViewport`` keyboard simulation as
``test_rename_keeps_sidebar_sessions_reachable.py``. The iOS shell disables
document scrolling while the app owns keyboard layout, so WebKit's native
focus reveal does not move the inner list; the app must scroll it.
"""

from __future__ import annotations

import os

from playwright.sync_api import Browser, expect

from tests.e2e_ui.mobile.test_rename_keeps_sidebar_sessions_reachable import (
    _FAKE_VISUAL_VIEWPORT,
    _FILLER_COUNT,
    _IOS_SHELL_INIT_SCRIPT,
    _KEYBOARD_HEIGHT,
    _LIST_SELECTOR,
    _VIEWPORT,
    _list_metrics,
    _long_press,
    _seed_filler_sessions,
)

# Distinct from the sibling test's titles so the two can share one server.
_TITLE_PREFIX = "Keyboard rename row"


def _edit_geometry(page) -> dict:
    return page.evaluate(
        f"""
        () => {{
          const input = document.querySelector('[data-testid="rename-conversation-input"]');
          const save = document.querySelector('button[aria-label="Save rename"]');
          const list = document.querySelector('{_LIST_SELECTOR}');
          const ir = input.getBoundingClientRect();
          const sr = save.getBoundingClientRect();
          const lr = list.getBoundingClientRect();
          const hit = document.elementFromPoint(sr.x + sr.width / 2, sr.y + sr.height / 2);
          return {{
            inputTop: ir.top, inputBottom: ir.bottom,
            listTop: lr.top, listBottom: lr.bottom,
            listScrollTop: list.scrollTop,
            keyboardTop: window.visualViewport.height,
            focused: document.activeElement === input,
            saveUncovered: !!hit && save.contains(hit),
          }};
        }}
        """
    )


def test_rename_scrolls_edit_row_into_view_above_keyboard(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """The focused rename field must be visible once the keyboard is up.

    :param browser: Playwright browser to open a touch phone context on.
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    _seed_filler_sessions(base_url, _FILLER_COUNT, title_prefix=_TITLE_PREFIX)

    ctx_kwargs: dict = {"viewport": _VIEWPORT, "has_touch": True, "is_mobile": True}
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir

    context = browser.new_context(**ctx_kwargs)
    try:
        page = context.new_page()
        page.add_init_script(_IOS_SHELL_INIT_SCRIPT)
        page.add_init_script(_FAKE_VISUAL_VIEWPORT)
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.locator('textarea[aria-label="Message the agent"]')).to_be_visible(
            timeout=60_000
        )
        expect(page.locator(".app-shell")).to_have_attribute("data-ios-native", "true")

        page.locator('button[aria-label="Open sidebar"]').click()
        expect(page.get_by_text(f"{_TITLE_PREFIX} 00", exact=False)).to_be_visible(timeout=10_000)
        page.wait_for_function(
            f"() => document.querySelector('{_LIST_SELECTOR}').getBoundingClientRect().x > -1"
        )
        page.wait_for_timeout(300)
        page.evaluate(f"() => document.querySelector('{_LIST_SELECTOR}').scrollTo(0, 0)")
        page.wait_for_timeout(200)

        metrics = _list_metrics(page)
        keyboard_top = _VIEWPORT["height"] - _KEYBOARD_HEIGHT
        print(f"[rename-into-view] list metrics after open: {metrics}")

        # A fully visible row that the keyboard will cover once it rises.
        box = None
        row_label = None
        for handle in page.locator('aside[aria-label="Conversations"] a[href^="/c/"]').all():
            b = handle.bounding_box()
            text = (handle.inner_text() or "").strip()
            if (
                b
                and _TITLE_PREFIX in text
                and b["y"] > keyboard_top + 40
                and b["y"] + b["height"] < metrics["y"] + metrics["h"] - 4
            ):
                box = b
                row_label = text
                break
        assert box is not None, "no visible row below the future keyboard top"
        print(f"[rename-into-view] long-pressing row {row_label!r} at {box}")

        cdp = context.new_cdp_session(page)
        _long_press(cdp, page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        expect(page.locator('[role="menu"][data-state="open"]')).to_be_visible(timeout=5_000)
        page.get_by_test_id("rename-conversation").tap()
        edit = page.get_by_test_id("rename-conversation-input")
        expect(edit).to_be_visible(timeout=5_000)
        expect(edit).to_be_focused()
        before = _edit_geometry(page)
        print(f"[rename-into-view] edit focused, keyboard down: {before}")

        # The focused field raises the soft keyboard.
        page.evaluate(f"() => window.__setKeyboardHeight({_KEYBOARD_HEIGHT})")
        page.wait_for_timeout(400)
        after = _edit_geometry(page)
        print(f"[rename-into-view] keyboard up: {after}")

        assert after["focused"], f"rename field lost focus: {after}"
        visible_bottom = min(after["listBottom"], after["keyboardTop"])
        assert after["inputTop"] >= after["listTop"] - 1, (
            f"rename field scrolled above the list's visible area: {after}"
        )
        assert after["inputBottom"] <= visible_bottom + 1, (
            "while renaming with the soft keyboard up, the focused rename field "
            f"sits at y={after['inputTop']:.0f}-{after['inputBottom']:.0f}, below "
            f"the list's visible bottom ({visible_bottom:.0f}px). The list did not "
            f"scroll it into view (scrollTop {before['listScrollTop']} -> "
            f"{after['listScrollTop']})."
        )
        assert after["saveUncovered"], (
            f"the rename field's Save control is covered by other drawer chrome: {after}"
        )
    finally:
        context.close()
