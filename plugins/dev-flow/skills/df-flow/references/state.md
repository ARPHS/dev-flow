# 持久化与恢复

## 何时创建

`feature` 和 `project` 在取基线之前必须在目标项目创建 `.dev-flow/`（证据工具只在该目录存在时记录）；`quick` 不创建，除非用户明确要求：

```text
.dev-flow/
├── .gitignore      # 内容为 *，让整个目录不进入版本库
├── project.md      # 项目画像：命令、约定和最近全量验证（跨任务复用）
├── task.md         # 当前任务：目标、验收条件、切片和验证计划
├── progress.md     # 当前状态、断点、基线和下一步
├── decisions.md    # 只有存在产品或架构取舍时创建
├── evidence.jsonl  # 验证记录，只由 df-verify 的 evidence.py 写入
├── objects/        # evidence.py 的快照对象，删除后旧快照可能无法对比
└── logs/           # 验证命令的完整长日志
```

## 不进入版本库

创建 `.dev-flow/` 时同时写入 `.dev-flow/.gitignore`，内容只有一行 `*`：整个目录（包括这个文件本身）不会出现在 `git status` 中，也不修改项目自己的 `.gitignore` 或 `.git/`（Codex 默认沙箱禁止写 `.git/`）。用户希望共享台账时，把这个文件改为只忽略 `objects/`、`evidence.jsonl` 和 `index-*`。

## 模板

台账只保留极简索引，不重复抄录完整对话或命令长日志（验证输出统一由 `evidence.jsonl` 和 `.dev-flow/logs/` 管理）。

### project.md

```markdown
# 项目画像
- 语言/框架：
- test：`<命令>`（来源：CI / package.json / 推断）
- lint/typecheck：`<命令>`
- build：`<命令>`
- run：`<命令>`
- 约定：<分支、提交信息、目录结构等>
- 已知基线失败：<测试名或"无">（记录于 <日期>）
- 最近全量验证：<日期> 快照 `<evidence.py fingerprint>` —— test <退出码>，lint <退出码>，build <退出码>，<已有失败项或"全部通过">
```

### task.md

```markdown
# <任务名>
- 级别：feature | project
- 目标：
- 范围外：
## 验收条件
- [ ] AC1：<可观察行为>
## 切片
1. [ ] <切片> —— 验证：<命令/操作>
## 验证计划
```

### progress.md

验证结果不写在这里，统一由 `evidence.py` 记录到 `evidence.jsonl`。状态细分为实现、验证、审查三个活跃阶段，避免把“已写完代码”误记为完成。

```markdown
状态：implementing | verifying | reviewing | blocked | done

## 当前
- 切片：
- 分支/工作区：
- 未提交文件：<git status --short 摘要>
## 基线
- 开工前已有改动：<文件列表或"无">
- 开工快照：<修改任务文件前的 evidence.py fingerprint；不可用时说明原因>
- <命令> → 退出码 <n>，<已有失败项或"全部通过" / "空项目，当前无测试设施">（全量 / 局部：<范围> / 复用 <日期> 的全量验证）
## 阻碍
## 下一步
```

### decisions.md

```markdown
## <日期> <决定>
- 备选：
- 依据：
- 影响：
```

## 更新时机

- 每个切片验证通过后。
- 被阻塞、向用户提问、上下文接近上限、结束会话前。
- 标记 `状态：done` 前必须先运行 `df-verify`，确认 `evidence.py report` 中作为证据的记录都是"当前有效"，或已写明保留理由。

派发审查时提供开工快照和候选版本快照（来自最终 `report`）。审查者用 `evidence.py diff <开工快照> <候选快照>` 查看本次改动；文件清单只作为索引，不用它排除整个已有脏改动文件。审查结束前保留快照对象；对象已清理时如实说明无法对比。

## 恢复

先读 `.dev-flow/`，再检查文件、分支、提交、未跟踪文件，并重新运行最近一次的验证命令。记录与现场冲突时以现场为准并修正记录。`evidence.py report` 中状态不是"当前有效"的记录，按 df-verify 的规则重跑或说明理由。不要为了让状态看起来完整而丢弃未提交改动。

## 开始新任务

在已有 `.dev-flow/` 的项目中开始新任务（而不是恢复旧任务）时：

1. 保留 `project.md`，按需要更新命令和约定。
2. 旧任务未完成时先询问用户是否放弃；已完成时用新任务覆盖 `task.md` 和 `progress.md`，`decisions.md` 中仍有效的决定保留。
3. 删除 `evidence.jsonl`、`objects/` 和 `logs/`。它们只服务于单个任务；跨任务复用的基线已记在 `project.md` 的"最近全量验证"中，且快照 id 的比较不依赖这些对象。
4. 修改任务文件前取得并记录本任务的开工快照。

任务完成后保留 `.dev-flow/` 供后续恢复。`objects/` 超过 200MB 时 `evidence.py` 会自动清空，你也可以随时删除它，代价只是更早的记录显示为"无法对比"。
