---
name: df-verify
description: 在声明代码功能、缺陷修复或配置变更完成之前调用：选择并运行适用的测试、静态检查、构建和真实入口验证，用配套的 evidence.py 记录可复核证据（verify, run tests, check before done）。仅改文案、拼写或只读问答等无需完整验证流程的单点小修不要自动调用。
---

# Verify

根据改动的行为和风险选择最小但充分的验证集合。先运行最接近改动的检查，再运行项目要求的整体检查（命令来源优先使用 `.dev-flow/project.md`，其次是 CI 配置）。证据类型按 [quality.md](../df-flow/references/quality.md) 选择。

## 证据工具

本技能目录下的 `scripts/evidence.py` 只做文字做不到的事：由程序执行命令、记录退出码，并用 git 给当时的工作区拍快照，事后列出每条记录之后改了哪些文件。它只报告事实，记录是否仍然有效由你按下文规则判断。下文用 `$EV` 表示 `python <本技能目录>/scripts/evidence.py`（需要 Python 3.8 和 git 2.31 及以上；没有 `python` 命令时用 `python3`）。

| 命令 | 用途 |
|---|---|
| `$EV run -l <AC1/baseline/lint…> -- <程序> <参数...>` | 运行命令并输出（超长输出自动落盘 `.dev-flow/logs/` 并打印摘要，失败保留关键细节），退出码与原命令一致；同时追加记录到 `.dev-flow/evidence.jsonl` |
| `$EV report [--json]` | 列出每条命令的最新记录及其状态：`当前有效`、`之后改动：<文件>`、`无法对比` |
| `$EV fingerprint` | 打印当前工作区的快照 id（内容相同则 id 相同，与是否提交无关） |
| `$EV diff <开工快照> <候选快照>` | 只读查看两个已保存快照的完整差异，包含未跟踪文件；快照缺失时报错，不自动退回 HEAD 对比 |
| `$EV doctor [--json]` | 只读诊断 Python、Git、仓库和 `.dev-flow/` 状态（不修改任何配置） |

`run` 直接执行 `--` 之后的程序，不经过 shell，参数由你当前的 shell 拆分，所以在 bash、PowerShell、cmd 下写法相同：

```text
$EV run -l AC1 -- python -m pytest tests/cli -q
$EV run -l lint -- npm run lint
```

需要管道、`&&`、设置环境变量等 shell 特性时，显式包一层：`-- bash -c "LEDGER_FILE=/tmp/l.json python -m ledger list"`，或 `-- pwsh -NoProfile -Command "..."`。不要把整条命令放进一对引号里传给 `run`，它会报错提示。Windows PowerShell 5.1 会丢掉传给外部程序的参数里嵌套的双引号，包装命令内部改用单引号，或改用 `pwsh`（7.x）。

遇到 Windows 沙箱 `tempfile` 权限错误（`PermissionError` / `WinError 5`）、Git `dubious ownership` 或 WSL `python3` 用法问题时，按 [sandbox.md](references/sandbox.md) 处理，不要反复绕行或改写项目测试。

快照对象只写入 `.dev-flow/objects`，不写进项目的 `.git`，也不改动暂存区。只有存在 `.dev-flow/` 时才会记录：`feature`、`project` 任务必须已有该目录（由 `df-flow` 创建，缺失时先创建，并写入内容为 `*` 的 `.dev-flow/.gitignore`）；`quick` 或单独验证且没有该目录时，直接运行命令即可。TDD 红阶段的探索性失败测试直接运行命令即可，不必用 `$EV run` 写入台账；基线、切片验收与最终检查再通过 `$EV run` 记录，保持证据表清晰。

## 步骤

1. 列出验证集合：每条验收条件对应至少一项检查，加上项目整体的 test、lint/typecheck、build。
2. 逐项运行。存在 `.dev-flow/` 时每项都通过 `$EV run -l <检查名>` 运行；失败时读取完整输出。
3. 对照基线区分：本次引入的失败、已知基线失败、环境阻碍。本次引入的失败必须修复或返回 `df-flow` / `df-debug`，修复后重新运行受影响检查。
4. **代码最后一次修改之后**运行 `$EV report`，按下表处理每条记录，再把输出原样放入证据。
5. 真实 UI 操作、浏览器截图等无法用命令表达的检查，在证据表后单独列出操作和观察结果。
6. 本次跑过项目的全量 test/lint/build 并全部符合预期时，把结果和 `$EV fingerprint` 写入 `.dev-flow/project.md` 的"最近全量验证"，供下个任务复用为基线。

| `report` 状态 | 处理 |
|---|---|
| 当前有效 | 可以作为证据 |
| 之后改动：源码、测试、配置、依赖或构建脚本 | 重跑这项检查 |
| 之后改动：只有该检查不读取的数据、日志、缓存或产物 | 可以保留，在"未重跑的记录"中逐条写明文件和理由 |
| 无法对比 | 重跑 |

拿不准某个文件是否影响检查时，重跑。报告末尾列出"未纳入对比的大文件"时，在最终报告中提醒用户考虑加入 `.gitignore`。

没有 Python 或 git 版本过低时手写证据表，版本写 `git rev-parse HEAD` 加上 `git status --short` 摘要，并注明"手工记录"。

## 输出格式

```text
<$EV report 的输出，或同格式的手工表格>
## 验收条件对照
- AC1 ✅ 见 #1
## 未重跑的记录
- #<n>：之后改动 <文件> —— <为什么不影响这项检查>
## 未运行的检查
- <检查>：<原因>
## 基线失败（非本次引入）
- <测试名>
```

## 禁止

- 用静态阅读、"代码已存在"、代理报告或未执行的命令替代运行结果。
- 手工编辑 `.dev-flow/evidence.jsonl`，或把状态不是"当前有效"的记录不加说明地当作证据。
- 只跑单元测试就声称 UI、性能或真实入口已验证。
