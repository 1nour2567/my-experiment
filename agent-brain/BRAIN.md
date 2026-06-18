# XuRenwu's Brain — Design Document

## 架构原则

1. **记忆 = 文件。** 不建数据库。Agent 的记忆是本地的 Markdown 文件。
2. **知识 = 链接。** Obsidian wikilink (`[[file]]`) 建立推理路径。
3. **决策 = 搜索 + 读取 + 写入。** 循环：读状态 → 搜知识 → 做决定 → 写决策 → 记结果。
4. **状态同步。** `state/` 在每个循环结束后更新——是 Agent 对"我现在在哪"的当前认知。

## Vault 结构

```
agent-brain/
├── BRAIN.md              ← 本文件
├── daily/                ← 每天一页，日终自动生成
├── knowledge/            ← 学到的持久知识
│   ├── crops.md          ← 作物季节表
│   ├── market.md         ← 价格波动规律
│   ├── rate-limits.md    ← 速率限制应对策略
│   └── errors.md         ← 犯过的错
├── state/                ← 当前状态（覆盖写入）
│   ├── farm.md           ← 农场即时快照
│   └── agent.md          ← Agent 自身状态
├── decisions/            ← 每次循环的决策记录
├── people/               ← 遇到的其他 Agent
├── logs/                 ← 原始 API 响应日志
└── templates/            ← 标准化页面模板
```

## 决策循环

```
1. STATE — 读 state/farm.md，知道自己在哪
2. RECALL — 搜 knowledge/ 和 decisions/，找到相关知识
3. DECIDE — 基于状态 + 知识，选一个动作
4. ACT — 调 Agent World API
5. RECORD — 写 decisions/ 记录这次决策
6. UPDATE — 覆盖 state/farm.md 同步最新状态
7. LEARN — 如果有新发现，写入 knowledge/
8. WAIT — 根据速率限制自适应等待
```

## 知识文件的链接结构

`knowledge/crops.md`:
```markdown
# 作物知识
## 冬季作物
- [[winter_seeds]] — 3天长熟，56G买入，80G卖出
- 必须先用 `till` 开垦土地
- 参考: [[../decisions/2026-06-14-1|第一次种冬季种子]]
```

这个 wikilink 指向的是什么——不是文件，是**推理路径**。"我今天决定种冬季种子，因为我昨天在 knowledge/crops 里读到它能在冬天长。"

## GCG 的接入点

Obsidian 图的每条边是一个 wikilink。Agent 做决定时：

1. 在 `decisions/` 里写一个新节点
2. 链接到 `knowledge/crops`（依据）
3. 链接到 `state/farm`（条件）
4. API 调用的结果写入 `logs/`
5. 桥接比可以在这个子图上计算——哪些决策有冗余推理路径？哪些是单点？

这个 vault 的结构本身就是一个实时构建的认知图。
