# Self Core v5 — 第二轮实验设计

**状态：** 设计阶段。待实现。

---

## 1. 与 v4 的关键差异

| | v4（首轮） | v5（本轮） |
|---|---|---|
| 训练样本 | 1000 | 3000 |
| λ 值 | 8（均匀分布 0.0–1.0） | 12（加密 0.4–0.8） |
| 重复 | 1 | 2 |
| 结构特征 | 无 | **有（Eidoku 路线）** |
| 测试样本 | 200 | 300 |
| Epochs | 10 | 10 |

---

## 2. λ 取值

```
0.0, 0.1, 0.2, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8
```

12 个点。0.4–0.7 区间加密至 0.05 间隔（v4 中 C 组信号最强的区域）。λ=1.0 去掉——那是训练崩溃，不是相变。

---

## 3. 结构特征（Eidoku 路线）

v4 的 Self-Update Gate 只接收 `[stacked_state, correctness_bit]`——信号太薄。v5 拼接三个结构特征：

| 特征 | 含义 | 为什么有用 |
|---|---|---|
| `streak` | 连续正确/错误的长度，取符号（正=正确连击，负=错误连击） | Gate 能区分"偶尔错一次"和"连续错" |
| `variance` | 最近 10 步正确性的方差 | 高方差 → 不稳定 → 应该降低自信 |
| `drift` | \|\|当前 self_state - 初始 self_state\|\| | 自我表征漂移幅度——"我变了多少" |

Gate 输入从 `[proj(512), correct(1)]` 变为 `[proj(512), correct(1), streak(1), variance(1), drift(1)]`——513→516 维，计算成本几乎不变。

**关键：** 结构特征不是 ground truth。streak 和 variance 从 Self Core 自己记录的历史计算——外部不可写。drift 从自我状态向量本身计算。三个特征都是**从内部状态导出的**——不是外部标签。

---

## 4. 训练中的结构特征追踪

在训练循环中维护三个状态变量（跨 batch 持久，和 self_state 同级）：

```python
self_state     = torch.randn(512) * 0.02     # Self Core 核心状态（已有）
state_stack    = deque([zeros(512)] * 4)      # 状态栈（已有）
recent_correct = deque([0.5] * 10)            # 最近 10 步正确性（新增）
streak         = 0                             # 连续同类结果计数（新增）
init_state     = self_state.clone()            # 初始状态快照（新增）
```

每个自指回合更新：

```python
# 更新 recent_correct
recent_correct.append(correct)
# 更新 streak
if correct == last_correct:
    streak += 1 if correct else -1
else:
    streak = 1 if correct else -1
# 计算结构特征
streak_feat   = min(max(streak / 10.0, -1.0), 1.0)  # 归一化到 [-1, 1]
variance_feat = np.var(recent_correct)                # 本就在 [0, 0.25]
drift_feat    = (self_state - init_state).norm().item()
```

---

## 5. 代码改动清单

基于 `self-core-experiment.py` 的改动：

1. **`SelfCore.forward_gate`** — 增加结构特征输入，gate 第一层 Linear 从 `state_dim+1` 变为 `state_dim+4`
2. **`train_self_core`** — 增加 `recent_correct`、`streak`、`init_state` 追踪；拼接结构特征传入 gate
3. **Config** — `TOTAL_SAMPLES=3000`、`TEST_SAMPLES=300`、`LAMBDA_VALUES` 改为 12 点
4. **重复运行** — 外层循环 2 次，取 mean ± std
5. **输出** — JSON 存 `{lambda, mean_ece, std_ece, mean_acc, std_acc}` + 误差棒图

---

## 6. 预期结果判断

### 情况 1：A 组出现干净驼峰，B/C 无
→ H1 被初步证实。Self Core + 结构特征是相变发生的必要条件。下一步：更大模型。

### 情况 2：C 组出现驼峰，A 组仍震荡
→ 相变是数据层现象。Self Core 的 Gate 在 3000 样本下仍未收敛。需要重新考虑 Gate 的设计或放弃可训练方案。

### 情况 3：三组都没有干净驼峰
→ H1 在 GPT-2 medium 上未被证实。需要迁移到 GPT-2 large (774M) 或增加 Self Core 维度到 1024+。

### 情况 4：A 组和 C 组都有驼峰，但 A 组的驼峰更宽/更平滑
→ 最有趣。Self Core 的 Gate 在学习"缓冲"相变——不是防止它，而是让它更渐进。这可能比"有/没有"驼峰更有理论价值。

---

## 7. 时间估计

- 代码修改：1–2 小时
- 训练：每 λ 约 15–20 min × 12 λ × 3 组 × 2 次 = ~24 GPU 小时
- 分析：2 小时

总计约 30 小时（可以在服务端后台跑）。
