import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from datetime import datetime
from typing import List, Optional, Union

from fastapi import APIRouter, Cookie, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, osutil, skills, worker
from .agents import Agent, AgentError

log = logging.getLogger("api")

STATIC_DIR = Path(__file__).parent.parent / "static"

class TaskCreate(BaseModel):
    title: str
    description: str
    workspace: str = ""
    priority: Optional[int] = None
    tags: List[str] = []
    source: str = "api"
    # 定时执行：Unix 秒或 ISO 时间（无时区按本机时间）；不填为普通任务，立即执行
    run_at: Optional[Union[float, datetime]] = None
    # 定时周期 once / daily / weekly，需同时指定 run_at（首次时间）；只给 run_at 时视为 once
    repeat: str = ""
    active: bool = True


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    workspace: Optional[str] = None
    priority: Optional[int] = None
    tags: Optional[List[str]] = None
    source: Optional[str] = None
    status: Optional[str] = None
    result: Optional[str] = None
    run_at: Optional[Union[float, datetime]] = None
    repeat: Optional[str] = None
    active: Optional[bool] = None
    archived: Optional[bool] = None


class ReviewRequest(BaseModel):
    title: str = ""
    description: str
    workspace: str = ""


class MessageCreate(BaseModel):
    content: str
    # 为真时：任务空闲则立即重新排队；执行中则在本轮结束后自动处理
    run: bool = True


