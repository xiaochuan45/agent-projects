# agent-projects

多项目管理 MVP，使用 Python 3.9+ 标准库和 SQLite。项目编号从 `P001` 起，任务编号从 `T001` 起。两套序列独立，超过三位后自然扩展。

## 快速开始

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from project_manager import ProjectManager

with TemporaryDirectory() as temp:
    root = Path(temp)
    workspace = root / "workspace"
    (workspace / "app").mkdir(parents=True)
    manager = ProjectManager(root / "projects.sqlite3", workspace)

    project = manager.create_project("示例项目", "app", aliases=["示例"])
    candidates = manager.find_projects("示例")
    if len(candidates) == 1:
        task = manager.create_task(
            candidates[0].number, "整理需求", external_message_id="example:123"
        )
        manager.update_task(task.number, status="in_progress", codex_session_id="session-123")
```

数据库文件由调用方指定，父目录须预先存在。同一数据库应配合同一工作区根目录使用。项目目录须已存在，可传工作区内的相对路径或绝对路径。路径包含 `..`、解析失败或解析后离开工作区时会被拒绝。工作区根目录和 Git 元数据目录（`.git` 及其子目录）不可注册。项目目录不能相同或互相嵌套，指向同一位置的符号链接也会触发冲突。创建时在同一个 SQLite 写事务中检查冲突并存储解析后的相对路径，交给执行器前应调用 `project_directory` 重新验证。如果有其他进程能在验证后替换目录，执行器仍需在实际打开目录时实施隔离。

## 模块接口

| 接口 | 返回值与行为 |
| --- | --- |
| `ProjectManager(database, workspace)` | 初始化数据库，要求工作区目录已存在 |
| `create_project(name, directory, *, aliases=(), codex_session_id=None)` | 返回新建的 `Project`。项目名称禁用 `P001`、`T001` 等系统编号（忽略大小写） |
| `find_projects(query)` | 返回按编号排序的 `list[Project]`，忽略首尾空白与大小写。明确的项目编号仅查询该编号，最多返回一条。其他查询词完整匹配名称和别名，返回全部候选 |
| `get_project(project_number)` | 按明确编号获取 `Project` |
| `project_directory(project_number)` | 返回重新验证过的绝对 `Path` |
| `create_task(project_number, title, *, external_message_id=None, codex_session_id=None)` | 返回 `Task`。相同的非空外部消息 ID 在整个数据库内去重。项目编号和去除首尾空白后的标题一致时返回原任务的当前状态。项目或标题不一致时抛出 `ValueError`。省略消息 ID 时每次创建新任务 |
| `get_task(task_number)` | 按明确编号获取 `Task` |
| `update_project(project_number, *, status=None, codex_session_id=...)` | 更新并返回 `Project` |
| `update_task(task_number, *, status=None, codex_session_id=...)` | 更新并返回 `Task` |

新记录默认状态为 `pending`，也可更新为 `in_progress`、`completed` 或 `cancelled`。更新时省略状态或传入 `status=None` 会保留现有状态。省略 Session ID 会保留原值，显式传入 `codex_session_id=None` 会清除。未知编号抛出 `KeyError`，无效查询文本、状态或项目路径抛出 `ValueError`。

## 数据结构

- `projects` 保存自增 ID、名称、名称查询键、工作区相对路径、状态和可选 Session ID。
- `project_aliases` 保存项目外键、别名和别名查询键，同一项目内去重，允许不同项目共享普通别名。编号格式的别名只能使用该项目自己的编号。
- `tasks` 保存自增 ID、项目外键、标题、状态、可选 Session ID 和可选外部消息 ID。外部消息 ID 有唯一约束，创建在写事务中检查重投并落库。

## 测试

在仓库根目录执行 `python3 -B -m unittest discover -v`。每条测试使用独立的临时目录和数据库。

## 飞书私聊任务登记

`FeishuMessageReceiver(manager, allowed_open_id=..., namespace=...)` 接收可信传输层交来的 JSON 对象。`handle_event(payload)` 支持飞书 V2 原始事件和本机 lark-cli 1.0.100 的扁平消息记录。调用方应先通过 SDK 或本地 CLI 获得可信事件。若未来接入 HTTP，须先在入口校验飞书请求并处理协议的验证和解密流程，防止伪造 Open ID。

私聊按行发送。已有项目填写编号、完整名称或别名。

```text
任务
P001
整理需求
```

新项目填写名称和已存在的工作区内目录，最后一项是首个任务。

```text
新建项目
示例项目
app
整理需求
```

任务文本可以跨行。只有指定 Open ID 发来的 `user` 类型单聊文本才会进入登记。`namespace` 由部署方固定为应用与租户组合，同一部署的原始事件与 CLI 记录必须使用相同值。SQLite 以 `feishu:<namespace>:<message_id>` 为唯一键，在同一写事务中检查重投、查找或新建项目并登记任务。

| `status` | 含义 |
| --- | --- |
| `registered` | 返回 `project_number`、`task_number`、`task_status` |
| `duplicate` | 返回原项目、原任务编号和任务当前状态 |
| `clarification_required` | 返回按编号排序的候选项目，包含编号和名称。没有匹配时列表为空。收到候选后，重新发送任务并填写明确编号 |
| `ignored` | 事件类型、发件人、会话或消息类型不符合条件 |
| `invalid` | 消息 ID、文本、命令、项目名称或路径无效，包含 `reason` |

去重记录随已登记任务持久化。澄清和无效输入无需写入项目库。

### 与 Autoloop 审批共存

本机 v1.9.2 的 `plugins/feishu/channel.ts` 使用 `lark-cli event consume card.action.trigger --as bot`，将卡片点击交给审批流程。`docs/plugins.md` 和 `docs/protocol.md` 定义的 `source.ts` 是同步 HTTP Webhook 转换器，入口为 `/v1/in/<来源>`，没有长连接消息钩子。`/v1/events` 可接收外部来源提交的标准事件。

本机 lark-cli 1.0.100 的 `event consume --help` 和 `skills read lark-event` 说明每个消费者订阅一个 EventKey，多个本地消费者可共享事件总线 daemon。上线方案是在同一主机、同一运行用户、同一应用和 daemon 下，保留原审批消费者，另用独立进程订阅 `im.message.receive_v1`，解码其 NDJSON 并调用本模块。共用 daemon 的兼容性仍需用测试应用验证，避免为同一应用在另一台主机或另一运行账户启动独立长连接。不能共享 daemon 时可评估为接单使用独立测试应用，届时须配置该应用对应的 Open ID。

### 开启真实接单前

1. 在隔离测试应用开启机器人能力，配置 `im.message.receive_v1` 长连接订阅和 `im:message.p2p_msg:readonly` 权限。准备稳定的应用和租户命名空间、该应用的允许 Open ID，以及仓库外的项目数据库。
2. 接上消费进程和私聊回执，在同一个测试 daemon 上交错验证私聊登记、卡片审批和恢复。验证重连、消费者退出再启动后两类事件仍可到达，测试重投、拒绝路径及回执失败。消费者回执失败时可用原消息重试查询登记结果，回执发送自身也需要处理重复。
3. 先上线登记和回执。今后单独接入执行时，以任务编号作为执行幂等键，实施领取和失败恢复机制，执行前重新验证工作目录。消息正文是用户输入，接入执行前需要定义审批与权限边界。

共用 daemon 带来共同故障点。测试应关注新增订阅权限或应用发布是否影响现有审批、不同 daemon 是否分流事件，以及重连或缓冲区溢出时是否漏收。运行日志应避免记录私聊正文和凭据。本轮只运行模拟事件测试。
