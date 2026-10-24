# TodoAgent

个人用的待办看板：添加任务，后台 Agent 自动领取执行。

## 启动

```bash
uv sync
uv run main.py                      # 使用 config.yaml
uv run main.py -c my.yaml           # 指定配置文件
uv run main.py --agent cursor       # 覆盖 agent.type
```

打开 http://127.0.0.1:8765 ，API 文档见 http://127.0.0.1:8765/docs 。

Cursor 底层需要先 `cursor-agent login`，或在 `config.yaml` 的 `agent.cursor.env.CURSOR_API_KEY` 填 key。

支持 macOS 和 Windows。与系统相关的操作（目录选择框、系统通知、中止时结束进程树）集中在 `app/osutil.py`；
Windows 下这些操作通过系统自带的 Windows PowerShell 5.1（`powershell.exe`）和 `taskkill` 实现。

## 日志

日志使用标准库 `logging`（`main`、`worker`）。级别由 `config.yaml` 的 `logging.level` 控制，改完需重启。可选 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`，缺省 `INFO`。应用日志和 HTTP 服务日志共用这一级别。

## 结构

```
main.py            入口：读配置、初始化 DB、启动 worker 和 HTTP 服务
config.yaml        所有可配置项
app/db.py          SQLite 任务表
app/api.py         HTTP API + 首页
app/worker.py      轮询 todo 任务，调用 Agent 执行
app/notify.py      任务提醒（系统通知、飞书 webhook）
app/osutil.py      系统相关操作（macOS / Windows）
app/agents/        Agent 抽象层（base.py）和各底层实现
static/index.html  看板前端（无构建）
```

任务状态：`todo`（待办）→ `doing`（执行中）→ `waiting`（待回复）/ `verify`（待验收）/ `failed`（失败）；待验收的任务由用户验收后改为 `done`（完成）。
Agent 正常执行完成时进入 `verify`，不会直接算完成；手动点"完成"或拖到完成看板仍可直接改为 `done`。
worker 只领取普通任务（`repeat` 为空）中的 `todo`，按优先级从高到低、同优先级按创建顺序；
与执行中任务工作目录相同的任务先跳过，等那个任务结束再领取，同一目录不会同时跑两个任务。

执行中的任务可以中止（`POST /api/tasks/{id}/stop`）：立即杀掉 Agent 及其启动的所有子进程，任务记为 `failed`（"已手动中止"），之后可重新执行。

任务进入待验收 / 待回复 / 失败时发提醒（手动中止不提醒），见 `config.yaml` 的 `notify` 段：
系统通知（服务所在机器，macOS 通知中心 / Windows Toast），以及飞书群自定义机器人 webhook（`webhook` 为空则不发；机器人开启签名校验时填 `secret`）。

定时任务（`repeat` 为 `once` / `daily` / `weekly`，`run_at` 为下次触发时间）本身不执行，在看板的"定时任务"页单独展示：
- 到点时派生一个新的普通任务去执行（`source=schedule`，`parent_id` 指向定时任务，复制描述、工作目录、优先级、标签），并在定时任务的会话里记一条"已生成执行任务 #N"。
- `once` 派生一次后自动停用；`daily` / `weekly` 把 `run_at` 顺延一天或一周（保持几点几分），服务停机错过多次只补一次。
- `active` 控制是否激活。激活时若时间已过：周期任务顺延到下一次，单次任务需先改 `run_at`。
- 派生的任务用 `GET /api/tasks?parent_id={id}` 查询；`POST /api/tasks/{id}/run-once` 不影响计划，立即派生一次。
- 修改定时任务的标签时，已派生的任务（含已归档的）同步更新为相同标签。
- 普通任务和定时任务不能互相转换。

已完成的任务可以归档（`PATCH {"archived": true}`，只能归档 `done` 状态的任务）。任务离开"完成"状态（重新执行、发新消息等）时自动取消归档。

## Skills

工程的 `skills/` 目录存放给 Agent 用的 skill，每个子目录一个（含 `SKILL.md`，格式见 [Agent Skills](https://cursor.com/docs/skills)）。
创建任务时会把它们复制到工作目录的 `.agents/skills/` 下，每轮执行前再同步一次（同名文件覆盖，所以修改 skill 后下次执行即生效）。
目录和安装位置见 `config.yaml` 的 `skills` 段；`.agents/skills` 是标准目录，Cursor 和 Codex 都会读取。

## AI 审核

创建/编辑任务时点"AI 审核"，会把标题、描述、工作目录和可用 skills（取自各 `SKILL.md` 的 name 和 description）交给 Agent 快速审核：
以只读问答方式运行（Cursor 为 `--mode ask`，在临时空目录中，不建会话），返回结论（可行 / 需补充 / 不可行）、问题列表和完善后的描述。
完善后的描述可以一键采用，也可以撤回恢复原文。超时、模型和提示词见 `config.yaml` 的 `review` 段。

## 任务会话

每个任务对应一个 Agent 会话（`session_id`），之后每一轮都通过 `--resume` 在同一会话上继续。

- 消息记录在 `messages` 表，role 有 `user`、`agent`、`system`（开始/完成/失败及耗时）、`progress`（执行中的工具调用）。
- 首轮发送 `worker.prompt_template`；之后各轮只发送尚未交给 Agent 的用户消息（`worker.followup_template`）；没有新消息时点"重新执行"发送 `worker.continue_prompt`。
- 用户发消息（`run=true`）：任务空闲时立即重新排队；执行中则先保存，本轮结束后自动再跑一轮。`run=false` 只记录，下次执行时一并发出。
- Agent 回复中带 `worker.question_marker` 时进入 `waiting`，等用户回答。
- 执行失败时，本轮的用户消息会保留为"未处理"，重试时重新发送。
- `worker.rules` 是强约束（中文回复、步骤说明、HTML 产出位置、提问标记），每一轮 prompt 末尾都会带上。
- Agent 最终回复中提到的、位于工作目录内的 HTML 文件，会自动附上预览链接 `/preview/{任务ID}/{相对路径}`（链接前缀由 `server.public_url` 决定）。
- 消息附件和任务描述一样：粘贴/拖入文件，或直接写路径、链接、文字。

## 任务字段

| 字段 | 说明 |
|---|---|
| title | 必填，只取首行，超过 `task.title_max_len` 截断 |
| description | 必填。目标（必须有）、背景、要求、预期结果等都写在这里，由 Agent 自行理解 |
| workspace | 选填。不填时自动创建 `task.workspace_template`（默认 `tasks/task_{id}`，相对启动目录）；修改时清空也会恢复为该目录 |
| priority | 优先级，取值见 `task.priorities`，缺省 `task.default_priority`，越大越先执行 |
| tags | 选填，字符串数组 |
| run_at | 选填，定时执行时间：Unix 秒或 ISO 时间（如 `2026-10-25T09:00`，无时区按本机时间）；不填为普通任务，立即执行 |
| repeat | 选填，`once` / `daily` / `weekly`，需同时给 `run_at`；只给 `run_at` 时为 `once` |
| active | 定时任务是否激活，默认 `true` |
| source | 来源：`web` / `api`（默认）/ `schedule` 或任意字符串 |

图片等文件：在看板的描述框里直接粘贴或拖入，会保存到 `task.files_dir`，并以 `![name](绝对路径)` 插入描述；也可以直接在描述里写本地文件路径。
发给 Agent 的 prompt 由 `worker.prompt_template` 生成。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/info | Agent 类型、优先级和默认值 |
| GET | /api/tasks?status=todo&tag=bug&parent_id=3 | 任务列表（可选过滤；`parent_id` 查某个定时任务派生的任务） |
| POST | /api/tasks | 创建 |
| GET | /api/tasks/{id} | 任务详情 |
| PATCH | /api/tasks/{id} | 修改任意字段 |
| DELETE | /api/tasks/{id} | 删除（含会话消息） |
| POST | /api/tasks/{id}/run-once | 定时任务立即派生一次，不影响计划 |
| POST | /api/tasks/{id}/stop | 立即中止执行中的任务 |
| GET | /api/tasks/{id}/messages?after=0 | 会话消息，`after` 为上次拿到的最大消息 ID |
| POST | /api/tasks/{id}/messages | 发消息 `{content, run?=true}` |
| POST | /api/files | 上传文件（multipart `files`），返回绝对路径，可写进描述 |
| GET | /api/files/{name} | 读取已上传文件 |
| POST | /api/review | AI 审核 `{title?, description, workspace?}`，返回 `{verdict, summary, issues, description}` |
| POST | /api/pick-dir?start= | 在服务所在机器弹出系统目录选择框（macOS / Windows），返回 `{path}`，取消为 null |
| GET | /preview/{id}/{path} | 预览任务工作目录内的文件（HTML 及其相对引用的资源）；配置 token 时首次需带 `?token=` |

```bash
curl -X POST localhost:8765/api/tasks -H 'Content-Type: application/json' -d '{
  "title": "修复登录 bug", "priority": 2, "tags": ["bug"],
  "description": "目标：修复登录 500。\n日志见 ~/logs/err.log，不要改接口。"
}'
```

配置了 `server.api_token` 时需加 `-H 'Authorization: Bearer <token>'`。

## 新增 Agent 底层

1. 在 `app/agents/` 新建类继承 `Agent`，实现 `run(prompt, workspace, session_id, on_event, stop) -> str`：
   `session_id` 为空时新建会话，否则继续；通过 `on_event("session", id)` 回报会话 ID，`on_event("progress", 文本)` 回报进度；
   `stop` 被 set 时应尽快结束并抛出 `AgentStopped`。
   可用 `self.stream_cmd(cmd, workspace, on_line, stop=stop)` 逐行读取 CLI 输出，它已处理中止和超时（杀掉整个进程组）。
   AI 审核还需实现 `ask(prompt, workspace, timeout, model) -> str`。
2. 在 `app/agents/__init__.py` 的 `AGENTS` 中注册名字。
3. 在 `config.yaml` 的 `agent` 下添加同名配置段，`agent.type` 改为该名字。