def create_app(cfg: dict, agent: Agent) -> FastAPI:
    token = cfg["server"].get("api_token") or ""
    task_cfg = cfg.get("task") or {}
    priorities = task_cfg.get("priorities") or {0: "默认"}
    default_priority = task_cfg.get("default_priority", 0)
    workspace_template = os.path.abspath(task_cfg.get("workspace_template", "tasks/task_{id}"))
    title_max_len = task_cfg.get("title_max_len", 50)
    files_dir = Path(task_cfg.get("files_dir", "data/files")).resolve()

    def auth(authorization: str = Header(""), token_q: str = Query("", alias="token")):
        if token and authorization != f"Bearer {token}" and token_q != token:
            raise HTTPException(401, "invalid token")

    app = FastAPI(title="TodoAgent")
    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    def must_get(task_id: int) -> dict:
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        return task

    def default_workspace(task_id: int) -> str:
        path = workspace_template.format(id=task_id)
        os.makedirs(path, exist_ok=True)
        return path

    def clean(fields: dict) -> dict:
        fields = {k: v for k, v in fields.items() if v is not None}
        for k in ("title", "description"):
            if k in fields:
                fields[k] = fields[k].strip()
                if not fields[k]:
                    raise HTTPException(400, f"{k} is required")
        if "title" in fields:
            title = fields["title"].splitlines()[0].strip()
            fields["title"] = title if len(title) <= title_max_len else title[:title_max_len] + "…"
        if "priority" in fields and fields["priority"] not in priorities:
            raise HTTPException(400, f"priority must be one of {list(priorities)}")
        if "status" in fields and fields["status"] not in db.STATUSES:
            raise HTTPException(400, f"status must be one of {db.STATUSES}")
        if fields.get("workspace"):
            # 统一成绝对路径（去掉末尾分隔符等），"相同工作目录不能同时跑"按此比较
            ws = os.path.abspath(os.path.expanduser(fields["workspace"].strip()))
            if not os.path.isdir(ws):
                raise HTTPException(400, f"workspace not found: {fields['workspace']}")
            fields["workspace"] = ws
        if "tags" in fields:
            fields["tags"] = list(dict.fromkeys(t.strip() for t in fields["tags"] if t.strip()))
        if isinstance(fields.get("run_at"), datetime):
            fields["run_at"] = fields["run_at"].timestamp()
        if fields.get("repeat") and fields["repeat"] not in db.REPEATS:
            raise HTTPException(400, f"repeat must be empty or one of {list(db.REPEATS)}")
        return fields

    def fix_schedule(task: dict, fields: dict) -> dict:
        """激活中的定时任务时间已过：周期任务顺延到下一次，单次任务要求先改时间。"""
        t = {**task, **fields}
        now = time.time()
        if t["repeat"] and t["active"] and t["run_at"] <= now:
            if t["repeat"] == "once":
                raise HTTPException(400, "执行时间已过，请先修改执行时间")
            fields["run_at"] = db.next_run(t["run_at"], t["repeat"], now)
        return fields

    @api.get("/info")
    def info():
        return {
            "agent": cfg["agent"]["type"],
            "statuses": db.STATUSES,
            "roles": db.ROLES,
            "priorities": priorities,
            "default_priority": default_priority,
            # 自动创建的工作目录前缀，前端据此区分"默认目录"和手动指定的目录
            "auto_workspace_prefix": workspace_template.split("{id}")[0],
            "files_dir": str(files_dir),
            "question_marker": cfg["worker"].get("question_marker", ""),
        }

    @api.get("/tasks")
    def list_tasks(status: Optional[str] = None, tag: Optional[str] = None, parent_id: Optional[int] = None):
        tasks = db.list_tasks(status, tag)
        return tasks if parent_id is None else [t for t in tasks if t["parent_id"] == parent_id]

    @api.post("/tasks")
    def create_task(body: TaskCreate):
        fields = body.model_dump()
        fields["workspace"] = fields["workspace"].strip()
        fields["run_at"] = fields["run_at"] or 0
        if fields["run_at"] and not fields["repeat"]:
            fields["repeat"] = "once"
        if fields["repeat"] and not fields["run_at"]:
            raise HTTPException(400, "repeat requires run_at as the first run time")
        if fields["priority"] is None:
            fields["priority"] = default_priority
        task = db.create_task(clean(fields), default_workspace)
        try:
            skills.install(task["workspace"])
        except OSError:
            # 不阻塞创建；执行前还会再装一次，那时失败会记为执行失败
            log.warning("install skills into %s failed", task["workspace"], exc_info=True)
        return task

    @api.get("/tasks/{task_id}")
    def get_task(task_id: int):
        return must_get(task_id)

    @api.patch("/tasks/{task_id}")
    def update_task(task_id: int, body: TaskUpdate):
        task = must_get(task_id)
        fields = clean(body.model_dump())
        if "repeat" in fields and bool(fields["repeat"]) != bool(task["repeat"]):
            raise HTTPException(400, "普通任务和定时任务不能互相转换")
        if fields.get("archived") and fields.get("status", task["status"]) != "done":
            raise HTTPException(400, "只能归档已完成的任务")
        if "workspace" in fields and not fields["workspace"].strip():
            fields["workspace"] = default_workspace(task_id)
        updated = db.update_task(task_id, **fix_schedule(task, fields))
        if task["repeat"] and "tags" in fields:
            db.sync_children_tags(task_id, fields["tags"])
        return updated

    @api.delete("/tasks/{task_id}")
    def delete_task(task_id: int):
        if not db.delete_task(task_id):
            raise HTTPException(404, "task not found")
        return {"ok": True}

    @api.post("/tasks/{task_id}/stop")
    def stop_task(task_id: int):
        """立即中止正在执行的任务（连同 Agent 启动的子进程），任务记为失败"已手动中止"。"""
        if must_get(task_id)["status"] != "doing":
            raise HTTPException(400, "任务不在执行中")
        if not worker.stop(task_id):
            raise HTTPException(409, "任务不在本服务的 worker 中执行，无法中止")
        return {"ok": True}

    @api.post("/tasks/{task_id}/run-once")
    def run_once(task_id: int):
        """定时任务：不影响定时计划，立即派生一次执行任务。"""
        task = must_get(task_id)
        if not task["repeat"]:
            raise HTTPException(400, "not a scheduled task")
        return db.spawn_run(task)

    @api.get("/tasks/{task_id}/messages")
    def list_messages(task_id: int, after: int = 0):
        must_get(task_id)
        return db.list_messages(task_id, after)

    @api.post("/tasks/{task_id}/messages")
    def post_message(task_id: int, body: MessageCreate):
        must_get(task_id)
        if not body.content.strip():
            raise HTTPException(400, "content is required")
        return db.post_user_message(task_id, body.content.strip(), body.run)

    @api.post("/review")
    def review(body: ReviewRequest):
        """AI 审核：结合可用 skills 快速判断任务可行性，并给出完善后的描述。"""
        if not body.description.strip():
            raise HTTPException(400, "description is required")
        rcfg = cfg.get("review") or {}
        listed = "\n".join(f"- {n}：{d}" for n, d in skills.describe()) or "（无）"
        prompt = rcfg["prompt_template"].format(
            title=body.title.strip() or "（未填）",
            description=body.description.strip(),
            workspace=body.workspace.strip() or "（未指定，自动创建空目录）",
            skills=listed,
        )
        with tempfile.TemporaryDirectory() as tmp:
            try:
                reply = agent.ask(prompt, tmp, rcfg.get("timeout", 90), rcfg.get("model", ""))
            except AgentError as e:
                raise HTTPException(502, f"审核失败：{str(e)[:300]}")
        start, end = reply.find("{"), reply.rfind("}")
        try:
            data = json.loads(reply[start:end + 1])
        except ValueError:
            # 没按格式返回时，把原文作为结论
            return {"verdict": "", "summary": reply.strip(), "issues": [], "description": ""}
        return {
            "verdict": str(data.get("verdict") or ""),
            "summary": str(data.get("summary") or ""),
            "issues": [str(i) for i in data.get("issues") or []],
            "description": str(data.get("description") or "").strip(),
        }

    @api.post("/pick-dir")
    def pick_dir(start: str = ""):
        """在服务所在的机器上弹出系统目录选择框（macOS / Windows），返回所选绝对路径；取消时 path 为 null。"""
        try:
            return {"path": osutil.pick_dir(os.path.expanduser(start.strip()))}
        except osutil.Cancelled:
            return {"path": None}
        except NotImplementedError as e:
            raise HTTPException(501, str(e))
        except Exception as e:
            raise HTTPException(500, str(e))

    @api.post("/files")
    def upload_files(files: List[UploadFile] = File(...)):
        """保存文件并返回绝对路径，供写进任务描述。"""
        files_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in files:
            name = Path(f.filename or "file").name
            path = files_dir / f"{uuid.uuid4().hex[:8]}_{name}"
            with open(path, "wb") as out:
                shutil.copyfileobj(f.file, out)
            saved.append({"name": name, "path": str(path)})
        return saved

    @api.get("/files/{name}")
    def get_file(name: str):
        path = files_dir / Path(name).name
        if not path.is_file():
            raise HTTPException(404, "file not found")
        return FileResponse(path)

    app.include_router(api)

    @app.get("/preview/{task_id}/{path:path}")
    def preview(task_id: int, path: str, token_q: str = Query("", alias="token"), token_c: str = Cookie("", alias="token")):
        """预览任务工作目录内的文件（Agent 生成的 HTML 及其引用的资源）。
        带 ?token= 访问时写入 cookie，页面内相对路径的资源请求靠 cookie 鉴权。"""
        if token and token not in (token_q, token_c):
            raise HTTPException(401, "invalid token")
        task = must_get(task_id)
        root = Path(os.path.expanduser(task["workspace"] or default_workspace(task_id))).resolve()
        file = (root / path).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            raise HTTPException(404, "file not found")
        resp = FileResponse(file)
        if token and token_q == token:
            resp.set_cookie("token", token, httponly=True, samesite="lax")
        return resp

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
