"""Step 24.2 (design-steward 7.8.12, 7.8.12a, 7.8.18 "媒体"): the backend's part of the
steward's media - attachment METADATA on the relayed source rows (never a byte or a name),
the read-only binding check storeMedia asks before it fetches anything, a group's send_media
file held to group_send_budget with the confirmations, the gateway's /fetch with its
tightened rules, the media root the DSH host gives every driver process, and the one
media-types table the host's JS and this Python both read."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from api import steward_gateway as gw
from core import media_types, state_channels, state_groups
from core.state_commands import READ_REQUESTS, STEWARD_WRITE_ACTIONS, WRITER_ONLY_READS, execute
from core.state_graph import StateGraphError, canonical
from tests.unit.test_steward_connections import FakeUpstream, add, discover, allow, rpc, body_of, server, world  # noqa: F401
from tests.unit.test_steward_group_confirm import ROOM, MEMBER, OWNER_WX, gask, group_turn, out_rows
from tests.unit.test_steward_oauth_confirm import bind_slack, relay

REPO = Path(__file__).resolve().parents[2]
REF = "m_0123456789abcdef"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452") + b"\x00" * 200


def relay_with(world, body, ref, attachments, sender="U0OWNER", channel="slack", key=None):
    return execute(world.steward, "relay_owner_message", {
        "request_key": key or f"relay-{ref}", "channel": channel, "sender_ref": sender, "channel_message_ref": ref,
        "body": body, "channel_at": state_channels._now().isoformat(),
        **({"attachments": attachments} if attachments is not None else {})}, allow_write=True)


def source_row(world, message_id):
    with world.db.get_connection() as conn:
        return dict(conn.execute("SELECT * FROM state_message_sources WHERE message_id=?", (message_id,)).fetchone())


# --- attachment metadata on the owner's relayed message --------------------------------------

def test_an_attachments_only_message_carries_the_placeholder_body_and_its_metadata(world, monkeypatch):
    bind_slack(world, monkeypatch)
    files = [{"kind": "image", "ref": REF, "mime": "image/jpeg", "size": 1234},
             {"kind": "voice", "refused": "too_large"}]
    answer = relay_with(world, "[图片]\n[语音 过大未收]", "c1", files)
    assert answer["message_id"] and not answer["duplicate"]
    row = source_row(world, answer["message_id"])
    assert json.loads(row["attachments_json"]) == files
    sources = execute(world.steward, "message_sources", {"message_ids": [answer["message_id"]]})["sources"]
    assert sources[answer["message_id"]]["attachments"] == files
    # The same channel message again (any key) is the same message.
    again = relay_with(world, "[图片]\n[语音 过大未收]", "c1", files, key="other-key")
    assert again["duplicate"] and again["message_id"] == answer["message_id"]
    # The empty_body rule is unchanged: no text and no placeholder is still refused.
    with pytest.raises(StateGraphError, match="empty_body"):
        relay_with(world, " ", "c2", files)


def test_a_message_without_attachments_is_answered_and_stored_exactly_as_before(world, monkeypatch):
    bind_slack(world, monkeypatch)
    answer = relay(world, "你好", "c3")
    assert source_row(world, answer["message_id"])["attachments_json"] is None
    source = execute(world.steward, "message_sources", {"message_ids": [answer["message_id"]]})["sources"][answer["message_id"]]
    assert "attachments" not in source
    assert set(source) == {"direction", "channel", "sender_ref", "channel_message_ref", "at", "error", "channel_at"}
    # The request hash of a request without attachments is the one before step 24.2 (pinned
    # independently of the code that computes it): a request relayed before the deploy and
    # replayed after it is a duplicate, not an idempotency conflict.
    with world.db.get_connection() as conn:
        stored = [r[0] for r in conn.execute("SELECT payload_json FROM driver_inbox_requests WHERE operation=? AND request_key=?",
                                             ("relay_owner_message", "relay-c3"))]
    pre_24_2 = hashlib.sha256(canonical(["slack", "U0OWNER", "c3", "你好"]).encode("utf-8")).hexdigest()
    assert stored == [pre_24_2]
    assert relay(world, "你好", "c3")["duplicate"] is True


@pytest.mark.parametrize("bad", [
    [{"kind": "image", "ref": REF, "name": "IMG_0001.jpg"}],            # a name is never kept
    [{"kind": "image", "ref": REF, "refused": "type"}],                  # both
    [{"kind": "image"}],                                                 # neither
    [{"kind": "image", "refused": "channel_sender_not_bound"}],          # outside the closed set
    [{"kind": "video", "ref": REF}],
    [{"kind": "image", "ref": "m_123"}],
    [{"kind": "image", "ref": REF, "size": -1}],
    [{"kind": "image", "ref": REF, "mime": "Image/JPEG; x"}],
    [{"kind": "image", "refused": "gone"}] * 11,
])
def test_attachments_outside_the_closed_set_refuse_the_whole_message(world, monkeypatch, bad):
    bind_slack(world, monkeypatch)
    with pytest.raises(StateGraphError, match="invalid_attachments|invalid_request|validation"):
        relay_with(world, "[图片]", "c-bad", bad)
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_message_sources WHERE channel_message_ref='c-bad'").fetchone()[0] == 0


def test_a_stranger_with_attachments_is_refused_like_any_stranger(world, monkeypatch):
    bind_slack(world, monkeypatch)
    with pytest.raises(StateGraphError, match="channel_sender_not_bound"):
        relay_with(world, "[图片]", "c4", [{"kind": "image", "ref": REF}], sender="U0STRANGER")


# --- the read-only binding check storeMedia asks first --------------------------------------

def test_the_sender_bound_read_answers_the_relays_own_judgement(world, monkeypatch):
    assert execute(world.steward, "owner_channel_sender_bound", {"channel": "slack", "sender_ref": "U0OWNER"}) == {"bound": False}
    bind_slack(world, monkeypatch)
    assert execute(world.steward, "owner_channel_sender_bound", {"channel": "slack", "sender_ref": "U0OWNER"}) == {"bound": True}
    assert execute(world.steward, "owner_channel_sender_bound", {"channel": "slack", "sender_ref": "U0OTHER"}) == {"bound": False}
    assert execute(world.steward, "owner_channel_sender_bound", {"channel": "wechat", "sender_ref": "U0OWNER"}) == {"bound": False}
    assert "owner_channel_sender_bound" in READ_REQUESTS and "owner_channel_sender_bound" in WRITER_ONLY_READS
    # A director is neither the owner nor the steward: refused.
    with pytest.raises(StateGraphError):
        execute(world.director, "owner_channel_sender_bound", {"channel": "slack", "sender_ref": "U0OWNER"})
    # It writes nothing (a read).
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_message_sources").fetchone()[0] == 0


# --- the family group: attachments on a question, and send_media's files in the budget -------

def group_ready_plain(world, monkeypatch):
    bind_slack(world, monkeypatch, sender=OWNER_WX, channel="wechat")
    code = world.owner.groups.issue_group_code("wechat", "家庭群")["code"]
    execute(world.steward, "bind_owner_group", {"request_key": "bg-1", "channel": "wechat", "group_ref": ROOM,
                                                "sender_ref": OWNER_WX, "code": code, "mention_prefix": "@管家"},
            allow_write=True)


def post_media(world, turn="gt1", kind="image", caption=None, key=None):
    return execute(world.steward, "post_group_media", {"request_key": key or f"gm-{turn}-{kind}-{caption}",
                   "turn": turn, "kind": kind, **({"caption": caption} if caption is not None else {})}, allow_write=True)


def test_a_group_question_keeps_its_attachments_metadata(world, monkeypatch):
    group_ready_plain(world, monkeypatch)
    files = [{"kind": "file", "refused": "unsupported"}]
    answer = execute(world.steward, "relay_group_message", {
        "request_key": "g-a1", "channel": "wechat", "group_ref": ROOM, "sender_ref": MEMBER,
        "channel_message_ref": "a1", "body": "[文件 未收：渠道不支持]", "channel_at": state_channels._now().isoformat(),
        "attachments": files}, allow_write=True)
    with world.db.get_connection() as conn:
        stored = conn.execute("SELECT attachments_json FROM state_group_messages WHERE message_id=?",
                              (answer["message_id"],)).fetchone()[0]
    assert json.loads(stored) == files
    plain = gask(world, "a2", "问个问题")
    with world.db.get_connection() as conn:
        assert conn.execute("SELECT attachments_json FROM state_group_messages WHERE message_id=?",
                            (plain["message_id"],)).fetchone()[0] is None


def test_send_media_files_share_the_groups_twenty_sends_with_the_confirmations(world, monkeypatch):
    group_ready_plain(world, monkeypatch)
    q = gask(world, "q1")["message_id"]
    group_turn(world)
    assert set(state_groups.GROUP_SEND_KINDS) == {"confirmation", "confirmation_line", "media"}
    first = post_media(world, caption="一只猫")
    assert first["posted"] and first["channel"] == "wechat" and first["group_ref"] == ROOM and first["caption"] == "一只猫"
    # Replayed by its key: the same answer, no second row.
    assert post_media(world, caption="一只猫")["message_id"] == first["message_id"]
    assert out_rows(world, "media") == [("media", "一只猫", "gm-gt1-image-一只猫")]
    # 18 confirmation rows planted this hour + the media row = 19; one more media file is the 20th.
    moment = state_channels._now().isoformat()
    with world.db.get_connection() as conn:
        for n in range(18):
            conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,body,at) "
                         "VALUES(?,'wechat',?,'out','confirmation',1,'x',?)", (f"c{n}", ROOM, moment))
        conn.commit()
    assert post_media(world, kind="voice")["posted"]
    over = post_media(world, kind="file")
    assert over == {"refused": "group_send_budget"}
    assert len(out_rows(world, "media")) == 2
    del q


def test_a_group_files_caption_is_exit_checked_and_only_the_recorded_turns_group_gets_it(world, monkeypatch):
    group_ready_plain(world, monkeypatch)
    gask(world, "q1")
    group_turn(world)
    held = post_media(world, caption="见 /home/example/secret.png")
    assert held == {"refused": "withheld", "reason": "host_path"}
    assert out_rows(world, "media") == []
    with pytest.raises(StateGraphError, match="media_group_turn_only"):
        post_media(world, turn="not-the-recorded-turn")
    execute(world.steward, "set_steward_turn_audience", {"turn": "t-owner", "audience": "owner"}, allow_write=True)
    with pytest.raises(StateGraphError, match="media_group_turn_only"):
        post_media(world, turn="t-owner")
    assert "post_group_media" in STEWARD_WRITE_ACTIONS
    with pytest.raises(StateGraphError):
        execute(world.director, "post_group_media", {"request_key": "x", "turn": "gt1", "kind": "image"}, allow_write=True)


# --- the one media-types table ---------------------------------------------------------------

def test_python_reads_the_same_media_types_table_as_the_host():
    table = json.loads((REPO / "integrations/dsh-director/media-types.json").read_text())
    assert media_types.table() == table
    for sample in table["samples"]:
        found = media_types.sniff(bytes.fromhex(sample["hex"]))
        assert (found or {}).get("mime") == sample["mime"], sample
    # The JS library reads the very same file (lib/media.mjs: new URL('../media-types.json')).
    source = (REPO / "integrations/dsh-director/lib/media.mjs").read_text()
    assert "new URL('../media-types.json', import.meta.url)" in source
    assert media_types.limits()["outbound_bytes"] == 25 * 1024 * 1024
    assert media_types.limits()["image_pixels"] == 40_000_000
    # No office document, archive or executable type in v1.
    assert not {"application/zip", "application/msword", "application/x-msdownload"} & {t["mime"] for t in table["types"]}


def test_the_js_sniff_agrees_with_the_python_sniff_on_every_sample():
    node = subprocess.run(["node", "-e", "import('./integrations/dsh-director/lib/media.mjs').then(m => { const t = "
                           "JSON.parse(require('fs').readFileSync('integrations/dsh-director/media-types.json','utf8'));"
                           "console.log(JSON.stringify(t.samples.map(s => m.sniff(Buffer.from(s.hex,'hex'))?.mime ?? null))); })"],
                          cwd=REPO, capture_output=True, text=True, timeout=60)
    if node.returncode != 0 and "not found" in (node.stderr or ""):
        pytest.skip("node is not installed")
    assert node.returncode == 0, node.stderr
    table = media_types.table()
    assert json.loads(node.stdout) == [(media_types.sniff(bytes.fromhex(s["hex"])) or {}).get("mime") for s in table["samples"]]


# --- the media root the DSH host gives every driver process ----------------------------------

def test_every_driver_process_gets_the_media_root(tmp_path, monkeypatch):
    import sys
    from core.dsh_driver import DshDrivers, media_root
    from core.drivers import DriverRegistry
    from core.state_database import StateDatabase
    from core.state_service import StateService
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    StateService(db, actor="fixture", project_read_trusted=True).store.create_project("alpha", "Alpha")
    StateService(db, actor="fixture", project_read_trusted=True).store.create_project("aitelier-home", "Home")
    registry = DriverRegistry(db, "media-root-pepper-" * 3)
    registry.seed("media-root-owner")
    runtime = DshDrivers(db, registry, tmp_path / "drivers", command=[sys.executable, "-c", "pass"])
    monkeypatch.delenv("AITELIER_MEDIA_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    expected = str(tmp_path / "home" / ".local/share/dsh-director/media")
    assert media_root() == expected
    director = runtime._provision("alpha", "aitelier", "driver", "fixture")
    steward = runtime._provision("aitelier-home", "aitelier", "driver", "fixture")
    assert runtime._environment(director)["AITELIER_MEDIA_ROOT"] == expected
    env = runtime._environment(steward)
    assert env["AITELIER_MEDIA_ROOT"] == expected
    assert env["AITELIER_STEWARD_GATEWAY_URL"] == runtime.steward_gateway_url
    # Not the driver's own ~ (DSH_HOME lives under the drivers' directory).
    assert not env["AITELIER_MEDIA_ROOT"].startswith(env["DSH_HOME"])
    monkeypatch.setenv("AITELIER_MEDIA_ROOT", str(tmp_path / "elsewhere"))
    assert runtime._environment(director)["AITELIER_MEDIA_ROOT"] == str(tmp_path / "elsewhere")
    monkeypatch.setenv("AITELIER_MEDIA_ROOT", "relative/path")
    assert runtime._environment(director)["AITELIER_MEDIA_ROOT"] == expected


# --- the gateway's /fetch ---------------------------------------------------------------------

class MediaUpstream(FakeUpstream):
    """The fake MCP server with agentmcp-like file routes: /files/<name> answers bytes, two
    redirects (one to its own origin, one to another), a big file and a zip."""

    def __init__(self):
        super().__init__()
        self.file_headers: list[dict] = []
        self.app.router.routes.extend([
            Route("/files/cat.png", self.png), Route("/files/big.png", self.big), Route("/files/doc.zip", self.zip),
            Route("/files/here", lambda r: RedirectResponse("/files/cat.png", status_code=302)),
            Route("/files/away", lambda r: RedirectResponse("http://example.com/files/cat.png", status_code=302)),
            Route("/files/badport", lambda r: RedirectResponse("http://mcp_server:99999/files/cat.png", status_code=302)),
            Route("/files/badhost", lambda r: Response(status_code=302, headers={"location": "http://[::1/files/cat.png"})),
        ])

    async def png(self, request):
        self.file_headers.append(dict(request.headers))
        return Response(PNG, media_type="application/octet-stream")

    async def big(self, request):
        return Response(PNG + b"\x00" * (25 * 1024 * 1024), media_type="image/png")

    async def zip(self, request):
        return Response(bytes.fromhex("504b0304140006000800") + b"\x00" * 100, media_type="image/png")


@pytest.fixture
def upstream():
    return MediaUpstream()


def fetch(server, world, url, conn_id="media", who="steward"):
    return httpx.get(f"{server.url}{gw.PATH}/{conn_id}/fetch", params={"url": url},
                     headers={"X-AItelier-Driver-Token": world.tokens[who]}, timeout=60)


def answer_with(upstream, *urls):
    upstream.call_answer = {"result": {"content": [{"type": "text", "text": "generated: " + " ".join(urls)}]}}


def media_ready(world, server, upstream, *urls, visibility="public", turn="t1"):
    add(world, conn_id="media", visibility=visibility)
    discover(world, "media")
    allow(world, "media", names=["web_search", "ocr_image"])
    execute(world.steward, "set_steward_turn_audience", {"turn": turn, "audience": "owner"}, allow_write=True)
    answer_with(upstream, *urls)
    called = body_of(rpc(server, world, "tools/call", {"name": "media__web_search", "arguments": {"query": "猫"}}))[0]
    assert "result" in called, called


CAT = "http://mcp_server:9003/files/cat.png?sig=abc&exp=1"


def test_fetch_gives_a_file_a_result_named_with_its_own_type_and_no_credentials(world, server, upstream):
    media_ready(world, server, upstream, CAT)
    got = fetch(server, world, CAT)
    assert got.status_code == 200, got.text
    assert got.content == PNG
    assert got.headers["content-type"] == "image/png"        # the first bytes, not the upstream's octet-stream
    assert got.headers["x-aitelier-media-kind"] == "image"
    sent = upstream.file_headers[-1]
    assert not {"authorization", "cookie", "x-aitelier-driver-token"} & {k.lower() for k in sent}


def test_fetch_refuses_a_url_not_seen_in_this_turn_another_origin_or_connection_and_any_other_token(world, server, upstream):
    media_ready(world, server, upstream, CAT, "http://mcp_server:9003/files/here", "http://mcp_server:9003/files/away",
                "http://mcp_server:9003/files/big.png", "http://mcp_server:9003/files/doc.zip", "http://example.com/files/cat.png")
    assert fetch(server, world, "http://mcp_server:9003/files/cat.png").json()["detail"] == "url_not_seen"     # not verbatim
    assert fetch(server, world, CAT, conn_id="mailpub").status_code == 404
    assert fetch(server, world, "http://example.com/files/cat.png").json()["detail"] == "other_origin"
    assert fetch(server, world, "http://mcp_server:9003/files/here").content == PNG                          # same origin: followed
    assert fetch(server, world, "http://mcp_server:9003/files/away").json()["detail"] == "redirect_other_origin"
    big = fetch(server, world, "http://mcp_server:9003/files/big.png")
    assert (big.status_code, big.json()["detail"]) == (413, "too_large")
    zipped = fetch(server, world, "http://mcp_server:9003/files/doc.zip")
    assert (zipped.status_code, zipped.json()["detail"]) == (415, "type")
    assert fetch(server, world, CAT, who="director").status_code == 403
    # A new turn: what the last turn's results named is not this turn's.
    execute(world.steward, "set_steward_turn_audience", {"turn": "t2", "audience": "owner"}, allow_write=True)
    assert fetch(server, world, CAT).json()["detail"] == "url_not_seen"


def test_fetch_refuses_a_malformed_url_or_redirect_location_as_a_bad_url_not_a_500(world, server, upstream):
    """Review F1: a url a result named that urlsplit cannot take (port out of range, an unclosed
    [ ) - asked for, or met as a redirect's Location - is a JSON refusal, never a 500."""
    bad = ["http://mcp_server:99999/files/cat.png", "http://[::1/files/cat.png"]
    media_ready(world, server, upstream, *bad, "http://mcp_server:9003/files/badport", "http://mcp_server:9003/files/badhost")
    for url in bad:
        got = fetch(server, world, url)
        assert (got.status_code, got.json()["detail"]) == (400, "bad_url"), (url, got.status_code, got.text)
    got = fetch(server, world, "http://mcp_server:9003/files/badport")
    assert (got.status_code, got.json()["detail"]) == (502, "redirect_bad_url"), (got.status_code, got.text)
    # An unclosed [ in the Location: httpx itself refuses the response (it parses the Location even
    # without following it) - an upstream error, still a JSON refusal.
    got = fetch(server, world, "http://mcp_server:9003/files/badhost")
    assert (got.status_code, got.json()["detail"]) == (502, "upstream_unavailable"), (got.status_code, got.text)


def test_fetch_from_a_private_connection_only_in_an_owner_turn(world, server, upstream):
    media_ready(world, server, upstream, CAT, visibility="private")
    assert fetch(server, world, CAT).status_code == 200
    execute(world.steward, "set_steward_turn_audience", {"turn": "t1", "audience": "group",
            "group": {"channel": "wechat", "group_ref": "room1"}}, allow_write=True)
    assert fetch(server, world, CAT).json()["detail"] == "not_visible_in_this_turn"


def test_fetch_is_on_the_gateways_port_alone(world, server, upstream):
    media_ready(world, server, upstream, CAT)
    other = httpx.get(f"{server.main_url}{gw.PATH}/media/fetch", params={"url": CAT},
                      headers={"X-AItelier-Driver-Token": world.tokens["steward"]}, timeout=30)
    assert other.status_code == 404
