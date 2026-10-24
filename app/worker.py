import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import db, notify, skills
from .agents import Agent, AgentStopped

log = logging.getLogger("worker")

# 正在执行的任务 ID -> 中止信号
_running = {}


def stop(task_id: int) -> bool:
    """中止正在执行的任务；任务不在本进程执行时返回 False。"""
    ev = _running.get(task_id)
    if ev:
        ev.set()
    return ev is not None


def start(cfg: dict, agent: Agent) -> None:
    for i in range(cfg.get("concurrency", 1)):
        threading.Thread(target=_loop, args=(cfg, agent), name=f"worker-{i}", daemon=True).start()
    threading.Thread(target=_schedule_loop, args=(cfg,), name="scheduler", daemon=True).start()


def _schedule_loop(cfg: dict) -> None:
    """定时任务到点时派生执行任务。"""
    while True:
        try:
            for child in db.spawn_due():
                log.info("scheduled task %s spawned #%s", child["parent_id"], child["id"])
        except Exception:
            log.exception("scheduler error")
        time.sleep(cfg.get("poll_interval", 3))


def _loop(cfg: dict, agent: Agent) -> None:
    while True:
        task = db.claim_next()
        if task:
            _run(cfg, agent, task)
        else:
            time.sleep(cfg.get("poll_interval", 3))


def build_prompt(cfg: dict, task: dict, pending: list) -> str:
    """新会话发任务描述；已有会话只发新增的用户消息；都没有则发 continue_prompt。"""
    marker = cfg.get("question_marker", "[NEED_INPUT]")
    parts = []
    if not task["session_id"]:
        parts.append(cfg.get("prompt_template", "# {title}\n\n{description}").format(**task, question_marker=marker))
    if pending:
        messages = "\n\n---\n\n".join(m["content"] for m in pending)
        parts.append(cfg.get("followup_template", "{messages}").format(messages=messages))
    if not parts:
        parts.append(cfg.get("continue_prompt", "请继续执行任务。"))
    if cfg.get("rules"):
        parts.append(cfg["rules"].format(question_marker=marker))
    return "\n\n".join(p.strip() for p in parts)


_HTML_RE = re.compile(r"[^\s`'\"()<>\[\]]+\.html?(?![\w/])")


def html_links(base_url: str, task_id: int, workspace: str, reply: str) -> list:
    """回复中提到的、位于工作目录内且存在的 HTML 文件 → Markdown 预览链接。"""
    root = Path(workspace).resolve()
    links = []
    for p in dict.fromkeys(_HTML_RE.findall(reply)):
        path = Path(os.path.expanduser(p))
        path = (path if path.is_absolute() else root / path).resolve()
        if path.is_file() and path.is_relative_to(root):
            rel = path.relative_to(root).as_posix()
            links.append(f"- [{rel}]({base_url}/preview/{task_id}/{quote(rel)})")
    return list(dict.fromkeys(links))


def _elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}分{s % 60}秒" if s >= 60 else f"{s}秒"


def _run(cfg: dict, agent: Agent, task: dict) -> None:
    tid = task["id"]
    workspace = os.path.expanduser(task["workspace"])
    marker = cfg.get("question_marker", "[NEED_INPUT]")
    prev_read = task["read_msg_id"]
    pending = db.take_pending(task)
    prompt = build_prompt(cfg, task, pending)

    def on_event(kind: str, data: str) -> None:
        if kind == "session":
            db.update_task(tid, session_id=data)
        elif kind in ("say", "progress"):
            db.add_message(tid, "agent" if kind == "say" else "progress", data)
            db.update_task(tid, progress=data.splitlines()[0][:300])
        elif kind == "status":
            db.update_task(tid, progress=data)

    db.add_message(tid, "system", "开始执行" + (f"，处理 {len(pending)} 条新消息" if pending else ""))
    log.info("task %s started in %s", tid, workspace)
    start = time.time()
    stop_ev = _running[tid] = threading.Event()
    try:
        if not os.path.isdir(workspace):
            raise FileNotFoundError(f"workspace not found: {workspace}")
        # 每轮执行前同步一次：覆盖派生任务、修改过工作目录、skill 有更新等情况
        skills.install(workspace)
        if stop_ev.is_set():
            raise AgentStopped("stopped by user")
        reply = agent.run(prompt, workspace, task["session_id"] or None, on_event, stop_ev).strip()
        status = "waiting" if marker in reply else "verify"
        reply = reply.replace(marker, "").strip()
        links = html_links(cfg["base_url"], tid, workspace, reply)
        if links:
            reply += "\n\n**HTML 预览：**\n" + "\n".join(links)
        db.add_message(tid, "agent", reply)
        db.add_message(tid, "system", f"{'等待用户回复' if status == 'waiting' else '执行完成，待验收'}，耗时 {_elapsed(time.time() - start)}")
        status = db.finish_run(tid, status, reply)
        log.info("task %s %s", tid, status)
        notify.task_status(task, status)
    except AgentStopped:
        db.add_message(tid, "system", f"已中止，耗时 {_elapsed(time.time() - start)}")
        db.finish_run(tid, "failed", "已手动中止", read_msg_id=prev_read)
        log.info("task %s stopped", tid)
    except Exception as e:
        db.add_message(tid, "system", f"执行失败，耗时 {_elapsed(time.time() - start)}：{e}")
        db.finish_run(tid, "failed", str(e), read_msg_id=prev_read)
        log.warning("task %s failed: %s", tid, e)
        notify.task_status(task, "failed")
    finally:
        _running.pop(tid, None)
