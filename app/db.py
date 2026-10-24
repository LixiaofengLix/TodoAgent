import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

# verify 为待验收：Agent 执行完成后进入，用户验收后改为 done
# Windows 路径不区分大小写
_WS_COLLATE = "COLLATE NOCASE" if os.name == "nt" else ""

STATUSES = ("todo", "doing", "waiting", "verify", "done", "failed")
# 定时任务的周期 -> 间隔天数；once 触发一次后自动停用
REPEATS = {"once": 0, "daily": 1, "weekly": 7}
# user 用户消息；agent Agent 回复；system 系统事件（开始/结束/失败）；progress 执行进度
ROLES = ("user", "agent", "system", "progress")

# 任务表字段；新增字段只需加在这里，启动时会自动给旧库补列
COLUMNS = {
    "title": "TEXT NOT NULL DEFAULT ''",
    "description": "TEXT NOT NULL DEFAULT ''",
    "workspace": "TEXT NOT NULL DEFAULT ''",
    "source": "TEXT NOT NULL DEFAULT 'api'",
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "tags": "TEXT NOT NULL DEFAULT '[]'",
    "status": "TEXT NOT NULL DEFAULT 'todo'",
    "result": "TEXT NOT NULL DEFAULT ''",
    # 定时任务的下次触发时间（Unix 秒）
    "run_at": "REAL NOT NULL DEFAULT 0",
    # 空为普通任务；once / daily / weekly 为定时任务：本身不执行，到点派生一个新任务去执行
    "repeat": "TEXT NOT NULL DEFAULT ''",
    # 定时任务是否激活，停用后不再派生
    "active": "INTEGER NOT NULL DEFAULT 1",
    # 由定时任务派生时，记录定时任务 ID
    "parent_id": "INTEGER NOT NULL DEFAULT 0",
    # 已完成任务是否归档；任务离开"完成"状态时自动取消
    "archived": "INTEGER NOT NULL DEFAULT 0",
    # 最近一条进度，执行中显示在卡片上
    "progress": "TEXT NOT NULL DEFAULT ''",
    # Agent 底层的会话 ID，后续轮次在该会话上继续
    "session_id": "TEXT NOT NULL DEFAULT ''",
    # 已交给 Agent 的最后一条用户消息 ID
    "read_msg_id": "INTEGER NOT NULL DEFAULT 0",
    "created_at": "REAL NOT NULL DEFAULT 0",
    "updated_at": "REAL NOT NULL DEFAULT 0",
}
EDITABLE_FIELDS = tuple(k for k in COLUMNS if k not in ("created_at", "updated_at"))

_path = ""
# 串行化"领取任务 / 用户发消息 / 结束执行"之间的状态判断
_lock = threading.Lock()


def init(path: str) -> None:
    global _path
    _path = path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        cols = ", ".join(f"{k} {v}" for k, v in COLUMNS.items())
        c.execute(f"CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols})")
        existing = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
        for k, v in COLUMNS.items():
            if k not in existing:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {k} {v}")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_task ON messages (task_id, id)")
        # 旧数据：未到点的普通定时任务转为单次定时任务；用"完成"停掉的周期任务转为停用
        c.execute("UPDATE tasks SET repeat='once' WHERE repeat='' AND status='todo' AND run_at>?", (time.time(),))
        c.execute("UPDATE tasks SET active=0, status='todo' WHERE repeat!='' AND status!='todo'")
        # 上次退出时还在执行的任务已经中断
        c.execute(
            "UPDATE tasks SET status='failed', result='interrupted: server restarted', updated_at=? "
            "WHERE status='doing'",
            (time.time(),),
        )


@contextmanager
def _conn():
    c = sqlite3.connect(_path, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _load(c, where: str = "", params: tuple = ()) -> list:
    rows = c.execute(
        f"SELECT id, {', '.join(COLUMNS)} FROM tasks {where} ORDER BY priority DESC, id", params
    ).fetchall()
    tasks = []
    for r in rows:
        t = dict(r)
        t["tags"] = json.loads(t["tags"])
        tasks.append(t)
    return tasks


def _encode(fields: dict) -> dict:
    fields = {k: v for k, v in fields.items() if k in EDITABLE_FIELDS and v is not None}
    if "tags" in fields:
        fields["tags"] = json.dumps(fields["tags"], ensure_ascii=False)
    return fields


def list_tasks(status: Optional[str] = None, tag: Optional[str] = None) -> list:
    with _conn() as c:
        tasks = _load(c, "WHERE status=?", (status,)) if status else _load(c)
    if tag:
        tasks = [t for t in tasks if tag in t["tags"]]
    return tasks


def get_task(task_id: int) -> Optional[dict]:
    with _conn() as c:
        tasks = _load(c, "WHERE id=?", (task_id,))
    return tasks[0] if tasks else None


def create_task(fields: dict, default_workspace: Optional[Callable[[int], str]] = None) -> dict:
    """default_workspace(task_id) 在未指定 workspace 时生成目录，与插入在同一事务内，
    避免 worker 领取到没有工作目录的任务。"""
    fields = _encode(fields)
    now = time.time()
    fields.update(created_at=now, updated_at=now)
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO tasks ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
            tuple(fields.values()),
        )
        if not fields.get("workspace") and default_workspace:
            c.execute("UPDATE tasks SET workspace=? WHERE id=?", (default_workspace(cur.lastrowid), cur.lastrowid))
    return get_task(cur.lastrowid)


