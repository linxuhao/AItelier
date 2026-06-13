# File: core/tool_executor.py

import os
import signal
import subprocess
from pathlib import Path

class SecureToolRunner:
    """
    工具子进程驱动层。
    负责在指定沙盒内安全地执行 CLI 命令，包含跨语言垫片注入与硬超时进程树收割机制。
    """
    
    def run_cmd(self, workspace: Path, cmd: list[str], timeout: int = 60,
                use_mise: bool = True, env: dict | None = None) -> dict:
        """
        执行沙盒命令并捕获标准输出与标准错误。
        :param workspace: 隔离的工作区路径 (需确保已通过 security_jail 校验)
        :param cmd: 待执行的指令列表
        :param timeout: 硬超时时间（秒）
        :param use_mise: 是否前置注入 mise 垫片 (本地跑测试时可置为 False)
        """
        full_cmd = ["mise", "exec", "--"] + cmd if use_mise else cmd

        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        try:
            # preexec_fn=os.setsid: 将子进程提升为新的进程组组长 (Process Group Leader)
            # 这样我们在超时收割时，可以一次性 kill 掉它 fork 出来的所有孙子进程
            process = subprocess.Popen(
                full_cmd,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # 合并 stderr 到 stdout
                text=True,
                env=run_env,
                preexec_fn=os.setsid
            )
        except Exception as e:
            return {"stdout_text": str(e), "exit_code": -1, "timeout": False}

        try:
            # 正常等待进程结束
            stdout_text, _ = process.communicate(timeout=timeout)
            return {
                "stdout_text": stdout_text.strip(),
                "exit_code": process.returncode,
                "timeout": False
            }
        except subprocess.TimeoutExpired:
            # 触发硬超时：获取子进程所在进程组的 PGID
            pgid = os.getpgid(process.pid)
            
            # 发送 SIGKILL 信号给整个进程组，无视任何捕捉和阻塞
            os.killpg(pgid, signal.SIGKILL)
            
            # 再次调用 communicate() 以收割僵尸进程 (Zombie Process) 并读取死亡前吐出的日志
            stdout_text, _ = process.communicate()
            
            error_msg = f"\n[DPE Engine] Process killed. Hard timeout of {timeout}s exceeded."
            return {
                "stdout_text": (stdout_text + error_msg).strip(),
                "exit_code": -9,  # -9 对应 SIGKILL
                "timeout": True
            }