"""把工程里的 skill 安装到任务工作目录，供 Agent 发现和使用。"""
import shutil
from pathlib import Path

import yaml

_src = None
_target = ""


def init(cfg: dict) -> None:
    global _src, _target
    _src = Path(cfg["dir"]).resolve() if cfg.get("dir") else None
    _target = cfg.get("target") or ".agents/skills"


def describe() -> list:
    """所有 skill 的 (name, description)，取自 SKILL.md 的 frontmatter。"""
    if not _src or not _src.is_dir():
        return []
    items = []
    for f in sorted(_src.glob("**/SKILL.md")):
        meta = {}
        parts = f.read_text(encoding="utf-8").split("---", 2)
        if len(parts) == 3 and not parts[0].strip():
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                pass
        items.append((meta.get("name") or f.parent.name, str(meta.get("description") or "").strip()))
    return items


def install(workspace: str) -> list:
    """把 skills 目录下的每个子目录复制到 <workspace>/<target>/ 下（同名覆盖，保持最新），返回安装的 skill 名。"""
    if not _src or not _src.is_dir():
        return []
    dst = Path(workspace).expanduser().resolve() / _target
    names = []
    for d in sorted(_src.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            shutil.copytree(d, dst / d.name, dirs_exist_ok=True)
            names.append(d.name)
    return names
