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

    lines = {item["address"]: item["index_line"] for item in
             notes.get("aitelier")["index"]}
    assert first["address"] in lines and second["successor"]["address"] in lines
    assert "superseded" in lines[first["address"]]
    assert "[in force]" in lines[second["successor"]["address"]]

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
    measured = notes.write_entry(
        "aitelier", "Measured 2026-09-16: injection was 196,497 B before cleanup",
        "Full measurement table ...", "director-a", force="informational")
    address = measured["address"]
    entry_id = entry_id_of(address)
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
    # Positive polarity: a section citing a real address is accepted.
    ok = notes.update("aitelier", "permanent", f"see {good['address']}", 0, "d")
    assert ok["revision"] == 1
    # Negative polarity: a section citing an address with no body is refused.
    with pytest.raises(StateGraphError) as refusal:
        notes.update("aitelier", "permanent", "see note://aitelier/000000000000", 1, "d")
    assert "do not resolve" in str(refusal.value)
    assert "note://aitelier/000000000000" in str(refusal.value)
    assert notes.get("aitelier")["revision"] == 1
    # An address that leaves this project's notebook is refused too.
    with pytest.raises(StateGraphError) as foreign:
        notes.update("aitelier", "permanent",
                     f"see note://wuxia-myth/{entry_id_of(good['address'])}", 1, "d")
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
    notes.update("aitelier", "permanent", "legacy permanent rules", 0, "d")
    notes.update("wuxia-myth", "temporary", "legacy temporary handoff", 0, "d")
    aitelier = notes.get("aitelier")
    assert aitelier["permanent"] == "legacy permanent rules"
    assert aitelier["index"] == [] and aitelier["delisted_count"] == 0
    assert aitelier["entry_count"] == 0
    assert notes.get("wuxia-myth")["temporary"] == "legacy temporary handoff"
    assert notes.history("aitelier")["entries"][0]["permanent"] == "legacy permanent rules"
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
