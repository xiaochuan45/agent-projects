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
| `create_project(name, directory, *, aliases=(), codex_session_id=None)` | 返回新建的 `Project` |
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

## 后续飞书接入

- 在独立适配层接收事件，完成验签和权限检查后提取项目查询词、任务标题及稳定的消息 ID。为消息 ID 加上平台和租户命名空间，重投时复用同一个值。
- 调用 `find_projects`。没有匹配项时请用户确认查询词，多条匹配时回复编号和名称，让用户选定项目。确认后携带原消息 ID 调用 `create_task`。
- 如需启动 Codex，先调用 `project_directory`。执行器获得 Session ID 后回写任务。任务落库与启动 Codex 是两个动作，接入时应以任务编号为执行去重键，避免重投触发多次执行。
