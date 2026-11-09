# 第 3 部分:标注版工作流图(Workflow Diagram)

```mermaid
flowchart TD
    subgraph TRIG["触发层(编排器 controller)"]
        T1["定时:交易日收盘后<br/>cron → run-day"]
        T2["事件:公司行动/公告<br/>→ run-day(插队)"]
        T3["周期:每月最后交易日<br/>→ S7"]
        T4["手动:replay / run-day / 重跑"]
    end

    subgraph DAY["每日流水线(线性依赖,逐步检查点)"]
        S1["S1 数据接入<br/>确定性代码<br/>as-of 截止,只读缓存<br/>+ 持仓快照对账(分歧即停工)"]
        S2["S2 风险监控<br/>确定性代码<br/>权重/回撤/集中度 + 记净值"]
        S3{"S3 信号生成<br/>代码计算 + LLM 叙述"}
        S4["S4 独立复核<br/>确定性代码,从原始数据重算<br/>不复用 S3 任何数字"]
        S5["S5 人工审批 ⛔<br/>异步门:审批单落盘+通知推送<br/>approve 命令批复,检查点续跑"]
        S6["S6 沙盒执行与对账<br/>确定性代码;T+1 模型<br/>先消化在途订单,再把今日批准<br/>转为待执行订单(次日成交)"]
    end

    subgraph MONTH["跨周期评估"]
        S7["S7 规则评估<br/>固定基线回放 + 候选参数网格"]
        AP2["改参人工批准 ⛔<br/>apply-rules"]
        RB["规则版本表(只追加)<br/>可回滚 rollback-rules"]
    end

    subgraph STATE[("SQLite 持久状态 runs/state.db<br/>持仓/现金 · 成交 · 审批 · 检查点 stage_runs<br/>artifact(内容寻址) · 规则版本 · 净值曲线"))]
    end

    T1 --> S1
    T2 --> S1
    T4 --> S1
    S1 --> S2 --> S3
    S3 -- "无越界 HOLD" --> S5
    S3 -- "越界 → 提案" --> S4
    S4 -- "fail:当日停止,<br/>有界修订循环" --> S3
    S4 -- "pass" --> S5
    S5 -- "驳回" --> END1["当日归档,不执行"]
    S5 -- "批准(哈希匹配)" --> S6
    S6 --> END2["成交日志 + 对账 + 新持仓"]
    T3 --> S7
    S7 --> AP2 --> RB
    RB -. "新版本参数次日生效" .-> S2

    S1 <--> STATE
    S2 <--> STATE
    S3 <--> STATE
    S4 <--> STATE
    S5 <--> STATE
    S6 <--> STATE
    S7 <--> STATE
    RB <--> STATE
```

## 图的注解

**主线(每日)**:S1→S6 严格线性。每一步开始前,编排器先查 `stage_runs` 检查点表:该日该阶段已 `done` 则整条跳过——这就是中断恢复不重跑的机制(证据:`docs/evidence/recovery.log`)。

**两个判断点**:

1. **S3 是否越界**:无越界产出 HOLD 提案,S5 自动归档("无需行动"),S6 空执行留痕;有越界才进入复核。
2. **S4 复核**:判 fail 当日流程终止于 S4,回到 S3 形成有界修订循环(界:重试 ≤3 次、当日 2 小时截止,超限升级人工)。

**两个人工门(⛔)**:S5 下单前、`apply-rules` 改参前。两处都在 SQLite 留下只追加审批记录;S6 执行前强制校验审批记录中的提案哈希与当前提案一致。

**状态流**:所有阶段只经状态库交换数据,不互相直接调用。持仓/现金的唯一写路径是 S6 的事务(`apply_fill`),规则参数的唯一写路径是 `apply-rules` / `rollback-rules`(只追加版本行)。

**跨周期回路**:S7 每月末从累积的净值、越界、成交历史中评估规则,改参提案批准后才成为新版本(v2),次日 S2 起生效;`docs/evidence/rules-lifecycle.log` 展示了 v1→v2(批准)→v3(回滚至 v1 参数)的完整留痕。

**防泄露边界**:S1 是唯一读取外部数据的阶段,且经数据层 `as_of` 强制截止;S7 回放同样逐日受限(详见 `docs/02-design-note.md` §2.3)。
