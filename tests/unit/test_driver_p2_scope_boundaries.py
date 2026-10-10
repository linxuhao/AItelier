"""Aggregate scope and published wait boundary controls."""
import pytest
from pydantic import ValidationError
from core.state_commands import execute, GetDriverNote
from core.state_graph import StateGraphError
from tests.unit.test_driver_p2 import peers

@pytest.mark.asyncio
async def test_combined_500_pages_keep_each_recipient_cursor(peers):
    _,s=peers
    bob=s["bob"]; ada=s["ada"]
    cursor=bob.store.events("beta")[-1]["seq"]
    for i in range(205):
        ada.driver_inbox.send_driver_message("page"+str(i),"row"+str(i),"",target_driver_id="bob")
    ada.driver_inbox.send_driver_message("other","foreign","",target_driver_id="carol")
    seen=[]; after=0
    for count in [100,100,5]:
        result=await bob.wait_for_state_change("beta",after=cursor,inbox_after=after,
              include_driver_inbox=True,limit=500,timeout_seconds=0)
        assert result["next_after"]==cursor
        assert len(result["driver_inbox"])==count
        seen += [row["seq"] for row in result["driver_inbox"]]
        after=result["next_inbox_after"]
    assert seen==list(range(1,206)) and after==205
    empty=await bob.wait_for_state_change("beta",after=cursor,inbox_after=after,
          include_driver_inbox=True,limit=500,timeout_seconds=0)
    assert empty["driver_inbox"]==[] and empty["next_inbox_after"]==205

def test_aggregate_native_scope_counts_and_owner_refusal(peers):
    _,s=peers
    own=s["ada"].driver_notes.for_driver("ada","ada")
    own.write_entry("ada","Private rule","private body","ada")
    result=own.get("ada")
    assert result["driver_id"]=="ada" and result["revision"]==0 and result["index_count"]==1
    assert "project_id" not in result
    with pytest.raises(StateGraphError):
        s["bob"].driver_notes.for_driver("ada","bob").write_entry("ada","Bad","body","bob")
    with pytest.raises(StateGraphError):
        own.get("bob")
    assert execute(s["ada"],"get_driver_note",{"driver_id":"ada"})==result

@pytest.mark.parametrize("args",[{},{"project_id":"alpha","driver_id":"ada"}])
def test_aggregate_model_scope_xor(args):
    with pytest.raises(ValidationError): GetDriverNote(**args)

@pytest.mark.parametrize("protocol",[None,"v2","v3"])
def test_same_project_code_preserves_closed_envelope(peers,protocol):
    _,s=peers
    args=dict(sender_project_id="alpha",target_project_id="alpha",
              director_identity="ada",request_key="same",subject="same",body="")
    if protocol:args["protocol_version"]=protocol
    result=execute(s["ada"],"send_director_message",args,allow_write=True)
    if protocol=="v3":
        assert result["code"]=="use_driver_inbox"
        from contracts.director_messaging.v3.conformance import validate_envelope
        validate_envelope(result)
    else:
        assert result=={"schema":"aitelier.director-messaging.v2",
                        "code":"invalid_request","detail":{"message":"invalid_request"}}
        import json
        from pathlib import Path
        from jsonschema import Draft202012Validator, ValidationError
        schema=json.loads(Path("contracts/director_messaging/v2/schema.json").read_text())
        validator=Draft202012Validator({"$ref":"#/$defs/error","$defs":schema["$defs"]})
        validator.validate(result)
        with pytest.raises(ValidationError):
            validator.validate({**result,"code":"use_driver_inbox","detail":{"message":"use_driver_inbox"}})


def test_versioned_refusal_real_consumer_inverse(peers):
    _,s=peers
    args=dict(sender_project_id="alpha",target_project_id="alpha",
              director_identity="ada",request_key="inverse",subject="same",body="")
    legacy=execute(s["ada"],"send_director_message",args,allow_write=True)
    current=execute(s["ada"],"send_director_message",{**args,"protocol_version":"v3"},allow_write=True)
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator, ValidationError
    from contracts.director_messaging.v3.conformance import validate_envelope
    schema=json.loads(Path("contracts/director_messaging/v2/schema.json").read_text())
    consumer=Draft202012Validator({"$ref":"#/$defs/error","$defs":schema["$defs"]})
    consumer.validate(legacy)
    validate_envelope(current)
    # The actual immutable v2 consumer rejects both proposed workarounds.
    with pytest.raises(ValidationError): consumer.validate(current)
    with pytest.raises(ValidationError):
        consumer.validate({**legacy,"detail":{"message":"Use the driver inbox"}})
