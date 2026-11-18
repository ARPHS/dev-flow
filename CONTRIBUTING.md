# 贡献指南

## 修改工作流

先说明修改解决的真实使用问题。优先改进路由、可执行约束、验证能力和恢复可靠性，不把一次偶发问题扩大为适用于所有任务的硬规则。

技能正文只保留触发条件、硬规则、步骤和必填输出模板。较长的细节放入 `references/`，并从入口明确链接。新增自动行为时必须说明它为什么不会接管无关任务。

## 本地检查

```powershell
python -X utf8 -m unittest discover -s tests
claude plugin validate . --strict
```

## 行为评测

修改技能文本后，至少运行受影响的用例，并与修改前对比：

```powershell
python -X utf8 evals/run.py --case feature quick
```

新增技能行为时同时新增一个应触发的用例和一个不应触发的用例。评分器必须在 `tests/test_evals.py` 中证明"未修改的夹具失败、参考答案通过"。验证失败时保留完整输出，不通过删减检查使其通过。
