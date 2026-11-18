# dev-flow

面向 Claude Code 和 Codex 的轻量软件工程工作流插件。

它适合新功能、新项目、跨文件修改、复杂缺陷和性能任务。小型低风险修改仍由 agent 直接完成，不会因为安装插件就强制建立完整流程。

## 安装

### Claude Code 本地试用

```powershell
claude --plugin-dir .\dev-flow
```

### Claude Code 从 GitHub marketplace 安装

将 `OWNER/dev-flow` 替换为实际仓库地址：

```powershell
claude plugin marketplace add OWNER/dev-flow
claude plugin install dev-flow@dev-flow
```

### Codex CLI

```powershell
codex plugin marketplace add OWNER/dev-flow
codex plugin add dev-flow@dev-flow
```

安装后启动新会话。技能统一使用 `df-` 前缀，主流程也会根据任务意图和风险按需触发。

## 入口

- `df-flow` 推进新功能、新项目、复杂缺陷和收尾验证。
- `df-plan` 明确规划、设计和方案变更。
- `df-review` 审查方案、代码、任务和交付。
- `df-debug` 处理根因不明确的缺陷或性能问题。
- `df-verify` 运行适用检查并整理证据。

技能公开名称统一为 `df-*`。Claude Code 中可使用短命令 `/df-plan`，插件完整命令为 `/dev-flow:df-plan`。Codex 和其他按技能名加载的 agent 使用 `$df-plan` 等名称。

## 设计取舍

工作流不使用启动 hook，不要求项目安装 `.claude/` 配置，也不把所有任务都强制升级到重流程。长任务才在目标项目创建 `.flow/` 状态目录。完成声明必须包含实际验证证据。

详细设计见 [`docs/design.md`](docs/design.md)。

## 开发和校验

```powershell
python -X utf8 tests/test_layout.py
claude plugin validate .
claude plugin validate . --strict
```

## 许可证

MIT
