# Final Project:组合监控与再平衡 Agent 工作流

- **学号**:25210660108
- **课程**:AI Coding in Finance, Fall 2026
- **仓库**:https://github.com/Tracyqq1b/25210660108-final-project

## 任务摘要

为一个股票/ETF 多资产组合(AAPL、MSFT、NVDA、QQQ、IEF、COST,美元计价)设计并实现了一套**长期运行的 Agent 工作流**:每日收盘后自动完成数据接入(S1)→ 风险监控(S2)→ 再平衡信号(S3)→ 独立复核(S4)→ 人工审批(S5)→ 沙盒执行对账(S6,T+1 模型:批准日挂待执行订单,次日按当日价格成交并披露价差),每月末运行规则评估(S7),对固定基线回放候选参数并产出需人工批准的改参提案。全部状态持久化于 SQLite,任意阶段崩溃后基于检查点精确续跑,已完成阶段绝不重复执行。所有数值计算为确定性代码;LLM Agent 负责叙述与判断层(见 `prompts/`)。

## 文件导览

| 路径 | 内容 |
| --- | --- |
| `docs/01-task-spec.md` | 第 1 部分:任务规格(阶段、触发、数据源、截止点、验收标准) |
| `docs/02-design-note.md` | 第 2 部分:设计说明(角色与交接、长运行、验证与改进) |
| `docs/03-workflow-diagram.md` | 标注版工作流图(Mermaid)+ 图解 |
| `docs/evidence/` | 验证与恢复证据:测试输出、完整回放日志、崩溃恢复日志、规则版本生命周期日志 |
| `prompts/` | 三个 Agent 的角色提示词(S3 信号、S4 复核、S7 规则评估) |
| `src/config.yaml` | 组合、规则阈值、执行参数、运行限额与权限声明 |
| `src/pma/` | 工作流实现:`datafeed.py`(数据层,as-of 截止)、`state.py`(SQLite 状态/检查点/规则版本)、`stages.py`(S1–S6)、`review.py`(S4 独立复核)、`controller.py`(编排/重试/续跑/审批门)、`rules_eval.py`(S7)、`render.py`(日终/月度报告渲染)、`cli.py`(命令行) |
| `data/cache/` | 真实行情缓存(东方财富 K 线 API 拉取的日线,2024-09 至 2025-10) |
| `examples/inputs/` | 示例输入(组合定义) |
| `examples/outputs/` | 示例产出:2025-09-02 再平衡日完整三连套(提案+复核+审批+对账)、两个月末的 S7 评估报告、`reports/` 下渲染好的日终与月度 Markdown 报告 |
| `tests/` | 20 个测试:崩溃恢复、无未来数据泄露、审批门与复核独立性、T+1 执行、持仓对账、报告渲染 |

## 安装与运行

环境:Python 3.9+。

```sh
pip3 install -r requirements.txt   # pandas, pyyaml, requests(数据拉取用)
```

`data/cache/` 已随仓库提交,以下全部命令**离线可复现**;如需刷新行情:`python3 src/pma/cli.py refresh --start 2024-09-01 --end 2025-10-31`(此命令需要联网)。

```sh
# 1. 初始化状态库(播种示例组合与规则 v1)
python3 src/pma/cli.py init

# 2. 回放一段历史交易日(每日 S1–S6,月末自动 S7;演示用自动审批并留痕)
python3 src/pma/cli.py replay --from 2025-09-02 --to 2025-10-31 --auto-approve --with-rules-eval

# 3. 交互模式跑单日(S5 会停下来等人工输入 y/N)
python3 src/pma/cli.py run-day 2025-11-03

# 4. 崩溃恢复演示:S3 后注入崩溃(退出码 75),再运行同一命令即从 S4 续跑
python3 src/pma/cli.py replay --from 2025-09-02 --to 2025-09-12 --auto-approve \
    --crash-after S3 --crash-on 2025-09-10 --db runs/crash-demo.db
python3 src/pma/cli.py replay --from 2025-09-02 --to 2025-09-12 --auto-approve --db runs/crash-demo.db

# 5. 规则生命周期:应用 S7 改参提案 / 回滚
python3 src/pma/cli.py apply-rules --yes
python3 src/pma/cli.py rollback-rules --to 1 --yes

# 6. 查看状态 / 导出某日的全部产出 / 渲染报告
python3 src/pma/cli.py status
python3 src/pma/cli.py export --day 2025-09-02 --out /tmp/pma-export
python3 src/pma/cli.py report --day 2025-09-02        # 日终报告(回放时已自动写入 runs/reports/)
python3 src/pma/cli.py report --month 2025-10        # 月度规则评估报告

# 7. 持仓对账:券商快照放入收件箱后,S1 自动对账;分歧时用此命令把
#    工作流外变动(手动交易/分红/拆股)记为只追加调整记录,再重跑当日
cp examples/inputs/positions_2025-09-02.csv data/inbox/
python3 src/pma/cli.py import-positions --file data/inbox/2025-09-02_positions.csv \
    --day 2025-09-02 --yes

# 8. 测试(20 个:恢复、泄露、审批门、T+1、对账、报告渲染)
python3 -m unittest discover -s tests -v
```

## 验证证据位置

- 自动化测试输出:`docs/evidence/tests.log`(20/20 通过)
- 44 个交易日完整回放:`docs/evidence/replay.log`
- 崩溃→续跑:`docs/evidence/recovery.log`(S1–S3 被检查点跳过,attempts 保持 1)
- 持仓对账升级:`docs/evidence/reconciliation.log`(快照分歧 → S1 停工 → 人工入账 → 续跑)
- 规则 v1→v2→(回滚)v3:`docs/evidence/rules-lifecycle.log`
- 再平衡全链条:`examples/outputs/2025-09-02/`(批准日:提案+复核+审批+挂单)+ `examples/outputs/2025-09-03/`(次日成交与价差对账)
- 渲染好的日终/月度报告样例:`examples/outputs/reports/`

## 个人贡献说明

选题与任务框架(阶段划分、审批流、规则评估思路)由本人设计;架构定稿、全部代码实现、测试与文档在与 AI 编程助手(Kimi Code)结对中完成,本人负责需求取舍、设计决策(如"计算确定性代码化、LLM 仅叙述层""审批哈希链""反向重建窗口起点防泄露")与结果验收。开发过程见 Git 提交历史(渐进式提交,非一次性导入)。
