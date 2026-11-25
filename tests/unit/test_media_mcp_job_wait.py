"""A media tool that answers with a pending job id is waited on until its link comes back."""

from aitelier import mcp_client


def _server(monkeypatch, replies):
    calls = []

    def rpc(method, params=None, notify=False):
        calls.append((params["name"], params["arguments"]))
        return {"result": {"content": [{"type": "text", "text": replies.pop(0)}]}}

    monkeypatch.setattr(mcp_client, "_connect", lambda: None)
    monkeypatch.setattr(mcp_client, "_rpc", rpc)
    return calls


PENDING = ("图片生成还没完 (状态: running), job_id=ab12。任务在生成服务上继续跑, 结果不会丢: "
           "调 media_job_wait(job_id='ab12') 接着等, 每次最多等 120 秒左右, 没完就再等一次。")


def test_a_pending_job_is_waited_on_until_the_link_arrives(monkeypatch):
    calls = _server(monkeypatch, [PENDING, PENDING, "图片已生成: https://m.test/files/ab12.png"])
    reply = mcp_client.call_tool("generate_image", {"prompt": "a cat"})
    assert mcp_client.urls_in(reply) == ["https://m.test/files/ab12.png"]
    assert calls == [("generate_image", {"prompt": "a cat"}),
                     ("media_job_wait", {"job_id": "ab12"}),
                     ("media_job_wait", {"job_id": "ab12"})]


def test_a_finished_reply_is_one_call(monkeypatch):
    calls = _server(monkeypatch, ["图片已生成: https://m.test/files/x.png"])
    mcp_client.call_tool("generate_image", {"prompt": "a cat"})
    assert len(calls) == 1


def test_the_wait_stops_at_the_deadline(monkeypatch):
    calls = _server(monkeypatch, [PENDING] * 5)
    clock = iter([0.0, 0.0, 1000.0, 2000.0])
    monkeypatch.setattr(mcp_client.time, "monotonic", lambda: next(clock))
    reply = mcp_client.call_tool("generate_image", {"prompt": "a cat"})
    assert "job_id=ab12" in reply and mcp_client.urls_in(reply) == []
    assert [c[0] for c in calls] == ["generate_image", "media_job_wait", "media_job_wait"]
