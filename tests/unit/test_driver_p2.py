import asyncio,copy,json,sqlite3
import pytest
from core.state_database import StateDatabase
from core.state_service import StateService
from core.state_commands import execute, PUBLIC_READS
from core.state_graph import StateGraphError
from core.drivers import DriverRegistry
from core.director_messaging_protocol import DirectorMessageError

@pytest.fixture
def peers(tmp_path):
 db=StateDatabase(str(tmp_path/"state.sqlite"))
 seed=StateService(db,actor="fixture",project_read_trusted=True)
 seed.create_project("alpha","alpha");seed.create_project("beta","beta")
 registry=DriverRegistry(db,"test-only-pepper-not-a-production-secret")
 for did in ["ada","bob","carol"]:
  registry.register(did,did,actor="test")
 for project,drivers in [("alpha",["ada"]),("beta",["bob","carol"])]:
  for did in drivers:registry.set_membership(project,did,"member",0,"fixture",actor="test")
 return registry,{did:StateService(db,actor="driver:"+did,driver_id=did,project_read_trusted=True) for did in ["ada","bob","carol"]}

def send(s,**kw):
 return execute(s,"send_director_message",{"sender_project_id":"alpha","target_project_id":"beta",
     "protocol_version":"v3","director_identity":"ada","request_key":"message","subject":"notice","body":"work",**kw},allow_write=True)

def test_private_driver_deliveries_cas_and_recipient(peers):
 _,s=peers
 msg=execute(s["ada"],"send_driver_message",{"request_key":"one","subject":"question","body":"body","project_members":"beta"},allow_write=True)
 assert len(msg["deliveries"])==2
 bob=next(d for d in msg["deliveries"] if d["target_driver_id"]=="bob")
 with pytest.raises(StateGraphError,match="not_inbox_recipient"):
  s["ada"].driver_inbox.list_driver_messages("bob")
 with pytest.raises(StateGraphError,match="not_inbox_recipient"):
  s["carol"].driver_inbox.transition(bob["delivery_id"],1,"wrong","acknowledged")
 args={"delivery_id":bob["delivery_id"],"expected_version":1,"request_key":"ack"}
 ack=execute(s["bob"],"acknowledge_driver_message",args,allow_write=True)
 assert ack["status"]=="acknowledged" and ack["version"]==2
 assert execute(s["bob"],"acknowledge_driver_message",args,allow_write=True)==ack
 with pytest.raises(StateGraphError,match="version_conflict"):
  s["bob"].driver_inbox.transition(bob["delivery_id"],1,"stale","resolved")
 result=s["bob"].driver_inbox.transition(bob["delivery_id"],2,"resolved","resolved")
 assert result["status"]=="resolved"
 assert s["carol"].driver_inbox.list_driver_messages()["messages"][0]["status"]=="unread"
 assert "list_driver_messages" not in PUBLIC_READS

@pytest.mark.asyncio
async def test_private_wait_owns_cursor_and_combined_wait(peers):
 _,s=peers
 cursor=s["bob"].store.events("beta")[-1]["seq"]
 waiting=asyncio.create_task(s["bob"].wait_for_state_change("beta",after=cursor,timeout_seconds=2,include_driver_inbox=True))
 await asyncio.sleep(.02)
 s["ada"].driver_inbox.send_driver_message("wake","wake","",target_driver_id="bob")
 result=await waiting
 assert result["next_inbox_after"]==1 and len(result["driver_inbox"])==1
 assert isinstance(result["next_after"],int)
 assert not s["carol"].driver_inbox.list_driver_messages()["messages"]
 own=await s["bob"].driver_inbox.wait_for_driver_inbox(after=1,timeout_seconds=0)
 assert own["messages"]==[] and own["next_after"]==1

def test_system_notice_and_ref_refusal(peers):
 _,s=peers
 m=s["ada"].driver_inbox.system_notice(request_key="system",subject="lease",body="notice",target_driver_id="bob",kind="lease_notice",delivery_mode="standing")
 row=s["bob"].driver_inbox.list_driver_messages()["messages"][0]
 assert row["sender_driver_id"] is None
 with pytest.raises(StateGraphError,match="invalid_refs"):
  s["ada"].driver_inbox.send_driver_message("badrefs","bad","",target_driver_id="bob",refs={"attempt_id":"absent"})
 assert len(s["bob"].driver_inbox.list_driver_messages()["messages"])==1

