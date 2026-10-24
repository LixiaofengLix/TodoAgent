import json
import threading
import time
from typing import Optional

from .base import Agent, AgentError, EventCallback

TOOL_LABELS = {
    "shell": "执行命令",
    "read": "读取",
    "edit": "编辑",
    "write": "写入",
    "delete": "删除",
    "grep": "搜索",
    "glob": "查找文件",
    "ls": "列目录",
    "semSearch": "语义搜索",
    "webSearch": "网页搜索",
    "webFetch": "抓取网页",
    "updateTodos": "更新计划",
}


class CursorAgent(Agent):
    def run(self, prompt: str, workspace: str, session_id: Optional[str], on_event: EventCallback,
            stop: Optional[threading.Event] = None) -> str:
        cmd = [
            self.cfg.get("bin", "cursor-agent"), "-p", "--output-format", "stream-json",
            "--trust", "--workspace", workspace,
        ]
        if session_id:
            cmd += ["--resume", session_id]
        if self.cfg.get("force", True):
            cmd.append("--force")
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        cmd += self.cfg.get("extra_args") or []
        cmd.append(prompt)

        # 每段 assistant 文本先缓存：后面还有动作就作为过程说明发出，最后一段作为最终回复
        st = {"said": None, "thinking": "", "last_status": 0.0, "result": None}

        def flush_said() -> None:
            if st["said"]:
                on_event("say", st["said"])
            st["said"] = None

        def on_line(line: str) -> None:
            try:
                ev = json.loads(line)
            except ValueError:
                return
            t = ev.get("type")
            if t == "system" and ev.get("session_id"):
                on_event("session", ev["session_id"])
            elif t == "thinking":
                if ev.get("subtype") == "delta":
                    if not st["thinking"]:
                        flush_said()
                    st["thinking"] += ev.get("text", "")
                    if time.time() - st["last_status"] > 1:
                        st["last_status"] = time.time()
                        on_event("status", "思考中：" + " ".join(st["thinking"].split())[-200:])
                else:
                    st["thinking"] = ""
            elif t == "assistant":
                flush_said()
                text = "".join(c.get("text", "") for c in ev.get("message", {}).get("content", []))
                st["said"] = text.strip() or None
            elif t == "tool_call" and ev.get("subtype") == "started":
                flush_said()
                on_event("progress", _describe_tool(ev.get("tool_call") or {}))
            elif t == "result":
                st["result"] = ev

        self.stream_cmd(cmd, workspace, on_line, stop=stop)
        result = st["result"]
        if not result:
            raise AgentError("agent exited without result")
        if result.get("is_error"):
            raise AgentError(result.get("result") or "agent error")
        return st["said"] or result.get("result") or ""

    def ask(self, prompt: str, workspace: str, timeout: int, model: str = "") -> str:
        cmd = [
            self.cfg.get("bin", "cursor-agent"), "-p", "--output-format", "json",
            "--trust", "--mode", "ask", "--workspace", workspace,
        ]
        if model or self.cfg.get("model"):
            cmd += ["--model", model or self.cfg["model"]]
        cmd.append(prompt)
        out = []
        self.stream_cmd(cmd, workspace, out.append, timeout)
        try:
            result = json.loads("\n".join(out))
        except ValueError:
            raise AgentError("unexpected output: " + "\n".join(out)[-500:])
        if result.get("is_error"):
            raise AgentError(result.get("result") or "agent error")
        return result.get("result") or ""


def _describe_tool(tool_call: dict) -> str:
    """把 tool_call 事件转成进度描述，如 `执行命令 npm test`。"""
    name, body = next(iter(tool_call.items()), ("tool", {}))
    if name == "function":
        return f"{body.get('name', 'tool')} {str(body.get('arguments', ''))[:500]}".strip()
    name = name.removesuffix("ToolCall")
    args = body.get("args") or {}
    label = TOOL_LABELS.get(name, name)
    if isinstance(args.get("todos"), list):
        marks = {"completed": "✓", "in_progress": "→"}
        items = [f"{marks.get(str(t.get('status', '')).lower().replace('todo_status_', ''), '○')} {t.get('content', '')}"
                 for t in args["todos"] if isinstance(t, dict)]
        return f"{label}\n" + "\n".join(items)
    detail = next((str(args[k]) for k in ("command", "path", "pattern", "query", "url", "globPattern") if args.get(k)), "")
    return f"{label} {detail[:1000]}".strip()
