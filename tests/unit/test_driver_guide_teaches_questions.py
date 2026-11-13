"""The served driver guide teaches the tagged-question surface.

These read the SERVED section text (what a driver fetches via guide_section),
not the module source, so deleting a clause from the guide reddens the test.
Each assertion pins one concept with a literal anchor phrase from the guide.
"""

from core.state_driver_guide import guide_section

ADDRESS = "guide://two-inboxes-and-your-private-notebook"


def _served_text() -> str:
    return guide_section(ADDRESS)["text"]


def test_a_question_block_carries_a_stable_id_the_reply_cites():
    text = _served_text()
    # The block kinds and the id a driver supplies, and that the service
    # assigns one when it is omitted.
    assert "`segments`" in text, "segments must be named in the send_user_message paragraph"
    assert "kind `info` or `question`" in text
    assert "Give each question block a stable `id`" in text
    assert "the service assigns one when you omit it" in text


def test_the_answer_comes_back_naming_the_message_and_the_segment():
    text = _served_text()
    # The citation key: the user's answer arrives as answers naming both the
    # message that asked and the block in it.
    assert "answers: [{message_id, segment_id}]" in text
    assert "naming your message and the question block it answers" in text
    assert "attributable to exactly one question" in text


def test_the_dashboard_keeps_questions_in_a_permanent_foldable_box():
    text = _served_text()
    assert "a permanent, foldable box" in text
    assert "between the thread and the composer" in text
    # Later info cannot wash a question out of the box.
    assert "a later info message cannot wash a question away" in text


def test_a_question_is_presentation_only_and_grants_nothing():
    text = _served_text()
    assert "A question block asks; it answers no checkpoint" in text
    assert "is not evidence and grants nothing" in text
    assert "This surface is PRESENTATION, never authority" in text