def test_driver_notebook_owner_privacy_force_and_reference_direction(peers):
 _,s=peers
 a=s["ada"]
 entry=execute(a,"write_driver_note_entry",{"driver_id":"ada","assertion":"Investigate current task","body":"private draft","director_identity":"ada/sub"},allow_write=True)
 assert entry["address"].startswith("dnote://ada/") and entry["force"]=="informational"
 got=execute(s["bob"],"get_driver_note_entry",{"driver_id":"ada","entry_id":entry["entry_id"]})
 assert got["body"]=="private draft"
 with pytest.raises(StateGraphError,match="not_notebook_owner"):
  execute(s["bob"],"write_driver_note_entry",{"driver_id":"ada","assertion":"foreign","body":"bad","director_identity":"bob"},allow_write=True)
 with pytest.raises(StateGraphError,match="private_notebook_reference"):
  a.driver_notes.write_entry("alpha","A rule","ref "+entry["address"],"ada")
 with a.store.transaction() as conn:
  with pytest.raises(sqlite3.IntegrityError):
   conn.execute("UPDATE driver_note_entries SET body='overwrite' WHERE driver_id='ada'")
 assert len(a.list_driver_notebooks()["notebooks"])==3
 with pytest.raises(Exception):
  execute(a,"driver_note_index",{"project_id":"alpha","driver_id":"ada"})
 before=a.store.events("alpha")
 execute(a,"delist_driver_note_entry",{"driver_id":"ada","entry_id":entry["entry_id"],"reason":"done","director_identity":"ada"},allow_write=True)
 assert a.store.events("alpha")==before
 assert execute(a,"get_driver_note_entry",{"driver_id":"ada","entry_id":entry["entry_id"]})["listing"]=="delisted"

def test_project_distinct_acks_and_transient_quorum(peers):
 _,s=peers
 result=send(s["ada"],ack_quorum=2)["result"];d=result["deliveries"][0]
 b=s["bob"].director_messages.acknowledge_director_message("beta",d["delivery_id"],1,"bob-ack")
 assert b["result"]["delivery"]["status"]=="acknowledged"
 replay=s["bob"].director_messages.acknowledge_director_message("beta",d["delivery_id"],1,"bob-again")
 assert replay["result"]["replayed"] and replay["result"]["delivery"]["version"]==2
 with pytest.raises(DirectorMessageError,match="version_conflict"):
  s["carol"].director_messages.acknowledge_director_message("beta",d["delivery_id"],1,"stale")
 c=s["carol"].director_messages.acknowledge_director_message("beta",d["delivery_id"],2,"carol-ack")
 assert c["result"]["delivery"]["status"]=="resolved"
 listed=s["bob"].director_messages.list_director_messages("beta",protocol_version="v3")["result"]["items"][0]
 assert listed["acked_count"]==2 and listed["acked_by_me"]
 assert {a["driver_id"] for a in listed["acks"]}=={"bob","carol"}

def test_broadcast_snapshot_removal_and_standing_lifecycle(peers):
 reg,s=peers
 d=send(s["ada"],ack_mode="broadcast",delivery_mode="standing")["result"]["deliveries"][0]
 s["bob"].director_messages.acknowledge_director_message("beta",d["delivery_id"],1,"ack")
 pending=s["carol"].director_messages.list_director_messages("beta",needs_my_ack=True)["result"]["items"]
 assert len(pending)==1 and pending[0]["pending_drivers"]==["carol"]
 reg.set_membership("beta","carol","removed",1,"remove",actor="test")
 item=s["bob"].director_messages.list_director_messages("beta",protocol_version="v3")["result"]["items"][0]
 assert item["delivery"]["status"]=="acknowledged" and item["quorum_met"]
 assert "notice" in s["bob"].director_messages.project_active_standing("beta")
 s["bob"].director_messages.resolve_director_message("beta",d["delivery_id"],item["delivery"]["version"],"resolve")
 assert not s["bob"].director_messages.project_active_standing("beta")

def test_quorum_unreachable_and_same_project_error(peers):
 _,s=peers
 result=send(s["ada"],ack_quorum=3)
 assert result["code"]=="ack_quorum_unreachable" and result["schema"].endswith("v3")
 result=send(s["ada"],target_project_id="alpha")
 assert result["code"]=="use_driver_inbox"
 assert s["bob"].director_messages.list_director_messages("beta",protocol_version="v3")["result"]["items"]==[]