def update_task(task_id: int, **fields) -> Optional[dict]:
    fields = _encode(fields)
    if fields.get("status") not in (None, "done"):
        fields["archived"] = 0
    if fields:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        with _conn() as c:
            c.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
    return get_task(task_id)


def sync_children_tags(parent_id: int, tags: list) -> None:
    """定时任务改标签时同步到它派生的任务（不改 updated_at，避免打乱排序）。"""
    with _conn() as c:
        c.execute("UPDATE tasks SET tags=? WHERE parent_id=?", (json.dumps(tags, ensure_ascii=False), parent_id))


def delete_task(task_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        c.execute("DELETE FROM messages WHERE task_id=?", (task_id,))
    return cur.rowcount > 0


def claim_next() -> Optional[dict]:
    """取已到执行时间、优先级最高、最早创建的 todo 任务并标记为 doing。
    与正在执行的任务工作目录相同的任务跳过，避免同一目录被同时修改。"""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT id FROM tasks WHERE status='todo' AND repeat='' AND run_at<=? "
            f"AND workspace {_WS_COLLATE} NOT IN (SELECT workspace FROM tasks WHERE status='doing') "
            "ORDER BY priority DESC, id LIMIT 1",
            (time.time(),),
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE tasks SET status='doing', progress='', updated_at=? WHERE id=?", (time.time(), row["id"]))
    return get_task(row["id"])


def next_run(run_at: float, repeat: str, now: float) -> float:
    """按周期把时间往后推到 now 之后（按本地时间加天数，保持几点几分不变）。"""
    dt = datetime.fromtimestamp(run_at if run_at > 0 else now)
    while dt.timestamp() <= now:
        dt += timedelta(days=REPEATS[repeat])
    return dt.timestamp()


def spawn_run(template: dict) -> dict:
    """由定时任务派生一个立即执行的任务，并在定时任务的会话中记录。"""
    when = datetime.now().strftime("%m-%d %H:%M")
    fields = {k: template[k] for k in ("description", "workspace", "priority", "tags")}
    fields.update(title=f"{template['title']}（{when}）", source="schedule", parent_id=template["id"])
    child = create_task(fields)
    add_message(template["id"], "system", f"已生成执行任务 #{child['id']}")
    return child


def spawn_due() -> list:
    """为所有到点的已激活定时任务派生执行任务：单次的随后停用，周期的把下次时间往后推（错过多次只补一次）。"""
    now = time.time()
    with _lock:
        with _conn() as c:
            due = _load(c, "WHERE repeat!='' AND active=1 AND run_at<=?", (now,))
        children = []
        for t in due:
            children.append(spawn_run(t))
            if t["repeat"] == "once":
                update_task(t["id"], active=0)
            else:
                update_task(t["id"], run_at=next_run(t["run_at"], t["repeat"], now))
    return children


def add_message(task_id: int, role: str, content: str) -> dict:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO messages (task_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (task_id, role, content, time.time()),
        )
        row = c.execute("SELECT * FROM messages WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_messages(task_id: int, after: int = 0) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE task_id=? AND id>? ORDER BY id", (task_id, after)
        ).fetchall()
    return [dict(r) for r in rows]


def post_user_message(task_id: int, content: str, run: bool = True) -> dict:
    """保存用户消息；run 为真且任务不在执行中时重新排队。执行中的消息由 finish_run 处理。"""
    with _lock:
        msg = add_message(task_id, "user", content)
        if run and get_task(task_id)["status"] != "doing":
            update_task(task_id, status="todo")
    return msg


def take_pending(task: dict) -> list:
    """取出尚未交给 Agent 的用户消息，并推进 read_msg_id。"""
    msgs = [m for m in list_messages(task["id"], task["read_msg_id"]) if m["role"] == "user"]
    if msgs:
        update_task(task["id"], read_msg_id=msgs[-1]["id"])
    return msgs


def finish_run(task_id: int, status: str, result: str, read_msg_id: Optional[int] = None) -> str:
    """结束一轮执行，返回最终状态。成功时若执行期间有新用户消息，直接重新排队。
    失败时把 read_msg_id 回退，让重试时重新带上这轮的消息。"""
    with _lock:
        if read_msg_id is not None:
            update_task(task_id, read_msg_id=read_msg_id)
        task = get_task(task_id)
        if status != "failed" and any(
            m["role"] == "user" for m in list_messages(task_id, task["read_msg_id"])
        ):
            status = "todo"
        update_task(task_id, status=status, result=result, progress="")
    return status
