import argparse
import logging

import uvicorn
import yaml

from app import db, notify, skills, worker
from app.agents import create_agent
from app.api import create_app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--agent", help="覆盖配置中的 agent.type")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if args.agent:
        cfg["agent"]["type"] = args.agent

    level_name = str((cfg.get("logging") or {}).get("level", "INFO")).upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        raise SystemExit(f"invalid logging.level: {level_name}")
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db.init(cfg["database"])
    skills.init(cfg.get("skills") or {})
    server = cfg["server"]
    cfg["worker"]["base_url"] = (server.get("public_url") or f"http://{server['host']}:{server['port']}").rstrip("/")
    notify.init(cfg.get("notify") or {}, cfg["worker"]["base_url"])
    agent = create_agent(cfg["agent"])
    if cfg["worker"].get("enabled", True):
        worker.start(cfg["worker"], agent)
    logging.getLogger("main").info("agent: %s", cfg["agent"]["type"])

    uvicorn.run(
        create_app(cfg, agent),
        host=cfg["server"]["host"],
        port=cfg["server"]["port"],
        log_level=level,
    )


if __name__ == "__main__":
    main()
