"""任务需要用户处理（待验收 / 待回复 / 失败）时提醒：系统通知（macOS / Windows）、飞书群机器人 webhook。"""
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.request

from . import osutil

log = logging.getLogger("notify")

LABELS = {"verify": "待验收", "waiting": "待回复", "failed": "执行失败"}

_cfg = {}
_base_url = ""


def init(cfg: dict, base_url: str) -> None:
    global _cfg, _base_url
    _cfg = cfg or {}
    _base_url = base_url


def task_status(task: dict, status: str) -> None:
    """任务进入 status 后调用；不在 notify.on 中的状态忽略。发送在后台线程进行，不阻塞执行。"""
    if status not in _cfg.get("on", list(LABELS)):
        return
    title = f"任务 #{task['id']} {LABELS.get(status, status)}"
    link = f"{_base_url}/#task-{task['id']}"
    threading.Thread(target=_send, args=(title, task["title"], link), daemon=True).start()


def _send(title: str, body: str, link: str) -> None:
    if _cfg.get("system", True):
        try:
            osutil.notify(title, body)
        except Exception:
            log.warning("system notification failed", exc_info=True)
    feishu = _cfg.get("feishu") or {}
    if feishu.get("webhook"):
        try:
            _feishu(feishu["webhook"], feishu.get("secret", ""), f"{title}\n{body}\n{link}")
        except Exception:
            log.warning("feishu webhook failed", exc_info=True)


def _feishu(webhook: str, secret: str, text: str) -> None:
    payload = {"msg_type": "text", "content": {"text": text}}
    if secret:
        ts = str(int(time.time()))
        sign = hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()
        payload.update(timestamp=ts, sign=base64.b64encode(sign).decode())
    req = urllib.request.Request(webhook, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read() or b"{}")
    if data.get("code", 0) != 0:
        log.warning("feishu webhook error: %s", data)
