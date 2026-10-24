import os
import subprocess
import tempfile
import threading
from typing import Callable, Optional

from .. import osutil

# on_event(kind, data)：
#   ("session", 会话 ID)  尽早回报，失败后也能在同一会话上继续
#   ("say", 文本)         Agent 执行过程中的说明（最终回复由 run 返回，不在此回报）
#   ("progress", 文本)    执行步骤，如工具调用
#   ("status", 文本)      当前状态（如思考中），只更新实时状态，不入消息历史
EventCallback = Callable[[str, str], None]


class AgentError(Exception):
    pass


class AgentStopped(AgentError):
    """被用户中止。"""


class Agent:
    """Agent 底层抽象：在会话中执行一轮对话。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}

    def run(self, prompt: str, workspace: str, session_id: Optional[str], on_event: EventCallback,
            stop: Optional[threading.Event] = None) -> str:
        """session_id 为空时新建会话，否则继续该会话。返回 Agent 最终回复，失败抛 AgentError。
        stop 被置位时应立即结束并抛 AgentStopped（用 stream_cmd 即可自动处理）。"""
        raise NotImplementedError

    def ask(self, prompt: str, workspace: str, timeout: int, model: str = "") -> str:
        """一次性只读问答（不建会话、不改文件），用于快速审核等。返回回复文本，失败抛 AgentError。"""
        raise NotImplementedError

    def stream_cmd(self, cmd: list, workspace: str, on_line: Callable[[str], None], timeout: Optional[int] = None,
                   stop: Optional[threading.Event] = None) -> None:
        """运行命令，逐行回调 stdout；超时、被 stop 中止或非 0 退出时抛 AgentError。"""
        env = {**os.environ, **{k: str(v) for k, v in (self.cfg.get("env") or {}).items() if v}}
        timeout = timeout or self.cfg.get("timeout", 1800)
        timed_out = threading.Event()
        stop = stop or threading.Event()
        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
            # 独立进程组：结束时连同 Agent 启动的子进程（命令、脚本等）一起杀掉
            p = osutil.popen_tree(cmd, cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=err,
                                  encoding="utf-8", errors="replace")

            def kill() -> None:
                osutil.kill_tree(p)

            def watch_stop() -> None:
                while p.poll() is None:
                    if stop.wait(0.3):
                        kill()
                        return

            timer = threading.Timer(timeout, lambda: (timed_out.set(), kill()))
            timer.start()
            threading.Thread(target=watch_stop, daemon=True).start()
            try:
                for line in p.stdout:
                    if line.strip():
                        on_line(line.strip())
                p.wait()
            finally:
                timer.cancel()
                if p.poll() is None:
                    kill()
                    p.wait()
            if stop.is_set():
                raise AgentStopped("stopped by user")
            if timed_out.is_set():
                raise AgentError(f"timeout after {timeout}s")
            if p.returncode != 0:
                err.seek(0)
                raise AgentError(f"exit code {p.returncode}\n{err.read()[-4000:]}")
