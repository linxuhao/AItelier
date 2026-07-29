# tests/unit/test_checkpoint_modal_selection.py
# Regression: the Approve / Request Changes selection must be reachable in the
# CLI TUI checkpoint modal, whatever the length of the reviewed step output.
#
# 2026-07-29, project `greet` at dpe_default_v2 step 2 ("Architecture Review"):
# ~400 Down presses moved the content pane but never the selection cursor, so
# "Request Changes" could not be chosen at all. Cause: Textual's
# ScrollableContainer is can_focus=True and owns up/down bindings; the modal's
# content pane took focus on mount and swallowed the arrow keys, so
# CheckpointModal.action_cursor_down never ran (for SHORT output Textual
# disables the scroll binding, the key bubbles up, and it appeared to work).

import asyncio

import pytest
from textual.app import App

from cli.tui.chat import CheckpointModal

# Long enough that the content pane is genuinely scrollable at test size.
LONG_OUTPUT = "\n".join(f"line {i}: " + "x" * 60 for i in range(400))


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "checkpoint": "2_review",
            "step_output": {"files": {"design.md": LONG_OUTPUT}},
        }


class _FakeHTTP:
    async def get(self, url, timeout=None):
        return _FakeResponse()

    async def post(self, url, json=None, timeout=None):
        return _FakeResponse()


class _Harness(App):
    """Minimal host app — CheckpointModal only needs `app.http`."""

    def __init__(self):
        super().__init__()
        self.http = _FakeHTTP()


async def _open_modal(pilot, app):
    modal = CheckpointModal("http://testserver", "greet", "Architecture Review", "2")
    await app.push_screen(modal)
    content = modal.query_one("#cp-content")
    # Wait for the fetch worker to mount the step output.
    for _ in range(40):
        await pilot.pause()
        if content.children:
            break
        await asyncio.sleep(0.05)
    assert content.max_scroll_y > 0, "test content must be scrollable"
    return modal, content


@pytest.mark.asyncio
async def test_scrollable_content_does_not_steal_the_arrow_keys():
    """The content pane must not be focusable, or it eats up/down itself."""
    app = _Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        modal, content = await _open_modal(pilot, app)
        assert content.focusable is False
        assert app.focused is None

        # At the bottom boundary, Down must reach the modal and move the cursor.
        content.scroll_end(animate=False)
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert modal._cursor == 1

        # At the top boundary, Up must do the same.
        content.scroll_home(animate=False)
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert modal._cursor == 0


@pytest.mark.asyncio
async def test_page_keys_still_scroll_the_content():
    """Non-focusable means the pane's own page/home/end bindings are dead —
    the modal must forward them, or a long checkpoint reads one line a time."""
    app = _Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        _, content = await _open_modal(pilot, app)

        await pilot.press("pagedown")
        await pilot.pause()
        assert content.scroll_target_y > 1

        await pilot.press("end")
        await pilot.pause()
        assert content.scroll_target_y == content.max_scroll_y

        await pilot.press("pageup")
        await pilot.pause()
        assert content.scroll_target_y < content.max_scroll_y

        await pilot.press("home")
        await pilot.pause()
        assert content.scroll_target_y == 0


@pytest.mark.asyncio
async def test_request_changes_selectable_without_scrolling_to_the_bottom():
    app = _Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        modal, content = await _open_modal(pilot, app)

        await pilot.press("down")  # scrolls; selection untouched
        await pilot.pause()
        assert modal._cursor == 0
        assert content.scroll_target_y == 1

        await pilot.press("right")
        await pilot.pause()
        assert modal._cursor == 1                 # Request Changes
        assert content.scroll_target_y == 1       # and it did not scroll

        # Enter on Request Changes opens the feedback input.
        await pilot.press("enter")
        await pilot.pause()
        assert modal._mode == "feedback"
        assert modal.query_one("#cp-feedback-input").display is True


@pytest.mark.asyncio
async def test_hints_name_their_keys():
    """Textual eats [Enter]/[Esc] as markup tags — they must be escaped, or
    the hint reads 'submit feedback   cancel' and names no key at all."""
    app = _Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        modal, _ = await _open_modal(pilot, app)
        hint = modal.query_one("#cp-hint")

        def rendered():
            return hint.visual.plain   # markup already applied

        assert "[Enter]" in rendered() and "[Esc]" in rendered()

        await pilot.press("right")
        await pilot.press("enter")          # -> feedback mode, hint is rewritten
        await pilot.pause()
        assert "[Enter]" in rendered() and "[Esc]" in rendered()


@pytest.mark.asyncio
async def test_tab_and_left_right_cycle_the_selection():
    app = _Harness()
    async with app.run_test(size=(100, 40)) as pilot:
        modal, _ = await _open_modal(pilot, app)
        for key, expected in (
            ("tab", 1), ("tab", 0),
            ("right", 1), ("left", 0),
            ("shift+tab", 1),
        ):
            await pilot.press(key)
            await pilot.pause()
            assert modal._cursor == expected, f"after {key}"
