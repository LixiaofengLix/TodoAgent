import json
import threading
import time
from typing import Optional

from .base import Agent, AgentError, EventCallback

# Claude Code 工具名 → 中文说明
TOOL_LABELS = {
    "Bash": "执行命令",
    "Read": "读取",
    "Edit": "编辑",
    "MultiEdit": "编辑",
    "Write": "写入",
    "NotebookEdit": "编辑",
    "Grep": "搜索",
    "Glob": "查找文件",
    "LS": "列目录",
    "WebSearch": "网页搜索",
    "WebFetch": "抓取网页",
    "TodoWrite": "更新计划",
    "Task": "子任务",
    "Skill": "使用 skill",
}


class ClaudeAgent(Agent):
    """Claude Code CLI（`claude`）底层：`-p` 非交互模式 + stream-json 事件流。"""

    def run(self, prompt: str, workspace: str, session_id: Optional[str], on_event: EventCallback,
            stop: Optional[threading.Event] = None) -> str:
        cmd = [
            self.cfg.get("bin", "claude"), "-p",
            "--output-format", "stream-json", "--verbose",
        ]
        mode = self.cfg.get("permission_mode", "bypassPermissions")
        if mode:
            cmd += ["--permission-mode", mode]
        if session_id:
            cmd += ["--resume", session_id]
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        if self.cfg.get("strict_mcp", True):
            cmd.append("--strict-mcp-config")
        cmd += self.cfg.get("extra_args") or []

        # Claude Code 每个 content block 单独发一条 assistant 事件：文本先累积，
        # 遇到工具调用时作为过程说明发出；最后一段（最后一次工具调用之后）作为最终回复
        st = {"said": None, "last_status": 0.0, "result": None}

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
            if t == "system":
                # 只有 init 事件需要上报；thinking_tokens 等事件也带 session_id，重复上报会造成无谓写库
                if ev.get("subtype") == "init" and ev.get("session_id"):
                    on_event("session", ev["session_id"])
            elif t == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    kind = b.get("type")
                    if kind == "text":
                        text = b.get("text") or ""
                        st["said"] = f"{st['said']}\n{text}" if st["said"] else text
                    elif kind == "thinking":
                        text = " ".join((b.get("thinking") or "").split())
                        if text and time.time() - st["last_status"] > 1:
                            st["last_status"] = time.time()
                            on_event("status", "思考中：" + text[-200:])
                    elif kind == "tool_use":
                        flush_said()
                        on_event("progress", _describe_tool(b))
            elif t == "result":
                st["result"] = ev

        # prompt 走 stdin：不受命令行长度限制，也避免特殊字符被 shell 解释
        self.stream_cmd(cmd, workspace, on_line, stop=stop, stdin_text=prompt)
        result = st["result"]
        if not result:
            raise AgentError("agent exited without result")
        if result.get("is_error"):
            raise AgentError(result.get("result") or result.get("subtype") or "agent error")
        return st["said"] or result.get("result") or ""

    def ask(self, prompt: str, workspace: str, timeout: int, model: str = "") -> str:
        # 只读问答：禁用所有内置工具、skills 和 MCP 配置，不读文件、不执行命令，
        # 也不依赖 run 的 strict_mcp 设置
        cmd = [self.cfg.get("bin", "claude"), "-p", "--output-format", "json",
               "--tools", "", "--strict-mcp-config", "--disable-slash-commands"]
        if model or self.cfg.get("model"):
            cmd += ["--model", model or self.cfg["model"]]
        out = []
        self.stream_cmd(cmd, workspace, out.append, timeout=timeout, stdin_text=prompt)
        # 正常只有一行 JSON；忽略可能混入的其他输出，取最后一条可解析的
        for line in reversed(out):
            try:
                result = json.loads(line)
                break
            except ValueError:
                continue
        else:
            raise AgentError("unexpected output: " + "\n".join(out)[-500:])
        if result.get("is_error"):
            raise AgentError(result.get("result") or result.get("subtype") or "agent error")
        return result.get("result") or ""


def _describe_tool(block: dict) -> str:
    """把 tool_use content block 转成进度描述，如 `执行命令 npm test`。"""
    name = block.get("name") or "tool"
    args = block.get("input") or {}
    label = TOOL_LABELS.get(name, name)
    if isinstance(args.get("todos"), list):
        marks = {"completed": "✓", "in_progress": "→"}
        items = [f"{marks.get(str(t.get('status', '')).lower(), '○')} {t.get('content', '')}"
                 for t in args["todos"] if isinstance(t, dict)]
        return f"{label}\n" + "\n".join(items)
    detail = next((str(args[k]) for k in
                   ("command", "file_path", "path", "pattern", "query", "url", "description", "skill")
                   if args.get(k)), "")
    return f"{label} {detail[:1000]}".strip()