def test_migration_preserves_existing_status_version_events_and_idempotency(tmp_path):
 import subprocess,importlib.util
 from core.state_graph import StateGraphStore
 root=__import__("pathlib").Path(__file__).resolve().parents[2]
 oldfile=tmp_path/"original_mailbox.py"
 oldfile.write_bytes(subprocess.check_output(["git","show","100a1886f435c3da76aab2bcf2f28c46649bb037:core/director_messaging.py"],cwd=root))
 spec=importlib.util.spec_from_file_location("p2_original_mailbox",oldfile)
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 db=StateDatabase(str(tmp_path/"migration.sqlite"))
 store=StateGraphStore(db,project_read_trusted=True)
 store.create_project("alpha","alpha");store.create_project("beta","beta")
 old=module.SQLiteDirectorMessaging(store,"old",project_read_trusted=True)
 message=old.send_director_message("alpha","old","legacy","subject","body",target_project_id="beta")["result"]
 did=message["deliveries"][0]["delivery_id"]
 old.acknowledge_director_message("beta",did,1,"ack")
 with db.get_connection() as conn:
  assert "ack_mode" not in {r["name"] for r in conn.execute("PRAGMA table_info(state_director_messages)")}
  statuses=[tuple(r) for r in conn.execute("SELECT * FROM state_director_deliveries")]
  events=[tuple(r) for r in conn.execute("SELECT * FROM state_events")]
  keys=[tuple(r) for r in conn.execute("SELECT * FROM state_director_idempotency")]
 from core.director_messaging import initialize
 initialize(db);initialize(db)
 with db.get_connection() as conn:
  assert [tuple(r) for r in conn.execute("SELECT * FROM state_director_deliveries")]==statuses
  assert [tuple(r) for r in conn.execute("SELECT * FROM state_events")]==events
  assert [tuple(r) for r in conn.execute("SELECT * FROM state_director_idempotency")]==keys
  assert not conn.execute("SELECT * FROM state_director_delivery_acks").fetchall()
  assert conn.execute("SELECT delivery_id FROM state_director_legacy_acks").fetchone()[0]==did
  assert conn.execute("SELECT ack_mode,ack_quorum FROM state_director_messages").fetchone()[0]=="at_least_n"


def test_postcompact_own_standing_and_unacked_broadcast_transient(peers):
 _,s=peers
 send(s["ada"],ack_mode="broadcast")
 s["ada"].driver_inbox.send_driver_message("private","private notice","body",target_driver_id="bob",delivery_mode="standing")
 before=s["bob"].driver_postcompact_guidance("beta")
 assert "notice" in before["projection"] and "private notice" in before["projection"]
 assert before["included"]<=8 and len(before["projection"])<=3000
 item=s["bob"].director_messages.list_director_messages("beta",protocol_version="v3")["result"]["items"][0]
 s["bob"].director_messages.acknowledge_director_message("beta",item["delivery"]["delivery_id"],1,"ack")
 after=s["bob"].driver_postcompact_guidance("beta")
 assert "private notice" in after["projection"] and '"source": "project_inbox"' not in after["projection"]



def test_v2_wire_projection_and_v3_closed_schema(peers):
 from pathlib import Path
 from jsonschema import Draft202012Validator
 _,s=peers;root=Path(__file__).resolve().parents[2]
 v2=Draft202012Validator(json.loads((root/"contracts/director_messaging/v2/schema.json").read_text()))
 v3=Draft202012Validator(json.loads((root/"contracts/director_messaging/v3/schema.json").read_text()))
 legacy=s["ada"].director_messages.send_director_message("alpha","ada","v2-wire","subject","",target_project_id="beta")
 v2.validate(legacy)
 did=legacy["result"]["deliveries"][0]["delivery_id"]
 ack=s["bob"].director_messages.acknowledge_director_message("beta",did,1,"ack-wire")
 v2.validate(ack);assert ack["result"]["delivery"]["status"]=="resolved"
 v2.validate(s["bob"].director_messages.resolve_director_message("beta",did,2,"resolve-wire"))
 v2.validate(s["bob"].director_messages.list_director_messages("beta"))
 rich=send(s["ada"],request_key="rich",ack_mode="broadcast")
 v3.validate(rich)
 listing=s["bob"].director_messages.list_director_messages("beta",protocol_version="v3")
 v3.validate(listing)
 did=rich["result"]["deliveries"][0]["delivery_id"]
 rich_ack=s["bob"].director_messages.acknowledge_director_message("beta",did,1,"rich-ack",protocol_version="v3")
 v3.validate(rich_ack)
 broken=copy.deepcopy(rich_ack);broken["schema"]="aitelier.director-messaging.v2"
 assert not v3.is_valid(broken) and not v2.is_valid(broken)

