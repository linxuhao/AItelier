# File: api/sse_manager.py

import asyncio
import json
from typing import Dict, AsyncGenerator

class StreamManager:
    """
    异步事件流广播层。
    管理各个任务的日志队列，用于向前端 GUI 提供 Server-Sent Events (SSE) 推送。
    """
    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}

    def _get_queue(self, task_id: str) -> asyncio.Queue:
        if task_id not in self._queues:
            self._queues[task_id] = asyncio.Queue()
        return self._queues[task_id]

    async def push_log(self, task_id: str, message: str):
        """引擎后端调用：将底层执行日志推入指定任务的队列"""
        queue = self._get_queue(task_id)
        await queue.put(message)

    async def event_generator(self, task_id: str) -> AsyncGenerator[str, None]:
        """前端客户端调用：作为异步生成器持续消费队列数据"""
        queue = self._get_queue(task_id)
        try:
            while True:
                # 挂起等待新日志
                message = await queue.get()
                
                # 接收到终结信号，主动关闭流式连接并退出
                if message == "__END__":
                    break
                    
                payload = {"log": message}
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            # 无论是由于 "__END__" 正常退出，还是客户端意外断开引发 CancelledError
            # finally 块都能确保资源被绝对清理，防止内存泄漏
            if task_id in self._queues:
                del self._queues[task_id]

# 全局单例
stream_manager = StreamManager()