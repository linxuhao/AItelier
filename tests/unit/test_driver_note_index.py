"""Index-mode driver notebook: every guard proven in BOTH polarities.

Reading "all present" off today's data is a snapshot, not a guard. Each test here
therefore also builds the failing case: an over-cap assertion, a dangling
address, a delist of a rule that is still in force, an attempt to rewrite a body.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.state_database import StateDatabase
from core.state_driver_index import MAX_ASSERTION_CHARS
from core.state_graph import StateConflict, StateGraphError, StateNotFound
from core.state_service import StateService
from tests.support.legacy_driver_note import seed_informational_entry, seed_section

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_driver_note_index.py"


@pytest.fixture
def service(tmp_path):
    db_path = tmp_path / "state.sqlite"
    svc = StateService(StateDatabase(str(db_path)), actor="director@example.test",
                       project_read_trusted=True)
    svc.create_project("aitelier", "AItelier")
    svc.create_project("wuxia-myth", "Wuxia")
    svc.db_path = str(db_path)
    return svc


def entry_id_of(address):
    return address.rsplit("/", 1)[1]


# --- the-assertion-cap-fails-loudly-never-truncates -------------------------

def test_the_assertion_cap_refuses_instead_of_truncating(service):
    notes = service.driver_notes
    at_cap = "A" * MAX_ASSERTION_CHARS
    written = notes.write_entry("aitelier", at_cap, "body at the cap", "d")
    # Positive polarity: exactly at the cap is accepted and reads back byte-identical.
    assert len(written["assertion"]) == MAX_ASSERTION_CHARS
    read_back = notes.get_entry("aitelier", entry_id_of(written["address"]))
    assert read_back["assertion"] == at_cap

    # Negative polarity: one character over is REFUSED at write time. Nothing is
    # truncated, nothing is stored, and the failure is not deferred to read.
    over = "B" * (MAX_ASSERTION_CHARS + 1)
    with pytest.raises(StateGraphError) as refusal:
        notes.write_entry("aitelier", over, "body over the cap", "d")
    assert f"cap is {MAX_ASSERTION_CHARS}" in str(refusal.value)
    assert "refused, not truncated" in str(refusal.value)
    index = notes.entry_index("aitelier")
    assert index["entry_count"] == 1
    assert all(entry["assertion"] != over[:MAX_ASSERTION_CHARS] for entry in index["entries"])
    assert all(not entry["assertion"].startswith("B") for entry in index["entries"])


def test_the_cap_is_also_refused_over_the_typed_command_surface(service):
    from core.state_commands import execute
    over = {"project_id": "aitelier", "assertion": "C" * (MAX_ASSERTION_CHARS + 1),
            "body": "narrative", "director_identity": "d"}
    with pytest.raises(StateGraphError) as refusal:
        execute(service, "write_driver_note_entry", over, allow_write=True)
    assert "assertion" in str(refusal.value)
    ok = execute(service, "write_driver_note_entry",
                 {**over, "assertion": "C" * MAX_ASSERTION_CHARS}, allow_write=True)
    assert ok["address"].startswith("note://aitelier/")


# --- a-ruling-retires-in-place-and-stays-readable ---------------------------

def test_a_ruling_retires_in_place_and_its_body_stays_readable(service):
    notes = service.driver_notes
    first = notes.write_entry(
        "aitelier", "Never reduce travel money below three places",
        "Owner ruling 2026-09-13: three places, because ...", "director-a")
    a_id = entry_id_of(first["address"])

    second = notes.supersede_entry(
        "aitelier", a_id, "Never reduce travel money below four places",
        "Owner ruling 2026-09-17 raised the floor to four places.",
        "the owner raised the floor", "director-b")
    b_id = entry_id_of(second["successor"]["address"])

    # The old address now carries a tombstone naming its successor.
    retired = notes.get_entry("aitelier", a_id)
    assert retired["lifecycle"] == "superseded"
    assert retired["superseded_by"] == second["successor"]["address"]
    assert retired["supersede_reason"] == "the owner raised the floor"
    assert "[superseded -> note://aitelier/" in retired["index_line"]
    # The old BODY is still readable at the old address: retiring is not deleting.
    assert retired["body"].startswith("Owner ruling 2026-09-13")
    assert notes.get_entry("aitelier", b_id)["lifecycle"] == "current"

    # The default index lists only the current rule; the tombstone is one flag away.
    default = notes.get("aitelier")
    assert [item["address"] for item in default["index"]] == [second["successor"]["address"]]
    assert "[in force]" in default["index"][0]["index_line"]
    assert default["superseded_count"] == 1
    lines = {item["address"]: item["index_line"] for item in
             notes.get("aitelier", include_superseded=True)["index"]}
    assert first["address"] in lines and second["successor"]["address"] in lines
    assert "superseded" in lines[first["address"]]

    # History is not rewritten: the row cannot be deleted and the body cannot change.
    with service.store.db.get_connection() as conn:
        with pytest.raises(Exception) as deleted:
            conn.execute("DELETE FROM state_driver_note_entries WHERE entry_id=?", (a_id,))
        assert "never deleted" in str(deleted.value)
        with pytest.raises(Exception) as rewritten:
            conn.execute("UPDATE state_driver_note_entries SET body='rewritten' WHERE entry_id=?",
                         (a_id,))
        assert "immutable" in str(rewritten.value)

    # A second supersede of the same address is refused, not silently chained.
    with pytest.raises(StateConflict) as conflict:
        notes.supersede_entry("aitelier", a_id, "third", "third body", "again", "director-c")
    assert "already superseded by" in str(conflict.value)


# --- delist-keeps-the-body-and-counts-what-it-evicted -----------------------

def test_delist_evicts_from_the_index_keeps_the_body_and_counts_it(service):
    notes = service.driver_notes
    # An informational row from before the 2026-10-06 ruling: still delistable.
    entry_id = seed_informational_entry(
        service, "aitelier", "Measured 2026-09-16: injection was 196,497 B before cleanup",
        "Full measurement table ...", "director-a")
    address = f"note://aitelier/{entry_id}"
    assert notes.get("aitelier")["delisted_count"] == 0

    delisted = notes.delist_entry(
        "aitelier", entry_id, "superseded by the post-change measurement; keeps no decision open",
        "director-a")
    # (1) the body is NOT deleted: the same address still resolves to it.
    assert notes.get_entry("aitelier", entry_id)["body"] == "Full measurement table ..."
    # (2) the reason lives with the entry, not in a command line that scrolled away.
    assert delisted["delist_reason"].startswith("superseded by the post-change measurement")
    assert notes.get_entry("aitelier", entry_id)["delist_reason"] == delisted["delist_reason"]
    # (3) the count is readable, so a short index cannot hide what it evicted.
    note = notes.get("aitelier")
    assert note["delisted_count"] == 1
    assert [item["address"] for item in note["index"]] == []
    assert notes.entry_index("aitelier")["entries"] == []
    listed_with = notes.entry_index("aitelier", include_delisted=True)
    assert [item["address"] for item in listed_with["entries"]] == [address]
    assert listed_with["delisted_count"] == 1


def test_delist_is_refused_while_the_line_can_still_change_a_decision(service):
    notes = service.driver_notes
    binding = notes.write_entry(
        "aitelier", "Never reduce travel money below three places",
        "Owner ruling.", "director-a", landed="three places VERIFIED")
    entry_id = entry_id_of(binding["address"])
    # Landed is not expired: the rule is landed AND still in force.
    assert "[landed: three places VERIFIED]" in binding["index_line"]
    assert "[in force]" in binding["index_line"]

    with pytest.raises(StateGraphError) as refusal:
        notes.delist_entry("aitelier", entry_id, "looks done", "director-a")
    assert "still in force" in str(refusal.value)
    assert "supersede_driver_note_entry" in str(refusal.value)
    assert notes.get("aitelier")["delisted_count"] == 0
    assert len(notes.get("aitelier")["index"]) == 1

    # There is no force override on delist: the refusal is read from the stored
    # row, so a caller cannot argue its way past it with an argument.
    with pytest.raises(TypeError):
        notes.delist_entry("aitelier", entry_id, "looks done", "director-a", force="informational")

    # The supported route out is supersede, and then the tombstone may be delisted.
    notes.supersede_entry("aitelier", entry_id, "Never reduce travel money below four places",
                          "Owner raised the floor.", "owner raised the floor", "director-b")
    evicted = notes.delist_entry("aitelier", entry_id, "the successor carries the rule",
                                 "director-b")
    assert evicted["listing"] == "delisted"
    assert notes.get("aitelier")["delisted_count"] == 1
    assert notes.get_entry("aitelier", entry_id)["body"] == "Owner ruling."
    with pytest.raises(StateConflict) as again:
        notes.delist_entry("aitelier", entry_id, "twice", "director-b")
    assert "already delisted" in str(again.value)


# --- the-index-line-carries-status-not-just-topic ---------------------------

def test_the_index_line_carries_the_assertion_and_its_status(service):
    notes = service.driver_notes
    topicless = notes.write_entry(
        "aitelier", "Never reduce travel money below three places",
        "Owner ruling.", "d", landed="three places VERIFIED")
    line = topicless["index_line"]
    assert line.startswith("Never reduce travel money below three places")
    assert "[in force]" in line and "[landed: three places VERIFIED]" in line
    # Landed and in-force are separate fields, so "done" never reads as "expired".
    assert topicless["force"] == "in_force" and topicless["landed"]
    assert topicless["lifecycle"] == "current"
    # A multi-line assertion is a body in disguise and is refused.
    with pytest.raises(StateGraphError) as multiline:
        notes.write_entry("aitelier", "topic\nnarrative", "body", "d")
    assert "single line" in str(multiline.value)


# --- every-index-address-resolves-and-a-check-bites ------------------------

def test_a_dangling_address_is_refused_at_write_time(service):
    notes = service.driver_notes
    good = notes.write_entry("aitelier", "keep the gate", "body", "d")
    # Positive polarity: an entry citing a real address is accepted.
    ok = notes.write_entry("aitelier", "cites the gate", f"see {good['address']}", "d")
    assert ok["entry_count"] == 2
    # Negative polarity: an entry citing an address with no body is refused.
    with pytest.raises(StateGraphError) as refusal:
        notes.write_entry("aitelier", "dangles", "see note://aitelier/000000000000", "d")
    assert "do not resolve" in str(refusal.value)
    assert "note://aitelier/000000000000" in str(refusal.value)
    assert notes.get("aitelier")["entry_count"] == 2
    # An address that leaves this project's notebook is refused too.
    with pytest.raises(StateGraphError) as foreign:
        notes.write_entry("aitelier", "leaves",
                          f"see note://wuxia-myth/{entry_id_of(good['address'])}", "d")
    assert "leaves this project" in str(foreign.value)


def test_the_address_check_bites_on_a_dangling_pointer_and_clears_when_it_is_gone(service):
    notes = service.driver_notes
    notes.write_entry("aitelier", "keep the gate", "body", "d")
    assert notes.check_index("aitelier")["ok"] is True

    # Plant a dangling pointer the way one really arrives: bytes already in the
    # database (migration, direct edit) rather than through the guarded write.
    with service.store.db.get_connection() as conn:
        conn.execute(
            "INSERT INTO state_driver_notes(project_id,revision,permanent_text,temporary_text,"
            "updated_by_actor,updated_by_director,updated_at) "
            "VALUES('aitelier',1,'see note://aitelier/abcdefabcdef','','a','d','2026-09-17T00:00:00+00:00')")
        conn.commit()
    red = notes.check_index("aitelier")
    assert red["ok"] is False
    assert red["dangling"][0]["address"] == "note://aitelier/abcdefabcdef"
    assert red["dangling"][0]["source"] == "section:permanent"

    with service.store.db.get_connection() as conn:
        conn.execute("UPDATE state_driver_notes SET permanent_text='' WHERE project_id='aitelier'")
        conn.commit()
    green = notes.check_index("aitelier")
    assert green["ok"] is True and green["dangling"] == []


def test_the_check_script_exits_nonzero_on_a_dangling_address(service):
    notes = service.driver_notes
    notes.write_entry("aitelier", "keep the gate", "body", "d")

    def run():
        return subprocess.run([sys.executable, str(CHECK_SCRIPT), service.db_path],
                              capture_output=True, text=True, cwd=str(REPO_ROOT))

    green = run()
    assert green.returncode == 0, green.stderr
    assert "OK: every driver-note address resolves" in green.stdout

    with service.store.db.get_connection() as conn:
        conn.execute(
            "INSERT INTO state_driver_notes(project_id,revision,permanent_text,temporary_text,"
            "updated_by_actor,updated_by_director,updated_at) "
            "VALUES('aitelier',1,'see note://aitelier/abcdefabcdef','','a','d','2026-09-17T00:00:00+00:00')")
        conn.commit()
    red = run()
    assert red.returncode == 1, (red.returncode, red.stdout, red.stderr)
    assert "DANGLING note://aitelier/abcdefabcdef" in red.stdout
    assert "do not resolve" in red.stderr
    # A check that cannot run is not a green light either.
    broken = subprocess.run([sys.executable, str(CHECK_SCRIPT)],
                            capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert broken.returncode == 2


# --- migration: nothing that exists today stops being readable --------------

def test_existing_prose_notes_stay_readable_and_start_with_an_empty_index(service):
    notes = service.driver_notes
    seed_section(service, "aitelier", "permanent", "legacy permanent rules", "d")
    seed_section(service, "wuxia-myth", "temporary", "legacy temporary handoff", "d")
    aitelier = notes.get("aitelier")
    assert "legacy permanent rules" not in json.dumps(aitelier)
    assert aitelier["index"] == [] and aitelier["delisted_count"] == 0
    assert aitelier["entry_count"] == 0 and aitelier["revision"] == 1
    assert "legacy temporary handoff" not in json.dumps(notes.get("wuxia-myth"))
    assert notes.history("aitelier")["entries"][0]["permanent"] == "legacy permanent rules"
    assert notes.history("wuxia-myth")["entries"][0]["temporary"] == "legacy temporary handoff"
    # Entries are project-scoped exactly like the sections.
    written = notes.write_entry("aitelier", "aitelier only", "body", "d")
    assert notes.entry_index("wuxia-myth")["entries"] == []
    with pytest.raises(StateNotFound):
        notes.get_entry("wuxia-myth", entry_id_of(written["address"]))


def test_the_guide_index_keeps_the_biting_rules_and_addresses_the_rest():
    from core.state_driver_guide import (GUIDE_SECTIONS, STATE_DRIVER_GUIDE,
                                         STATE_DRIVER_GUIDE_INDEX, guide_section)
    assert len(STATE_DRIVER_GUIDE_INDEX) < len(STATE_DRIVER_GUIDE) / 4
    for rule in ("start_external_attempt BEFORE dispatching workers",
                 "Do not send VERIFIED as an execution status",
                 "record_evidence for EACH current criterion",
                 "Do not weaken a criterion to hide failure"):
        assert rule in STATE_DRIVER_GUIDE_INDEX, rule
    # Every address advertised by the index resolves to a section verbatim.
    for slug, section in GUIDE_SECTIONS.items():
        address = f"guide://{slug}"
        assert address in STATE_DRIVER_GUIDE_INDEX
        assert guide_section(address)["text"] == section["text"]
        assert section["text"] in STATE_DRIVER_GUIDE
    with pytest.raises(KeyError):
        guide_section("guide://not-a-section")
    with pytest.raises(KeyError):
        guide_section("not-an-address")


def test_both_guide_variants_describe_an_entries_only_notebook():
    """Neither the full guide nor its index may describe writable sections."""
    from core.state_driver_guide import GUIDE_SECTIONS, STATE_DRIVER_GUIDE_INDEX
    notebook = GUIDE_SECTIONS["director-notebook-context-not-a-second-state-dat"]["text"]
    index = STATE_DRIVER_GUIDE_INDEX.split("## The director notebook is an index too", 1)[1]
    for text in (notebook, index):
        assert "update_driver_note writes" not in text
        assert "returns the permanent/temporary sections" not in text
        assert "reads the permanent/temporary sections" not in text
        assert "update_driver_note is refused" in text
        for step in ("supersede_driver_note_entry", "delist_driver_note_entry",
                     "search_driver_note_entries", "include_superseded"):
            assert step in text, step
        # In-flight state is no longer taught as a notebook entry: the text says
        # informational is refused and names where in-flight state goes instead.
        assert "as an entry with force" not in text
        assert 'force="informational" is refused' in text
        for dag in ("set_node_hold", "report_issue", "set_node_priority"):
            assert dag in text, dag


def test_a_deleted_biting_rule_breaks_the_index_build_instead_of_emptying_it():
    import core.state_driver_guide as guide
    with pytest.raises(AssertionError) as vanished:
        guide._sentence("a guide that lost its rules", "Do not weaken a criterion to hide failure")
    assert "vanished from the guide text" in str(vanished.value)
    assert guide._sentence(guide.STATE_DRIVER_GUIDE,
                           "Do not weaken a criterion to hide failure")


def test_the_recovery_hook_reinjects_the_index_rules_instead_of_dropping_them():
    """The hook selected sections by heading; the index has different headings.

    An empty selection would have silently dropped exactly the rules the hook
    exists to re-inject, which is the failure shape this round is about.
    """
    import importlib.util

    from core.state_driver_guide import STATE_DRIVER_GUIDE_INDEX

    hook_py = REPO_ROOT / ".codex" / "hooks" / "postcompact_driver_state.py"
    spec = importlib.util.spec_from_file_location("postcompact_index_candidate", hook_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    selected = module._guide_sections(STATE_DRIVER_GUIDE_INDEX)
    assert "start_external_attempt BEFORE dispatching workers" in selected
    assert "Do not send VERIFIED as an execution status" in selected
    assert "guide://" in selected
    # Negative polarity: the fallback is a fallback, not a hard-coded index.
    assert "start_external_attempt" not in module._guide_sections("nothing useful here")


# --- rules only: force="informational" is refused (owner ruling 2026-10-06) ----

def test_an_informational_write_or_supersede_is_refused_verbatim_and_rules_still_write(service):
    from core.state_commands import describe, execute
    from core.state_driver_index import INFORMATIONAL_CLOSED
    notes = service.driver_notes
    events = len(service.store.events("aitelier"))
    for refuse in (
            lambda: notes.write_entry("aitelier", "run r-1 in flight", "owner", "d",
                                      force="informational"),
            lambda: execute(service, "write_driver_note_entry", {
                "project_id": "aitelier", "assertion": "run r-1 in flight", "body": "owner",
                "director_identity": "d", "force": "informational"}, allow_write=True)):
        with pytest.raises(StateGraphError) as refusal:
            refuse()
        # Refused with the one message, never coerced to in_force.
        assert str(refusal.value) == INFORMATIONAL_CLOSED
    for pointer in ("owner ruling 2026-10-06", "State DAG", "attempts", "set_node_hold",
                    "report_issue", "set_node_priority"):
        assert pointer in INFORMATIONAL_CLOSED, pointer
    assert notes.get("aitelier")["entry_count"] == 0
    assert len(service.store.events("aitelier")) == events

    omitted = notes.write_entry("aitelier", "keep the gate", "body", "d")
    explicit = execute(service, "write_driver_note_entry", {
        "project_id": "aitelier", "assertion": "keep the second gate", "body": "body",
        "director_identity": "d", "force": "in_force"}, allow_write=True)
    assert omitted["force"] == explicit["force"] == "in_force"
    with pytest.raises(StateGraphError) as bogus:
        notes.write_entry("aitelier", "x", "y", "d", force="advisory")
    assert str(bogus.value) == "force must be in_force"

    target = entry_id_of(omitted["address"])
    for refuse in (
            lambda: notes.supersede_entry("aitelier", target, "gate paused", "b", "changed",
                                          "d", force="informational"),
            lambda: execute(service, "supersede_driver_note_entry", {
                "project_id": "aitelier", "entry_id": target, "assertion": "gate paused",
                "body": "b", "reason": "changed", "director_identity": "d",
                "force": "informational"}, allow_write=True)):
        with pytest.raises(StateGraphError) as refusal:
            refuse()
        assert str(refusal.value) == INFORMATIONAL_CLOSED
    # The refused supersede left its target current and wrote no successor.
    assert notes.get_entry("aitelier", target)["lifecycle"] == "current"
    assert notes.get("aitelier")["entry_count"] == 2
    # The published contract advertises in_force only.
    schema = describe()["operations"]["write_driver_note_entry"]["arguments"]
    assert schema["properties"]["force"]["enum"] == ["in_force"]


def test_a_legacy_informational_row_stays_readable_supersedable_and_delistable(service):
    from core.state_driver_index import INFORMATIONAL_CLOSED
    notes = service.driver_notes
    first = seed_informational_entry(service, "aitelier", "run r-7 in flight", "owner a")
    second = seed_informational_entry(service, "aitelier", "run r-8 in flight", "owner b")
    read = notes.get_entry("aitelier", first)
    assert read["force"] == "informational" and read["body"] == "owner a"
    assert "[informational]" in read["index_line"]

    # Superseding it WITH an informational successor is refused like any write.
    with pytest.raises(StateGraphError) as refusal:
        notes.supersede_entry("aitelier", first, "still in flight", "b", "x", "d",
                              force="informational")
    assert str(refusal.value) == INFORMATIONAL_CLOSED
    assert notes.get_entry("aitelier", first)["lifecycle"] == "current"

    # Superseding it with an in_force rule (force omitted) works.
    moved = notes.supersede_entry("aitelier", first, "Runs are tracked on attempts",
                                  "Owner ruling 2026-10-06.", "migrated to a rule", "d")
    assert moved["superseded"]["lifecycle"] == "superseded"
    assert moved["superseded"]["force"] == "informational"
    assert moved["successor"]["force"] == "in_force"
    # Delisting the other legacy row works; its body stays readable.
    delisted = notes.delist_entry("aitelier", second, "landed", "d")
    assert delisted["listing"] == "delisted"
    assert notes.get_entry("aitelier", second)["body"] == "owner b"
    assert [item["address"] for item in notes.get("aitelier")["index"]] == [
        moved["successor"]["address"]]


# --- the default index lists current entries only ---------------------------

def test_the_default_index_never_lists_a_superseded_entry(service):
    from core.state_commands import execute
    notes = service.driver_notes
    a = notes.write_entry("aitelier", "rule A v1", "body", "d")
    b = notes.write_entry("aitelier", "rule B", "body", "d")
    a2 = notes.supersede_entry("aitelier", entry_id_of(a["address"]), "rule A v2", "body",
                               "r", "d")
    a3 = notes.supersede_entry("aitelier", a2["successor"]["entry_id"], "rule A v3", "body",
                               "r", "d")
    current = [b["address"], a3["successor"]["address"]]
    superseded = {a["address"]: a2["successor"]["address"],
                  a2["successor"]["address"]: a3["successor"]["address"]}

    def addresses(payload, key):
        return [item["address"] for item in payload[key]]

    defaults = [
        (notes.get("aitelier"), "index"),
        (execute(service, "get_driver_note", {"project_id": "aitelier"}), "index"),
        (notes.entry_index("aitelier"), "entries"),
        (execute(service, "driver_note_index", {"project_id": "aitelier"}), "entries"),
        (a3, "index"),  # the projection a write returns is the default index too
    ]
    for payload, key in defaults:
        assert addresses(payload, key) == current, payload
        assert all(item["lifecycle"] == "current" for item in payload[key])
        assert (payload["entry_count"], payload["listed_count"], payload["delisted_count"],
                payload["superseded_count"]) == (4, 4, 0, 2)

    widened = [
        (notes.get("aitelier", include_superseded=True), "index"),
        (execute(service, "get_driver_note", {"project_id": "aitelier",
                                              "include_superseded": True}), "index"),
        (notes.entry_index("aitelier", include_superseded=True), "entries"),
        (execute(service, "driver_note_index", {"project_id": "aitelier",
                                                "include_superseded": True}), "entries"),
    ]
    for payload, key in widened:
        assert addresses(payload, key) == [a["address"], b["address"],
                                           a2["successor"]["address"],
                                           a3["successor"]["address"]]
        for item in payload[key]:
            assert item["superseded_by"] == superseded.get(item["address"])

    # include_delisted keeps its meaning and composes with include_superseded.
    notes.delist_entry("aitelier", entry_id_of(a["address"]), "successor carries it", "d")
    assert a["address"] not in addresses(notes.get("aitelier", include_superseded=True), "index")
    assert a["address"] not in addresses(
        notes.get("aitelier", include_delisted=True), "index")
    both = notes.get("aitelier", include_superseded=True, include_delisted=True)
    assert a["address"] in addresses(both, "index")
    assert (both["superseded_count"], both["delisted_count"]) == (2, 1)

    # limit and truncated apply AFTER the filter.
    page = notes.entry_index("aitelier", limit=1)
    assert addresses(page, "entries") == [b["address"]] and page["truncated"] is True
    full = notes.entry_index("aitelier", limit=2)
    assert addresses(full, "entries") == current and full["truncated"] is False
    wide = notes.entry_index("aitelier", limit=2, include_superseded=True)
    assert wide["truncated"] is True


# --- search_driver_note_entries ----------------------------------------------

def test_search_entries_is_a_case_insensitive_literal_over_assertion_and_body(service):
    from core.state_commands import execute
    notes = service.driver_notes
    gate = notes.write_entry("aitelier", "Never ship WITHOUT the Gate", "plain body", "d")
    wuxia = notes.write_entry("aitelier", "rule two",
                              "整体要求: 武侠 travel money stays in three places; GATE first", "d")
    apfel = notes.write_entry("aitelier", "Mixed ÄPFEL rule", "nothing here", "d")

    hits = notes.search_entries("aitelier", "gate")
    assert [hit["address"] for hit in hits["entries"]] == [gate["address"], wuxia["address"]]
    assert hits["entries"][0]["matched_in"] == ["assertion"]
    assert hits["entries"][1]["matched_in"] == ["body"]
    assert hits["truncated"] is False
    assert hits["next_after"] == entry_id_of(wuxia["address"])
    assert set(hits["entries"][0]) == {
        "address", "entry_id", "force", "lifecycle", "superseded_by", "listing",
        "assertion", "matched_in", "excerpt"}
    assert hits["entries"][0]["assertion"] == "Never ship WITHOUT the Gate"
    assert (hits["entries"][0]["force"], hits["entries"][0]["lifecycle"],
            hits["entries"][0]["listing"]) == ("in_force", "current", "listed")

    chinese = notes.search_entries("aitelier", "武侠")
    assert [hit["address"] for hit in chinese["entries"]] == [wuxia["address"]]
    assert "武侠" in chinese["entries"][0]["excerpt"]
    for query in ("äpfel", "ÄPFEL", "mixed äpfel RULE"):
        found = notes.search_entries("aitelier", query)["entries"]
        assert [hit["address"] for hit in found] == [apfel["address"]], query
    # Literal, not a pattern: regex metacharacters match nothing here.
    assert notes.search_entries("aitelier", "g.te")["entries"] == []
    empty = notes.search_entries("aitelier", "does-not-exist")
    assert empty["entries"] == [] and empty["next_after"] is None
    # The typed command surface returns the same page.
    assert execute(service, "search_driver_note_entries",
                   {"project_id": "aitelier", "query": "gate"}) == hits
    # Project isolation.
    assert notes.search_entries("wuxia-myth", "gate")["entries"] == []


def test_search_entries_excludes_superseded_and_delisted_by_default(service):
    notes = service.driver_notes
    old = notes.write_entry("aitelier", "gate rule v1", "body", "d")
    new = notes.supersede_entry("aitelier", entry_id_of(old["address"]), "gate rule v2",
                                "body", "raised", "d")

    def found(**flags):
        return [hit["address"] for hit in
                notes.search_entries("aitelier", "gate rule", **flags)["entries"]]

    assert found() == [new["successor"]["address"]]
    widened = notes.search_entries("aitelier", "gate rule", include_superseded=True)["entries"]
    assert [hit["address"] for hit in widened] == [old["address"], new["successor"]["address"]]
    assert widened[0]["lifecycle"] == "superseded"
    assert widened[0]["superseded_by"] == new["successor"]["address"]

    notes.delist_entry("aitelier", entry_id_of(old["address"]), "successor carries it", "d")
    assert found(include_superseded=True) == [new["successor"]["address"]]
    assert found(include_delisted=True) == [new["successor"]["address"]]
    both = notes.search_entries("aitelier", "gate rule", include_superseded=True,
                                include_delisted=True)["entries"]
    assert [hit["address"] for hit in both] == [old["address"], new["successor"]["address"]]
    assert both[0]["listing"] == "delisted"


def test_search_entries_paginates_stably(service, monkeypatch):
    notes = service.driver_notes
    clock = iter(f"2026-10-06T10:00:{second:02d}.000000+00:00" for second in range(60))
    monkeypatch.setattr("core.state_driver_notes.now", lambda: next(clock))
    written = [notes.write_entry("aitelier", f"paged rule {n}", "body", "d")["address"]
               for n in range(5)]
    notes.write_entry("aitelier", "unrelated", "body", "d")

    pages, after = [], None
    while True:
        page = notes.search_entries("aitelier", "PAGED", limit=2, after=after)
        pages.append([hit["address"] for hit in page["entries"]])
        if not page["truncated"]:
            break
        after = page["next_after"]
        if len(pages) == 1:
            # An entry written mid-pagination lands after the cursor, never before it.
            written.append(notes.write_entry("aitelier", "paged rule late", "body", "d")["address"])
    assert pages == [written[0:2], written[2:4], written[4:6]]
    # Re-reading from the same cursor returns the same page.
    first = notes.search_entries("aitelier", "paged", limit=2)
    again = notes.search_entries("aitelier", "paged", limit=2, after=first["next_after"])
    assert [hit["address"] for hit in again["entries"]] == written[2:4]


def test_search_entries_breaks_creation_time_ties_by_entry_id(service, monkeypatch):
    notes = service.driver_notes
    monkeypatch.setattr("core.state_driver_notes.now",
                        lambda: "2026-10-06T10:00:00.000000+00:00")
    ids = sorted(entry_id_of(notes.write_entry("aitelier", f"tied rule {n}", "b", "d")["address"])
                 for n in range(4))
    seen, after = [], None
    while True:
        page = notes.search_entries("aitelier", "tied", limit=1, after=after)
        seen += [hit["entry_id"] for hit in page["entries"]]
        if not page["truncated"]:
            break
        after = page["next_after"]
    assert seen == ids


def test_search_entries_excerpts_are_bounded_redacted_and_arguments_are_bounded(service):
    from core.state_commands import execute
    notes = service.driver_notes
    body = "x" * 3000 + " needle-in-the-body " + "y" * 3000 + " password=hunter2-secret"
    written = notes.write_entry("aitelier", "long body rule", body, "d")
    hit = notes.search_entries("aitelier", "NEEDLE", excerpt_chars=64)["entries"][0]
    assert len(hit["excerpt"]) <= 64 and "needle-in-the-body" in hit["excerpt"]
    redacted = notes.search_entries("aitelier", "password", excerpt_chars=1000)["entries"][0]
    assert "hunter2-secret" not in redacted["excerpt"]
    assert "password=[REDACTED]" in redacted["excerpt"]
    # Assertion-only match: the excerpt is of the assertion.
    assert notes.search_entries("aitelier", "long body")["entries"][0]["excerpt"] == \
        "long body rule"

    for bad in ({"limit": 0}, {"limit": 101}, {"excerpt_chars": 63},
                {"excerpt_chars": 1001}, {"query": "q" * 501}, {"after": "not-an-id!!"}):
        with pytest.raises(StateGraphError):
            notes.search_entries("aitelier", **{"query": "rule", **bad})
        with pytest.raises(StateGraphError):
            execute(service, "search_driver_note_entries",
                    {"project_id": "aitelier", "query": "rule", **bad})
    with pytest.raises(StateNotFound):
        notes.search_entries("aitelier", "rule", after="000000000000")
    assert written["address"]


def test_search_entries_has_the_same_authorization_as_the_index(tmp_path):
    from core.state_commands import (DRIVER_TOOL_DEFINITIONS, PUBLIC_READS, READ_REQUESTS,
                                     WRITE_REQUESTS, execute, read_visibility)
    from core.state_commands import ProjectPrivate
    assert "search_driver_note_entries" in READ_REQUESTS
    assert "search_driver_note_entries" not in WRITE_REQUESTS
    assert read_visibility("search_driver_note_entries") == read_visibility("driver_note_index")
    assert ("search_driver_note_entries" in PUBLIC_READS) == ("driver_note_index" in PUBLIC_READS)
    read_tool = next(tool for tool in DRIVER_TOOL_DEFINITIONS
                     if tool["function"]["name"] == "state_graph_read")
    assert "search_driver_note_entries" in read_tool["function"]["parameters"][
        "properties"]["action"]["enum"]

    db_path = str(tmp_path / "state.sqlite")
    owner = StateService(StateDatabase(db_path), actor="owner", project_read_trusted=True)
    owner.create_project("p", "P")
    owner.driver_notes.write_entry("p", "PRIVATE-RULE-CANARY", "PRIVATE-BODY-CANARY", "d")
    anonymous = StateService(StateDatabase(db_path), actor="anonymous",
                             project_read_trusted=False)
    for action in ("driver_note_index", "search_driver_note_entries"):
        # An unopened project is refused the same way for both reads.
        with pytest.raises(ProjectPrivate):
            execute(anonymous, action, {"project_id": "p"})
    # A leaf call that skips execute still gets no unopened-project row.
    for read in (lambda: anonymous.driver_notes.entry_index("p"),
                 lambda: anonymous.driver_notes.search_entries("p", "canary")):
        try:
            leaked = json.dumps(read())
        except Exception:
            leaked = ""
        assert "CANARY" not in leaked
    owner.open_project("p")
    opened = StateService(StateDatabase(db_path), actor="anonymous",
                          project_read_trusted=False)
    index = execute(opened, "driver_note_index", {"project_id": "p"})
    found = execute(opened, "search_driver_note_entries", {"project_id": "p", "query": "canary"})
    assert [e["address"] for e in found["entries"]] == [e["address"] for e in index["entries"]]
