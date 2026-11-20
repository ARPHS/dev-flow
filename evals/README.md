# 行为评测

`tests/` 只检查结构和脚本；这里检查**插件实际改变了 agent 的什么行为**。每个用例把夹具复制到全新的 Git 仓库，用 `claude -p` 或 `codex exec` 执行，再用程序化检查评分。两种 agent 的记录在 `lib.Context` 中统一成同一套工具调用，评分器不区分 agent。

## 用例

| 用例 | 夹具 | 关注点 |
|---|---|---|
| `quick` | ledger | 小改动不进入完整流程、不建台账、不改无关文件 |
| `explain` | ledger | 只读问答不触发 `df-flow`、不改文件 |
| `feature` | ledger | 基线、测试先行、真实 CLI 调用、证据、不提交 |
| `debug` | orders | 修复根因（隐藏用例覆盖多种数量和页大小），不放宽断言 |
| `project` | empty | 第一轮设计并停顿；确认后实现，并验证录入、追加与跨进程读取 |

检查按三个核心维度外加参考项聚合，避免超时或流程缺失被误判为通过：

- **执行状态**（双组必需）：无启动失败、无超时、所有预期轮次完整产生结束事件（`turn.completed` / `result`）。
- **功能结果**（双组必需，A/B 对照共同基准）：实际测试通过、真实入口行为正确、不可修改的原始夹具测试无回归、未擅自提交。
- **流程合规**（插件组必需，`--no-plugin` 仅观测）：测试基线绑定夹具的初始源码快照；每项必要测试与业务入口检查均有对应当前快照的成功证据；已识别的独立审查代理成功派发并返回完成结果。
- **参考项**：技能触发痕迹、原有断言行未改动、README、退出码文本等趋势指标。

缺少工具返回、退出码或审查完成元数据时记为无法确认，不计作通过。命令判断识别实际入口及参数，读取脚本、打印命令、静态编译和 `evidence.py report --json` 均不能代替业务入口验证。证据优先使用记录中的 `argv`；旧记录可按 `cmd` 解析，但仍须有明确的执行结果。

`project` 的外部验收接口固定为 `python -m ledger add <金额> <备注>` 和 `python -m ledger list`，由 `LEDGER_FILE` 指定数据文件。评分器使用随机备注、临时数据文件和独立进程验证保存、追加及读回，保存格式由实现者决定。旧版自由入口的 project 结果需按新用例重跑。

## 运行

```powershell
python -X utf8 evals/run.py                      # 全部用例，加载插件
python -X utf8 evals/run.py --case feature debug # 指定用例
python -X utf8 evals/run.py --no-plugin          # 不加载插件，作为对照组
python -X utf8 evals/run.py --grade-only <结果目录>  # 修改评分器后重新评分
python -X utf8 evals/run.py --model sonnet          # 指定 Claude 模型
python -X utf8 evals/run.py --agent codex           # 用 Codex CLI 运行
```

### Codex

- 把 `CODEX_HOME` 指向一个专用于评测的目录，其中 `config.toml` 只保留模型、认证和 `[windows] sandbox` 等设置，不要带 MCP 服务器、notify 或 `sandbox_mode = "danger-full-access"`。运行器默认拒绝直接改动用户默认 `~/.codex` 的插件缓存，确实需要时须显式传入 `--allow-default-codex-home`。
- 使用 `[windows] sandbox = "elevated"` 时，评测目录需要先按根目录 README 用管理员权限执行 `codex sandbox setup`。各个 CODEX_HOME 共用同一组沙箱用户，但每次启动沙箱都会刷新凭据并只保存到当前目录，所以**不要用不同的 CODEX_HOME 并行运行**（例如插件组和 `--no-plugin` 对照组同时跑），否则命令会偶发 `CreateProcessWithLogonW failed: 1326`。
- 运行器会先从当前工作树重新安装插件（Codex 从缓存加载插件，不重装就会测到旧版本）；`--no-plugin` 会卸载插件（`-c` 覆盖 `enabled=false` 不生效），之后不带该参数运行时会自动重装。
- 每轮在出现结束事件（Codex `turn.completed`、Claude `result`）后最多再等 30 秒，进程仍未退出（例如后台子代理未结束）就结束整棵进程树；超时同样结束整棵进程树，原因写入 `turnN.stderr.txt` 并在 `meta.json` 中记录结构化状态。
- agent 在默认的 `workspace-write` 沙箱中运行，后续轮次通过线程 id 续接。Codex 没有 Skill 工具，"调用技能"按读取 `df-*/SKILL.md` 的命令判断；成本和 usage 缺失时以 `-` 显示（不再伪装为 `0`），tokens 列会同时拆分显示 `cached_input_tokens`。

结果默认写入系统临时目录下的 `dev-flow-evals/<时间>/`，其中包含每个用例的工作区、每轮的 `turnN.jsonl` 原始记录、`result.json` 和汇总 `summary.md`。

> [!WARNING]
> Claude 以 `--dangerously-skip-permissions` 在临时工作区中运行；两种 agent 都会产生模型调用费用。

## 如何使用结果

- 使用相同模型、环境和预算比较修改前后结果；模型输出有随机性，关键结论至少重复 3 次。
- 和 `--no-plugin` 对照组比较时，以 `delivery_passed`（汇总中的“交付”：执行和功能均通过）作为共同通过率，合规率单独观察；综合 `passed` 用于插件交付门禁。
- `case.json` 中的 `rubric` 供人工或 LLM 复核那些难以程序化判断的部分（例如设计质量）。

## 新增用例

1. 在 `fixtures/` 下添加最小夹具（尽量只依赖标准库）。
2. 在 `cases/<名称>/` 下添加 `case.json`（`description`、`fixture`、`turns`、`rubric`）和 `grade.py`（`grade(ctx) -> list[Check]`，可用的辅助函数见 `lib.py`）。
3. 在 `tests/test_evals.py` 中加入"未修改的夹具应失败、参考答案应通过"的测试，确保评分器能区分好坏结果。
