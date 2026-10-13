"""E2E: a link in the Markdown editor shows a popup with an Open action.

In edit mode a plain click on a link places the caret instead of following it.
The popup under the caret is the visible way to open the link: a web link opens
in a new page while the app stays on its session, and a workspace-relative link
opens in the FileViewer. Seeded via the filesystem PUT endpoint (no agent run);
the linked site is a routed stub, so the test never reaches the network.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

_MARKDOWN_FILE_PATH = "editor_link_popup.md"
_LINKED_FILE_PATH = "editor_link_popup_plan.md"

_LINK_URL = "https://tracker.external.example/browse/ES-1"

# The web link sits in a table cell, as in the original report.
_MARKDOWN_CONTENT = f"""\
# Capacity requests

| Cheat sheet |
| ----------- |
| **Example ticket.** [ES-1]({_LINK_URL}), raising the quota |

Read [the plan]({_LINKED_FILE_PATH}) first.
"""

_LINKED_FILE_CONTENT = """\
# Rollout plan

Steps for the rollout.
"""


@pytest.fixture
def seeded_link_popup_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    base_url, session_id = seeded_session
    filesystem_url = (
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/filesystem"
    )
    seeded = {
        _MARKDOWN_FILE_PATH: _MARKDOWN_CONTENT,
        _LINKED_FILE_PATH: _LINKED_FILE_CONTENT,
    }
    for path, content in seeded.items():
        resp = httpx.put(
            f"{filesystem_url}/{path}",
            json={"content": content, "encoding": "utf-8"},
            timeout=10.0,
        )
        resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        # Deleting the session keeps its workspace files, so remove the seeds here.
        for path in seeded:
            httpx.delete(f"{filesystem_url}/{path}", timeout=10.0)


def _fulfill_external_page(route: Route) -> None:
    route.fulfill(
        status=200,
        content_type="text/html",
        body="<title>ES-1</title><h1>ES-1</h1>",
    )


def test_editor_link_popup_opens_web_link_in_new_page(
    page: Page,
    seeded_link_popup_session: tuple[str, str],
) -> None:
    """Clicking a web link shows the popup; its Open opens a new page."""
    base_url, session_id = seeded_link_popup_session
    page.context.route("https://tracker.external.example/**", _fulfill_external_page)
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    link = editor.get_by_role("link", name="ES-1")
    expect(link).to_be_visible()

    link.click()
    popup = page.locator("[data-link-bubble]")
    expect(popup).to_be_visible()
    expect(popup).to_contain_text(_LINK_URL)
    open_link = popup.get_by_role("link", name="Open")
    expect(open_link).to_have_attribute("href", _LINK_URL)

    with page.context.expect_page(timeout=10_000) as new_page_info:
        open_link.click()
    new_page = new_page_info.value
    new_page.wait_for_load_state()
    expect(new_page).to_have_url(_LINK_URL)
    expect(new_page.get_by_role("heading", name="ES-1")).to_be_visible()

    expect(page).to_have_url(re.compile(rf"/c/{session_id}"))
    expect(editor).to_be_visible()


def test_editor_link_popup_opens_relative_link_in_file_viewer(
    page: Page,
    seeded_link_popup_session: tuple[str, str],
) -> None:
    """Open on a workspace-relative link shows that file in the FileViewer."""
    base_url, session_id = seeded_link_popup_session
    page.goto(f"{base_url}/c/{session_id}?file={_MARKDOWN_FILE_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)

    editor.get_by_role("link", name="the plan").click()
    popup = page.locator("[data-link-bubble]")
    expect(popup).to_contain_text(_LINKED_FILE_PATH)
    popup.get_by_role("button", name="Open").click()

    expect(page).to_have_url(re.compile(rf"file={re.escape(_LINKED_FILE_PATH)}"))
    expect(file_viewer.get_by_text("Steps for the rollout.")).to_be_visible(timeout=10_000)
