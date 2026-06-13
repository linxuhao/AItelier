# File: tests/test_api_sse.py

import pytest
import json
from httpx import AsyncClient, ASGITransport
from api.main import app
from api.sse_manager import stream_manager

@pytest.mark.asyncio
async def test_sse_stream_pipeline():
    """测试 SSE 广播管道的推送与接收闭环（采用优雅停机）"""
    task_id = "test_sse_task_999"

    # Bypass localhost middleware for direct ASGI testing
    app.state._test_mode = True

    # 预先推送数据，并在末尾追加 "__END__" 死亡药丸信号
    await stream_manager.push_log(task_id, "Initializing sandbox...")
    await stream_manager.push_log(task_id, "Running tool via mise...")
    await stream_manager.push_log(task_id, "Execution completed.")
    await stream_manager.push_log(task_id, "__END__")  # <- 释放阻塞的关键

    # 使用 ASGITransport 直接测试 FastAPI 应用
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 建立异步流式请求连接
        async with client.stream("GET", f"/api/tasks/{task_id}/stream") as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

            logs_received = []
            # 我们不再使用 break 强行打断，而是让 async for 自然枯竭 (耗尽)
            # 因为服务器端碰到 __END__ 后会停止 yield，关闭 HTTP 流
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    payload = json.loads(line[6:])
                    logs_received.append(payload["log"])

    # 断言接收内容与顺序
    assert len(logs_received) == 3
    assert logs_received[0] == "Initializing sandbox..."
    assert logs_received[1] == "Running tool via mise..."
    assert logs_received[2] == "Execution completed."

    # 断言服务端 finally 清理逻辑已生效
    assert task_id not in stream_manager._queues

    app.state._test_mode = False