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

任务去重键随已登记任务持久化。澄清和无效输入不会创建项目或任务。

### 实时接单与单次联调

`feishu_live_bridge.py` 通过本机 `lark-cli event consume im.message.receive_v1 --as bot` 订阅消息。它保持子进程 stdin 开启，等 stderr 出现该事件的就绪标记后读取 stdout NDJSON，并继续排空 stderr。启动等待上限为 30 秒。消费者退出后按 2、4、8 秒递增退避，最多等待 60 秒，一次运行满 60 秒后恢复初始间隔。收到 SIGTERM 或 Ctrl+C 时停止接收，等待正在处理的事件，再关闭消费者 stdin 并回收进程。

接单桥从环境读取逗号分隔的 Open ID 白名单，每位授权用户使用同一个项目库。它将授权的单聊文本交给 `FeishuMessageReceiver` 登记，再用 `lark-cli im +messages-send --as bot --user-id …` 私聊发送项目编号和任务编号，或发送格式错误与项目澄清提示。已确认发送的回执在同一 SQLite 库中保存成功标记，重投时直接跳过。发送失败时保留已登记任务，原事件重投会复用任务编号和回执幂等键。接单桥没有后台回执补发队列，错误提示后应按提示发一条新消息。

手动联调前，在测试应用启用机器人和 `im.message.receive_v1` 长连接订阅，确认有读私聊权限 `im:message.p2p_msg:readonly` 和机器人发送权限 `im:message:send_as_bot`。用已配置该应用的本机 CLI 账户运行以下命令，替换占位值。数据库父目录、工作区和待注册的项目目录须已存在。数据库必须位于 Git 仓库之外。`FEISHU_NAMESPACE` 请为同一应用和租户保持固定。

```bash
FEISHU_DB_PATH='/已有的仓库外目录/projects.sqlite3' \
FEISHU_WORKSPACE='/已有的工作区' \
FEISHU_ALLOWED_OPEN_IDS='ou_REPLACE_WITH_TEST_OPEN_ID' \
FEISHU_NAMESPACE='test-app.test-tenant' \
python3 -B feishu_live_bridge.py --once --timeout 120
```

等日志出现 `Feishu consumer ready` 后，按上文格式给测试机器人发一条私聊。单次模式跳过无关事件，处理第一条符合身份、会话、类型及消息 ID 校验的消息后退出。收到业务错误提示也算完成一次处理。发送失败或等待超时返回 1，配置错误返回 2。等待期限以开始接单为止，进行中的发送和进程回收另有各自的超时。

### 与 Autoloop 审批共存

本机 Autoloop v1.9.2 的 `plugins/feishu/channel.ts` 用独立子进程消费 `card.action.trigger --as bot`，将卡片点击交给原审批流程。lark-cli 1.0.100 的本机文档说明每个消费者订阅一个 EventKey，多个消费者可以共用同一个本地事件总线 daemon。接单桥只订阅 `im.message.receive_v1`，停止时只关闭自己的消费者。部署时应以同一主机、运行用户和 CLI 应用上下文启动两个消费者。

上线前需用测试应用交错验证私聊登记和卡片点击，并在消费者重启后重复验证。共用 daemon 带来共同故障点，应确认断线和缓冲溢出时的漏收行为。同一应用在不同主机或账户上启动独立 daemon 时，事件可能分流。回执请求超时或发送成功后进程在写入成功标记前退出时，重投依赖 CLI 文档所述的一小时幂等窗口。本轮测试使用模拟 CLI 和 SQLite，真实消息、回执及审批共存仍需手动验收。