def test_bounded_projection_and_private_notebook_does_not_enter_guidance(peers):
 _,s=peers
 for i in range(12):
  s["ada"].driver_inbox.send_driver_message("msg"+str(i),"standing"+str(i),"body"*100,target_driver_id="bob",delivery_mode="standing")
 execute(s["bob"],"write_driver_note_entry",{"driver_id":"bob","assertion":"Private rule","body":"NEVER_IN_GUIDANCE","force":"in_force","director_identity":"bob"},allow_write=True)
 result=s["bob"].driver_postcompact_guidance("beta")
 assert result["included"]<=8 and len(result["projection"])<=3000
 assert result["omitted"]==12-result["included"]
 assert "NEVER_IN_GUIDANCE" not in result["projection"]

def test_broadcast_transient_only_finishes_after_every_snapshot_member(peers):
 _,s=peers
 d=send(s["ada"],ack_mode="broadcast")["result"]["deliveries"][0]
 first=s["bob"].director_messages.acknowledge_director_message("beta",d["delivery_id"],1,"first",protocol_version="v3")
 assert first["result"]["delivery"]["status"]=="acknowledged"
 assert first["result"]["pending_drivers"]==["carol"]
 last=s["carol"].director_messages.acknowledge_director_message("beta",d["delivery_id"],2,"last",protocol_version="v3")
 assert last["result"]["delivery"]["status"]=="resolved"
 with s["ada"].store.transaction() as conn:
  with pytest.raises(sqlite3.IntegrityError):
   conn.execute("UPDATE state_director_delivery_acks SET acked_at='changed' WHERE delivery_id=?",(d["delivery_id"],))
  with pytest.raises(sqlite3.IntegrityError):
   conn.execute("DELETE FROM state_director_delivery_acks WHERE delivery_id=?",(d["delivery_id"],))

def test_driver_argument_contract_and_private_read_refusal(peers):
 from core.state_commands import describe
 _,s=peers
 operation=describe()["operations"]["write_driver_note_entry"]
 assert operation["arguments"]["properties"]["force"]["enum"]==["in_force"]
 assert operation["driver_arguments"]["properties"]["force"]["default"]=="informational"
 anonymous=StateService(s["ada"].db,actor="anonymous",project_read_trusted=False)
 with pytest.raises(Exception):
  execute(anonymous,"driver_note_index",{"driver_id":"ada"})
 with pytest.raises(Exception):
  anonymous.list_driver_notebooks()

def test_private_notebook_all_entry_operations_reuse_same_owner(peers):
 _,s=peers;a=s["ada"]
 project=a.driver_notes.write_entry("alpha","Project rule","body","ada")
 first=execute(a,"write_driver_note_entry",{"driver_id":"ada","assertion":"Old draft","body":"ref "+project["address"],"director_identity":"ada"},allow_write=True)
 newer=execute(a,"supersede_driver_note_entry",{"driver_id":"ada","entry_id":first["entry_id"],"assertion":"New draft","body":"ref "+first["address"],"reason":"replace","director_identity":"ada","force":"in_force"},allow_write=True)
 successor=newer["successor"]
 assert successor["address"].startswith("dnote://ada/")
 assert newer["superseded"]["superseded_by"]==successor["address"]
 index=execute(s["bob"],"driver_note_index",{"driver_id":"ada"})
 assert index["superseded_count"]==1 and len(index["entries"])==1
 searched=execute(s["bob"],"search_driver_note_entries",{"driver_id":"ada","query":"New draft"})
 assert len(searched["entries"])==1
 checked=execute(a,"check_driver_note_index",{"driver_id":"ada"})
 assert checked["ok"]
 with pytest.raises(StateGraphError,match="do not resolve"):
  execute(a,"write_driver_note_entry",{"driver_id":"ada","assertion":"Unknown ref","body":"issue://absent","director_identity":"ada"},allow_write=True)

def test_real_concurrent_project_acks_have_one_cas_winner(peers):
 from concurrent.futures import ThreadPoolExecutor
 import threading
 _,s=peers
 d=send(s["ada"],ack_mode="broadcast")["result"]["deliveries"][0]
 barrier=threading.Barrier(2)
 def run(name):
  barrier.wait()
  try:
   return s[name].director_messages.acknowledge_director_message("beta",d["delivery_id"],1,"parallel-"+name,protocol_version="v3")
  except DirectorMessageError as exc:
   return exc.code
 with ThreadPoolExecutor(max_workers=2) as pool:
  results=list(pool.map(run,["bob","carol"]))
 assert sum(isinstance(x,dict) for x in results)==1 and results.count("version_conflict")==1
 item=s["bob"].director_messages.list_director_messages("beta",protocol_version="v3")["result"]["items"][0]
 assert item["acked_count"]==1 and len(item["pending_drivers"])==1
