from .base import Agent, AgentError, AgentStopped
from .cursor import CursorAgent

# 新增底层：实现 Agent.run 和 Agent.ask，然后在这里注册，并在 config.yaml 的 agent 下加同名配置段
AGENTS = {
    "cursor": CursorAgent,
}


def create_agent(agent_cfg: dict) -> Agent:
    name = agent_cfg["type"]
    if name not in AGENTS:
        raise ValueError(f"unknown agent type: {name}, available: {list(AGENTS)}")
    return AGENTS[name](agent_cfg.get(name) or {})
