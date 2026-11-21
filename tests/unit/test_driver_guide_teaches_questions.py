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
    # An id-less send returns no segment id, so the id must be READ BACK before
    # it can be cited; the guide names the read that yields it.
    assert "but the send returns the message_id and no segment id" in text, (
        "the guide must say the send does not return the segment id")
    assert ("read the message back with list_user_conversation(project_id, limit, cursor) "
            "to learn the id you must cite") in text, (
        "the guide must name the read-back that yields an assigned segment id")


def test_the_answer_comes_back_naming_the_message_and_the_segment():
    text = _served_text()
    # The citation key: the user's answer arrives as answers naming both the
    # message that asked and the block in it.
    assert "answers: [{message_id, segment_id}]" in text
    assert "naming your message and the question block it answers" in text
    assert "attributable to exactly one question" in text
    # Several blocks per message is only safe because each question stands alone
    # as a citation target; deleting this sentence leaves the per-message
    # attribution above unqualified.
    assert ("one message may hold several of each, and every question must be "
            "answerable on its own") in text, (
        "the guide must say a message may hold several blocks of each kind and "
        "that every question is answerable on its own")


def test_the_dashboard_keeps_questions_in_a_permanent_foldable_box():
    text = _served_text()
    assert "a permanent, foldable box" in text
    assert "between the thread and the composer" in text
    # Later info cannot wash a question out of the box.
    assert "a later info message cannot wash a question away" in text
    # ...and that is bounded, not absolute: the rail reads a finite window and
    # SAYS when the list may be missing older questions. The bound is the rail's
    # own (web/src/lib/questions.ts: QUESTIONS_PAGE_LIMIT 200 x
    # QUESTIONS_MAX_PAGES 10), named as such rather than invented here.
    assert "the questions rail's own read bound" in text
    assert "it walks at most 10 pages of 200 rows (2000 rows)" in text
    assert "it says it may be missing older questions" in text


def test_a_question_is_presentation_only_and_grants_nothing():
    text = _served_text()
    assert "A question block asks; it answers no checkpoint" in text
    assert "is not evidence and grants nothing" in text
    assert "This surface is PRESENTATION, never authority" in text


def test_the_driver_inbox_says_the_guard_covers_only_the_admin_and_external_tokens():
    text = _served_text()
    # Message text is stored and delivered, and the server guard covers the two
    # plaintext credentials only; a driver's own token is not covered, so a
    # hand-off names where the token lives, never its value.
    assert ("Message text (subject, body, segments, answers, a notebook entry's "
            "assertion) is stored and delivered") in text
    assert "the server-side credential guard covers the admin and MCP external tokens only" in text
    assert "a driver's own `AITELIER_DRIVER_TOKEN` is not covered" in text
    assert "a hand-off names where the token lives, never its value" in text


def test_the_driver_inbox_teaches_the_reader_page_shape():
    """A driver reading the guide can learn the whole reader contract.

    The served-parameter surface of ``list_driver_messages`` (``after``,
    ``limit``, ``newest``, ``statuses``) and the two counters the reader adds
    to the page are what a compacted driver cannot discover from a guide that
    only names the call, and a wrong default would send every older client's
    forward-keyset poll somewhere else.
    """
    text = _served_text()
    # The reader parameters, named as parameters (backticked) rather than
    # incidental prose.
    for parameter in ("`after`", "`limit`", "`newest`", "`statuses`"):
        assert parameter in text, f"the guide must name the reader parameter {parameter}"
    # The two additive counters, and what they count: the whole match, not
    # the page that was just returned.
    assert ("`matched_total` counts every delivery the same filters match") in text
    assert "the whole match, not the page just returned" in text
    assert ("`has_more` says whether any matched delivery falls outside that page") in text
    # The default call is still the ascending forward keyset older clients
    # page with, so the new flags cannot change what they observe.
    assert ("a call passing neither `newest` nor `statuses` keeps the ascending "
            "forward-keyset answer older clients rely on") in text, (
        "the guide must say the unflagged call is still the ascending forward page")
