---
name: df-verify
description: 在声明开发任务完成之前调用：选择并运行适用的测试、静态检查、构建和真实入口验证，用配套的 evidence.py 记录可复核证据（verify, run tests, check before done）。
---

# Verify

根据改动的行为和风险选择最小但充分的验证集合。先运行最接近改动的检查，再运行项目要求的整体检查（命令来源优先使用 `.dev-flow/project.md`，其次是 CI 配置）。证据类型按 [quality.md](../df-flow/references/quality.md) 选择。

## 证据工具

本技能目录下的 `scripts/evidence.py` 只做文字做不到的事：由程序执行命令、记录退出码，并用 git 给当时的工作区拍快照，事后列出每条记录之后改了哪些文件。它只报告事实，记录是否仍然有效由你按下文规则判断。下文用 `$EV` 表示 `python <本技能目录>/scripts/evidence.py`（需要 Python 3.8 和 git 2.31 及以上；没有 `python` 命令时用 `python3`）。

| 命令 | 用途 |
|---|---|
| `$EV run -l <AC1/baseline/lint…> -- <程序> <参数...>` | 运行命令并原样输出，退出码与原命令一致；同时追加记录到 `.dev-flow/evidence.jsonl` |
| `$EV report` | 列出每条命令的最新记录及其状态：`当前有效`、`之后改动：<文件>`、`无法对比` |
| `$EV fingerprint` | 打印当前工作区的快照 id（内容相同则 id 相同，与是否提交无关） |

`run` 直接执行 `--` 之后的程序，不经过 shell，参数由你当前的 shell 拆分，所以在 bash、PowerShell、cmd 下写法相同：

```text
$EV run -l AC1 -- python -m pytest tests/cli -q
$EV run -l lint -- npm run lint
```

需要管道、`&&`、设置环境变量等 shell 特性时，显式包一层：`-- bash -c "LEDGER_FILE=/tmp/l.json python -m ledger list"`，或 `-- pwsh -NoProfile -Command "..."`。不要把整条命令放进一对引号里传给 `run`，它会报错提示。Windows PowerShell 5.1 会丢掉传给外部程序的参数里嵌套的双引号，包装命令内部改用单引号，或改用 `pwsh`（7.x）。

Codex 的 Windows 沙箱中，Python 3.12.4 及以上用 `tempfile`（`mkdtemp`、`TemporaryDirectory`）创建的目录随后无法访问，表现为测试报 `PermissionError` / `WinError 5`，路径是 `tmp*` 目录。**第一次出现就判定为环境阻碍，停止排查**：改 `TEMP`/`TMP`、预建目录、写包装脚本重跑都无效，也不要改写项目测试。能申请提权就提权重跑；不能时把受影响的测试列入"未运行的检查"，验收条件改用真实入口（CLI、API 调用）验证。沙箱里删不掉的残留目录在最终报告中列出，请用户手动删除。git 报 `detected dubious ownership`（`elevated` 沙箱用户不是仓库所有者）同样是环境阻碍：给该命令设置环境变量 `GIT_CONFIG_COUNT=1`、`GIT_CONFIG_KEY_0=safe.directory`、`GIT_CONFIG_VALUE_0=*`，不要修改项目或全局的 git 配置，也不必阅读 `evidence.py` 源码排查。

快照对象只写入 `.dev-flow/objects`，不写进项目的 `.git`，也不改动暂存区。只有存在 `.dev-flow/` 时才会记录：`feature`、`project` 任务必须已有该目录（由 `df-flow` 创建，缺失时先创建，并写入内容为 `*` 的 `.dev-flow/.gitignore`）；`quick` 或单独验证且没有该目录时，直接运行命令即可。

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
