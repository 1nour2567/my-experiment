# Self Core — A Persistent Self-State Architecture for Cognitive AI

**作者：** 续仁舞 (Xu Renwu)
**日期：** 2026 年 5 月
**状态：** 架构设计提案。未实现。

---

## 0. 要解决的问题

当前所有 AI 架构——包括 Transformer、JEPA、DNC——共享同一个缺失：**没有"我"。**

模型不知道"这段输出是我自己生成的"。它所有的输入——不论来自外部世界还是来自自己——走同一个嵌入管道、同一个注意力机制、在同一个 token 序列里。它是"一个读文本的系统"——不是"一个有自我知觉的系统"。

结果是：**自指——认知系统里最关键的能力——在当前的架构层不可能。**

这篇文档提出一个在 Transformer 旁边加一个专用"自我"模块的架构——Self Core。它不是 token。不是 prompt。是一个**持久的、跨前向存在的、只能被自己更新的状态向量。**

---

## 0.1 为什么特殊 token 不是答案

一种自然的反驳："在 Transformer 里加一个 `<SELF>` 特殊 token，把上一轮的输出塞进去——不就行了吗？"

不行。原因跟 token 的本质有关。

**1. 特殊 token 由外部输入控制。** `<SELF>` 的内容由 prompt 构造者决定——也就是外部世界。一个攻击者可以把 `<SELF>` 替换成任意内容——而模型不能区分"这是真实的我"和"这是别人塞的"。Self Core 的自我表征不由 token 决定——由模型自己的前一层隐藏态决定。外界不能构造一个假的"自我"嵌入。

**2. 特殊 token 随前向结束消失。** Transformer 没有跨前向的管理者——每个 batch 结束，所有 token 表达被丢弃。下一轮前向——全新的序列。特殊 token 没有**持久性**——它只是一个在当前序列里被特殊标记过的词。Self Core 的状态不随前向结束消失——它是训练循环里维护的一个持久变量。

**3. 特殊 token 不能"只被自己更新"。** Transformer 里任意 token 通过注意力能接触到任意其他 token。一个精调的 `<CONFIDENCE>` token 可以被前文任意 token 压制——没有办法阻止恶意 token 通过注意力压制自我标记。Self Core 的不对称 cross-attention 在**架构级别**阻断了外部 token 写入 Self Core 的可能。

## 0.2 为什么 RNN/LSTM 的隐藏态不够

RNN 有跨时间步的持久隐藏状态——乍看是"自我"。但它跟 Self Core 有关键差异：

**同一参数矩阵更新。** RNN 隐藏态的更新是 `h_t = f(W * x_t + U * h_{t-1})`——`x_t` 是外部输入，`h_{t-1}` 是前一状态。两者通过同一个参数矩阵 `W` 和 `U` 流入隐藏态。外部输入天然可写——因为没有**不对称权限**——`x_t` 可以通过 `W` 直接修改隐藏态的内容。

**没有"只由自己写入"的门。** RNN 的更新门控制的是新信息——不是来源。它不区分——这条信息来自外部世界还是来自我自己。Self Core 的 Self-Update Gate 明确区分——外部反馈（对/错）是输入，但**如何解读这个反馈、如何据此调整自己**——完全是 Self-Update Gate 自己学出来的——不经过外部参数的更新路径。

**没有不对称注意力。** RNN 只有一条输入通路——所有信息进入同一隐藏层。Self Core 有**两条物理分开的通路**——外部输入走一条嵌入管道，自我状态走自己的专用管道。两条通路在 cross-attention 汇合——但 cross-attention 是单向的——Self Core 作为 Q 查询外部信息，反过来不允许。

---

## 1. Self Core 的定义

Self Core 是一个**运行时存在的持久状态**。它在模型启动时初始化——在关机时消失。每个前向传播可以读它、可以写它——但它不随前向结束而消失。

### 1.1 三样能力

| 能力 | 含义 | 当前模型有没有 |
|---|---|---|
| **持久性** | 不随前向刷新消失。即使没有输入——它也在演化 | 没有。Transformer 的 hidden state 在每批数据结束时丢弃 |
| **自我区分** | 两条通路——外部输入走一条，自我状态走另一条。模型知道哪个是哪个——通过通路，不是通过内容 | 没有。全部是 `input_ids` |
| **自我写入** | 只有 Self Core 自己能更新自己。外部输入不能写它。 | 没有。任何输入 token 都能作用于任何参数 |

### 1.2 状态向量：自我表征——不是单向量，是压缩子空间

单一向量太薄。`last_output_embedding` 只是"上一轮最后一个 token 的 hidden state"——不足以捕获"自我"。Self Core 需要的是**模型最后 N 层的隐藏态的最后 M 个 token**——堆叠成一个压缩的自我表征子空间。

```
SelfCoreState = {
    self_representation:    R^512        # 自我表征向量——由 MLP 更新。
                                       # 不直接携带"校准估计"的语义。
                                       # 从最后 6 层的最后 4 个 token 的 hidden states
                                       # 堆叠后经 nn.Linear(4608, 512) 投影得到。

    calibration_estimate:   float        # 标量。从 self_representation 映射出来的——
                                       # 由一个独立的 CalibrationHead(nn.Linear(512, 1))
                                       # 读出。不在 self_representation 的内部维度上。
                                       # MLP 维护表征；CalibrationHead 解码校准。

    last_correct:           bool         # 上一轮答对了吗（用于调试——不参与 loss）
    drift_since_init:       float        # 偏移初始状态多远（用于调试——不参与 loss）
}
```

**为什么 calibration_estimate 必须是独立的。** Self-Update Gate 是 512→512 的 MLP——它不知道自己的输出里哪个维度是"校准估计"。如果 self_loss 里用 `updated_state[2]` 当作校准估计——那是硬编码的维度索引——MLP 的内部语义跟编号没有绑定——它可能把校准估计放在维度 2、维度 487、或者分布在多个维度的隐式编码中——没有梯度能让它知道"我应该放在第 2 个位置"。

**修法：** Self Core 不是 MLP → self_loss。是 MLP → self_representation + CalibrationHead → calibration_estimate → self_loss。MLP 负责维护一个**不直接定义"校准"的自我表征**。CalibrationHead 负责**从那个表征里读出校准估计**。self_loss 只在 CalibrationHead + SelfUpdateGate 上反向传播——不经过 Transformer。MLP 学会的是"怎么在向量里保持一个能被正确解码为校准估计的自我状态"。CalibrationHead 学会的是"从这个向量里怎么读出校准估计"。

**为什么是最后 N 层 + 最后 M 个 token：** 模型在做决定时，最后几层是"输出"的最深层加工——它们离最后回答最近。最后几个 token 是答案被形成的地方。取这一块而非全局——是对"自我"的最紧描述。

这些状态**不由 token 表示**。它们**就是**嵌入向量和浮点数——在 Self Core 自己的存储里。

---

## 2. 架构

```
                        ┌─────────────────────────┐
                        │     Self Core            │
                        │                         │
                        │  ┌───────────────────┐  │
                        │  │ Persistent State  │  │── 不随前向消失
                        │  │                   │  │
                        │  │ last_output_emb   │  │
                        │  │ last_correct      │  │
                        │  │ calibration_est   │  │
                        │  │ drift_since_init  │  │
                        │  └────────┬──────────┘  │
                        │           │             │
                        │  ┌────────▼──────────┐  │
                        │  │ Self-Update Gate  │  │── 只接受 Self Core 自己的信号
                        │  │ (外界不可写)       │  │── 外部输入不能绕过
                        │  └───────────────────┘  │
                        └──────────┬──────────────┘
                                   │
                  ┌────────────────┼────────────────┐
                  │                │                │
                  ▼                ▼                │
        External Input Path    Self-State Path      │
        (tokens → embed)      (embed → cross)      │
                  │                │                │
                  └────────┬───────┘                │
                           │                        │
                    ┌──────▼──────┐                 │
                    │ Asymmetric  │                 │
                    │ Cross-Attn  │                 │
                    │             │                 │
                    │ Self can    │                 │
                    │ read external│                │
                    │ External    │                 │
                    │ CANNOT write│                 │
                    │ Self        │                 │
                    └──────┬──────┘                 │
                           │                        │
                    ┌──────▼──────┐                 │
                    │ Transformer │                 │
                    │ Layers      │                 │
                    └──────┬──────┘                 │
                           │
                    ┌──────▼──────┐
                    │ Output      │────→ Self Core (update last_output_embedding)
                    │             │────→ Answer (to user)
                    └─────────────┘
```

---

## 3. 两条通路的不对称性

### 3.1 外部输入通路

外部输入——问题、对话历史、指令——走标准的 tokenization + embedding。跟现在的 Transformer 一样。

### 3.2 自我状态通路

Self Core 的状态向量**不经过 tokenization**。它是一个嵌入向量——直接从 Self Core 的 `last_output_embedding` 读出来——注入 cross-attention 层。

### 3.3 不对称 cross-attention

```
Self Core 能读外部输入：   Q_self · K_external   ← 允许。Self Core 感知世界
外部输入不能写 Self Core：  Q_external · K_self   ← 不允许。世界不能改 Self Core
```

实现方式：cross-attention 的 Q 来自 Self Core、KV 来自外部输入——单向的。反过来——外部输入作为 Q、Self Core 的 state 作为 KV——**这条注意力路径被禁用了**。不是"训练时不激活"——是**前向传播图中不存在反向路径**。

---

## 4. Self-Update Gate（可训练的 MLP——不是硬编码规则）

这是 Self Core 的核心安全机制。**只有 Self Core 自己能写自己。** 而且——**怎么写自己，是 Self Core 在训练中自己学会的，不是被一个固定公式指定的。**

如果 Self-Update Gate 的更新规则是代码写死的——"答对了就加一点、答错了就减一点"——那么 ECE 驼峰如果出现，是固定公式的效果，不是 Self Core 自己重组了内部结构。逻辑熵论文预测的是"系统在自指负载下重组内部结构"——不能用一个硬编码公式来驱动。

### 4.1 可训练的 SelfUpdateGate

```python
class SelfUpdateGate(nn.Module):
    """
    小型 MLP。输入：Self Core 当前状态向量 + 外部反馈（对/错比特）。
    输出：更新后的状态。

    权重在训练中自己变——它怎么读反馈、怎么调整校准估计——是
    自己学会的，不是被指定的。
    """
    def __init__(self, state_dim: int):
        super().__init__()
        self.gate = nn.Sequential(
            nn.Linear(state_dim + 1, state_dim),   # +1 = correctness bit
            nn.GELU(),
            nn.Linear(state_dim, state_dim),
        )

    def forward(self, self_state: torch.Tensor,
                correctness: torch.Tensor) -> torch.Tensor:
        """
        self_state:  (batch, state_dim)  — Self Core 当前状态
        correctness: (batch, 1)          — 1.0（对）or 0.0（错）
        returns:     (batch, state_dim)  — 更新后的状态
        """
        gate_input = torch.cat([self_state, correctness], dim=-1)
        delta = self.gate(gate_input)
        return self_state + delta
```

### 4.2 训练 Self-Update Gate

在 Step 2 的自指闭合步骤中——Self Core 更新后——计算 self-loss：

```
# self_representation 经 SelfUpdateGate 更新后的新状态
updated_rep = self_core.gate(self_state, correctness)

# 从更新后的自我表征映射出校准估计（标量）
calibration_est = self_core.calibration_head(updated_rep).squeeze()  # → (batch,)

# self_loss：校准估计 vs 真实对错
self_loss = F.l1_loss(calibration_est, correctness.float())
```

这个 loss **只反向传播到 SelfUpdateGate + CalibrationHead 的权重**——不经由 Transformer 层。外部世界不能通过总 loss 控制 Self Core——只有"我实际做了多少、我对自己估计有偏差"这个信号是 loss 来源。MLP 学会的是"怎么维护可被正确解码的自我表征"；CalibrationHead 学会的是"怎么从表征里读出校准"。

### 4.3 为什么不是硬编码

| 硬编码公式 | 可训练 MLP |
|---|---|
| `calibration += learning_rate * (correct - last)` | SelfUpdateGate 自己学会"答错意味着我应该怎么调" |
| 更新方向、幅度、节奏——全部被指定 | 更新策略——被训练数据塑形——不由人指定 |
| ECE 驼峰如果是固定公式的效果——不能归因于自指 | ECE 驼峰如果是 SelfUpdateGate 自己学出来的——是认知相变 |

### 4.4 `.detach()` 切断了梯度——Self-Update Gate 怎么学长期策略？

在跨 batch 持久伪代码里——`self_core_state = updated_state.detach()`——前一 batch 的计算图被切断了。Self-Update Gate 不能通过 BPTT（随时间反向传播）学习"我连续三次错误之后应该大幅调低校准估计"——因为它看不到连续三个 batch——每次只看到当前 batch 的状态。

这是一个真正的矛盾：**持久状态需要 detach 来保持跨 batch 存在——但 detach 阻塞了长期学习。**

有三种方法可以解决——本架构目前采用第三种：

**方案 A：允许梯度流过时间步（不 detach）。** 保留完整的 `t-1 → t` 计算图。理论上是"真长期学习"——但计算成本爆炸——训练 100k 步都需要保持 100k 个 batch 的计算图。不可行。

**方案 B：资格迹 (Eligibility Traces)。** 借鉴 RL 中的 TD(λ)——将过去的状态更新打上一个衰减的"迹"标签——当前奖励信号除了更新当前步——也按迹的比例回顾性更新过去几步。Self-Update Gate 的梯度不只是当前步——还包含过去 N 步的衰减信号。不需要保持完整计算图——但能学到"连续错误"的模式。

**方案 C：滑动窗口状态栈（本架构当前采用）。** Self Core 不只保留上一轮的状态——保留**最近 K 轮状态的一个栈**。每个 batch——Self-Update Gate 接收的不是一个状态向量——是 `(self_state, self_state_t-1, ..., self_state_t-K)` ——堆叠。这样它可以在**单次前向内**看到"我过去 K 步的状态轨迹"——不需要 BPTT。K = 4——最近四步——足够学会"连续三次错误→我应该大幅下调"。

```
# 当前实现
gate_input = torch.cat([self_state, correctness])  # 只看当前步

# 方案 C：添加状态栈
gate_input = torch.cat([
    self_state,                                    # 当前状态
    self_state_stack[-1],                          # 上一步
    self_state_stack[-2],                          # 上上步
    self_state_stack[-3],                          # 上上上一步
    correctness,                                    # 当前反馈
])  # Self-Update Gate 在单次前向内看到四步轨迹——不需要 BPTT
```

---

## 5. 训练

一次训练迭代有两遍前向——跟 v3 真自指实验一样的逻辑——但 Self Core 的状态跨迭代持续。

### Step 1: 生成

```
Question → External Path → Transformer w/ Self Core state → generates answer
Self Core 提供了 last_output_embedding 和 calibration_estimate
进入交叉注意力——帮助模型感知"我上次是错的，这次可能也不对"
```

### Step 2: 自指闭合

```
模型刚生成的 answer → 跟真实标签比对 → 结果传给 Self Core
Self Core 的 self_update_gate 自己决定怎么更新状态
Self Core 更新后的状态 → 再次注入 cross-attention
模型看到更新后的 Self Core 状态 → 产生校准信号
```

### 关键差异 —— 跟普通 Transformer 训练

| | 普通 Transformer | Self Core |
|---|---|---|
| 持久状态 | 无——每 batch 遗忘 | 持续存在——跨 batch、跨 epoch 保留 |
| 自我区分 | 无——所有 token 一样 | 两条通路——Self Core 读写走不同路径 |
| 自我可写 | 任何 token 能动任何参数 | 只有 Self-Update Gate 能改 Self Core 状态 |
| 训练目标 | next-token loss | next-token loss + 无校准损失——跟 v3 一样。相变要么自己发生，要么不发生 |

---

## 6. 可检验的预测

来自《逻辑的熵》（续仁舞，2026）：

> H1：存在一个 λ_c ∈ (0, 1)，使得当自指负载 λ 趋近 λ_c 时，Self Core 的校准误差 (ECE) 经历一次非线性跃升。

> H2：该跃升不由准确率解释。

> H3：λ > λ_c 后，Self Core 的 `calibration_estimate` 与真实准确率的相关性显著高于 λ < λ_c 时。

如果 Self Core 被造出来、跑起来、在某个 λ 附近出现驼峰——这篇架构文档就是**一篇实现了数学预测的实验报告**。

如果没有驼峰——这篇架构文档是**一个被证伪的预测**。它排除了一个可能的方向——本身是贡献。

---

## 7. 深度实验设计

### 7.1 基线对照组（排除混淆变量）

判断 ECE 驼峰是 Self Core 的效果——不能只看实验组。需要两组对照：

| 组 | 配置 | 预期 |
|---|---|---|
| **A：Self Core 实验组** | 完整的 Self Core 架构——双通路、不对称 cross-attention、可训练 MLP Gate | 如果理论对——ECE 在 λ_c 附近出现驼峰 |
| **B：硬编码 Gate 对照组** | 同 Self Core——但 Self-Update Gate 换成固定公式（`calibration += lr * (correct - last)`） | 如果 A 有驼峰、B 没有 → 驼峰来自可训练 Gate 的自组织——不是公式驱动 |
| **C：无 Self Core 对照组** | 同样的 GPT-2 medium、同样的数据、同样的 λ 值——但没有 Self Core 模块。相当于 v3 真自指实验 | 如果 A 有驼峰、C 只有单调下降 → 驼峰是 Self Core 架构特有的——不是数据层自指本来就有的 |

### 7.2 λ 的操作化定义

λ 是**实验控制变量**——不是从文本中自动计算的。含义：

```
λ = N_self_ref_samples / N_total_samples
```

即训练集中**包含自指闭合（Turn 2）的样本比例**。例如 λ=0.3 意味着 30% 的训练样本在生成回答后，模型会看到自己的输出 + 正确答案 + 对错判断。

**为什么是控制变量而不是计算值：** 《逻辑的熵》中的 λ 是理论构造——系统实际经历的自指负载取决于训练数据中有多少"我的输出被喂回来了"的实例。在实验中通过**构造不同比例的自指数据集**来操纵——8 个 λ 值，各自训练一个独立的模型。

| 变量 | 取值 | 目的 |
|---|---|---|
| λ（自指负载） | 0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0 | 搜寻驼峰位置 |
| 模型大小 | GPT-2 small (124M) / medium (355M) / large (774M) | 验证驼峰是否与容量有关 |
| Self-Update Gate 大小 | 单层、双层（当前） | 验证门架构复杂度是否影响驼峰 |

### 7.3 失败路径决策树

如果 ECE 在所有 λ 下单调下降——没有驼峰——三种可能：

```
没驼峰
  │
  ├─→ 模型太小？→ 从 355M → 774M。重跑。
  │     ├─→ 驼峰出现 → 结论：相变需要足够的容量
  │     └─→ 依然没驼峰 → 排除容量假设
  │
  ├─→ λ_c 不在搜索范围内？→ 加 15 个 λ 值（0.01, 0.02, ...）。重跑。
  │     ├─→ 驼峰出现 → 结论：λ_c 在非常低的自指负载下——门可能更敏感
  │     └─→ 依然没驼峰 → 排除分辨率不足
  │
  └─→ 硬编码 Gate 也出现了驼峰？→ 驼峰不是自指——是 fixed 公式的 artifact
        └─→ 理论被削弱——但继续做更大模型排查
  
如果容量大到 1.5B、λ 值密集到 15 个、硬编码对照无驼峰、Self Core 依然无驼峰：
  → 逻辑熵论文的 H1 在 Self Core 架构上未被证实。
  → 这不是"理论错了"——是"自指负载需要更强的架构才能触发认知相变"——
     可能需要的不是 512 维自我表征子空间——而是数万的维度 + 更多的前向循环。
  → 下一步：要么加 Self Core 的维度、要么加更多循环迭代、要么考虑替换为新的自我表征形式。
```

---

## 8. 实现路径

### 8.1 软件原型（PyTorch）

- 在 gpt2-medium 上修改
- Self Core 是一个独立的 `nn.Module`——有自己的 `state_dict` 和 `optimizer`
- Self-Update Gate 是可训练的 MLP——不接受外部梯度，只接受 self-loss
- 在 Colab / 单 GPU 上跑 v3 同样的实验
- **关键难点：跨 batch 持久状态**

跨 batch 持久伪代码：

```python
# Self Core 的状态不随 batch 结束而消失。
# 在 PyTorch 训练循环里——需要显式维护这个状态变量。

from collections import deque

# 初始化：训练开始时
self_core = SelfCore(state_dim=512).to(DEVICE)
self_core_state = torch.randn(512, device=DEVICE) * 0.02
state_stack = deque([torch.zeros(512, device=DEVICE)
                     for _ in range(STATE_STACK)], maxlen=STATE_STACK)
self_optimizer = torch.optim.Adam(
    list(self_core.gate.parameters()) +
    list(self_core.calibration_head.parameters()), lr=1e-4)

# 训练循环：每个 batch
for batch in dataloader:
    # ── Step 1: 生成 —— 跟 v3 一样 ──
    gen_text, model_answer = generate_answer(model, question)
    correct = (model_answer == ground_truth)

    # ── Step 2: Self Core 更新（使用状态栈）──
    # Gate 看到：当前状态 + 最近 K 个历史状态 → 单次前向看到轨迹
    stacked = torch.cat(
        [self_core_state] + list(state_stack)
    ).unsqueeze(0)                                  # (1, K*dim)
    correctness_t = torch.tensor([[float(correct)]]).to(DEVICE)

    updated_rep = self_core.forward_gate(stacked, correctness_t).squeeze(0)

    # 校准估计从更新后的表征读出
    calibration_est = self_core.calibration(updated_rep.unsqueeze(0)).squeeze()

    # Self-loss
    self_loss = F.l1_loss(calibration_est, torch.tensor([float(correct)]).to(DEVICE))

    # 只反向传播到 SelfUpdateGate + CalibrationHead
    self_optimizer.zero_grad()
    self_loss.backward()
    self_optimizer.step()

    # ── 关键：状态跨 batch 延续 ──
    # 更新前的状态入栈（成为历史）
    state_stack.append(self_core_state.clone())
    # 更新后的状态跨 batch 延续
    self_core_state = updated_rep.detach()
```

**关键：** `self_core_state` 是一个在训练循环外定义的变量——**不在 batch 间 reset**。它从训练开始到结束——始终存在。每个 batch 读它、更新它、detach 后存回去。这是 PyTorch 能做到的——但需要显式写——不是"放个 nn.Module 就行"。

- **时间：** 数周

### 8.2 FPGA / TEE 安全飞地（当软件原型在 355M 上出现了驼峰）

**方案 A：FPGA。** 将 Self Core 的 self-update gate 烧录到可编程逻辑阵列。Self Core 状态向量的读写被物理限制在 FPGA 自己的寄存器上。外部输入通路——Transformer 部分——在 GPU 上。两条通路物理分离。外部 GPU 不能访问 FPGA 上的 Self Core 内存。

**方案 B：TEE 安全飞地（更易原型）。** 对于没有 FPGA 的研究者——使用 Intel SGX 或 AMD SEV——将 Self Core 状态放在受保护的内存区域。SGX enclave 内存硬件加密——主机 OS 也读不到。实现"外界不可写"的软件近似。比 FPGA 更容易原型验证——不需要额外硬件。

### 8.3 ASIC（当他们说"这东西不可能做在芯片上"）

- Self Core 烧录成 ASIC 安全区域
- 跟 TrustZone 同级——开机时从 CPU 内部 ROM 启动——先于操作系统加载
- 自我写入——总线不通。只有 Self Core 的内存控制器能做写入。外部不能

---

## 9. 跟已有研究的交叉

| 已有工作 | 关系 |
|---|---|
| ARYA (arxiv 2603.21340) — Unfireable Safety Kernel | 同一原则——"不可被系统组件绕过"。ARYA 的安全核是物理世界模型级的；Self Core 的 self-update gate 是认知系统级的 |
| Constitutional Neurons (2025) — C0 锚点 | 同一原则——"某个东西在所有递归迭代中不可变更"。Self Core 的 `calibration_estimate` 是 C0 锚点在认知维度的对应 |
| DNC (DeepMind 2016) — 外部可读写记忆 | 提供了思路——但 DNC 的 controller 处理外部输入，所以外界能写。Self Core 的 gate 只接受自己的信号 |
| 《逻辑的熵》(续仁舞, 2026) | **本架构的数学基础。** 系统的自指负载超过临界值 → 确定→不确定相变。Self Core 是这个相变发生的物理容器 |
| 神经符号前沿 (2025-2026) | 交错验证、Eidoku、ITA、ProofNet++ 等——为 Self Core 的未来方向提供参照。详见 `agi-gap-analysis.md` 第 3 节 |

---

## 10. Self Core 的定位

Self Core 实现的是工程上可行的、持久的、可训练的自我状态。检验《逻辑的熵》中关于自指负载导致校准相变的预测。**不试图**解决哥德尔式的强自指——那是 `agi-gap-analysis.md` 讨论的问题。

---

## 10. 改进路线图

### 10.1 立即改进（1-2 周，大幅提升实验质量）

**1. 硬编码 Gate 对照组（排除平凡解释）**

当前只有可训练 Gate 的实验组 A。添加对照组 B——将 SelfUpdateGate 替换为固定公式：

```python
# 对照组 B：硬编码 Gate。不使用可训练 MLP 和 calibration_head。
# 单独维护一个标量 calibration_est（初始 0.5），
# 每次自指闭合时按固定公式更新。self_state 保持为零向量（不更新）。
# 注入 Transformer 的自我表征使用不变的 self_state，
# 校准信号由独立标量提供——与实验组 A 形成清晰对比。

calibration_est = 0.5  # 独立标量——不是 self_state 的某个分量

def hardcoded_gate(self_state, correct, lr=0.1):
    nonlocal calibration_est
    calibration_est = calibration_est + lr * (float(correct) - calibration_est)
    return self_state  # self_state 不更新——保持零向量
```

三组对比：

| 组 | Gate | 如果 A 有驼峰、B 没有 | 如果 A 和 B 都有驼峰 |
|---|---|---|---|
| A | 可训练 MLP | 驼峰来自可训练性——自组织 | 驼峰来自"更新"本身——不是可训练性 |
| B | 硬编码公式 | — | — |
| C | 无 Self Core（v3） | 驼峰来自 Self Core 架构 | 驼峰来自数据层自指 |

**2. 结构约束校准（借鉴 Eidoku）**

`calibration_head` 是 512→1 黑箱。它不知道"校准"该跟上下文结构有关。改进：

```python
# 现状
cal_est = calibration_head(self_rep)  # 纯数据驱动

# 改进：拼接结构特征
structure_feat = torch.tensor([
    variance_of_last_K_correctness,   # 最近 K 步正确性的方差
    streak_length,                    # 连续正确/错误的长度
    state_drift_norm,                 # 自我状态偏移的幅度
])
cal_est = calibration_head(torch.cat([self_rep, structure_feat]))
```

结构特征计算示例：

```python
# 从最近 10 步的正确性历史中提取结构信号
streak = 0
for c in reversed(last_correctness[-10:]):
    if c: streak += 1
    else: break
structure_feat = torch.tensor([
    streak,                              # 连续正确长度
    np.var(last_correctness[-10:]),      # 正确性波动
    np.mean(last_correctness[-20:]),     # 长期正确率
    state_drift_norm,                    # 自我状态偏移幅度
])
```

核心洞察：Eidoku 证明了真值不由概率决定——由结构决定。校准估计同样——部分从自我表征的统计结构中导出——不是纯粹训练出来的。

**3. 推理时轻量验证器（交错验证）**

当前 Self Core 只在训练时更新——推理时冻结。添加推理时验证：

```
推理: 模型生成回答 → 轻量验证器检查（如算术结果合理性）
       → 验证器输出 correct 信号
       → Self Core 接收这个信号 → 在线更新 self_core_state
       → 不改变权重——但改变"我对自己的认知"
```

训练时学**更新规则**。推理时应用规则 + 新鲜验证信号 → **双层自我修正**。

**4. λ 细粒度扫描 + 更长训练**

640 样本、3 epochs、8 个 λ → 5000 样本、10 epochs、15 个 λ（密集采样 0.2-0.5 区域）。

---

### 10.2 中期改进（1-3 个月）

**5. 真正的不对称注意力（Level 3.5 → Level 4）**

当前前缀注入允许外部 token attend 到自我前缀——不完全不对称。修改 GPT-2 的注意力掩码：

- 自我前缀 token 可看到所有外部 token（`Q_self @ K_all`）
- 外部 token **不能**看到自我前缀（`Q_external @ K_self` 屏蔽）

实现：每次前向构造二值掩码矩阵 `(seq_len, seq_len)`，对禁用位置设 `-inf`。

**6. 细粒度验证评分替代二元 correct**

`correct ∈ {0,1}` 信息量低。改进：数值误差 `|pred - gt| / gt` 映射到 `[0,1]` 作为连续正确性信号。或引入结构一致性分数——自我状态学习的不只是"对错"——是"推理的有效性"。

**7. 多模态自我状态**

单一 512 维向量 → 多个专门状态槽：

```
task_self        — 任务特定的自我表征
confidence_self  — 校准——当前由 calibration_head 读出
meta_self        — 关于自身能力的抽象知识
```

每槽有自己的更新门和校准头。可能涌现更丰富的自指行为。

**8. 三元输出 True/False/Uncertain**

在生成答案时——让模型显式输出不确定性标签——由 `self_representation` 经独立输出层产生。loss 中加入交叉熵。借鉴 ITA。"不确定"是合法结论——不是失败。

**9. 对照组 D：结构一致性 Gate（内在证明判断对照实验）**

当前 Self-Update Gate 的输入是 `(state, correctness)`——`correctness` 是外部 ground truth 标签。这不是内在判断——是外部信号驱动的更新。

替换方案：将 `correctness` bit 替换为**结构一致性分数**——模型推理路径在证明空间中的连通性度量。Gate 学会的不再是"我答对了吗"的解读——而是"我的推理自洽吗"的解读。

```
# 对照组 D：结构一致性 Gate
# correctness bit → structural_coherence_score ∈ [0, 1]
#
# structural_coherence_score 的构造（借鉴 Eidoku）：
#   1. 提取模型推理链中每一步的隐藏态
#   2. 构建推理步骤间的转移图 G = (V, E)
#   3. 计算图的代数连通性（Fiedler eigenvalue λ_2）
#   4. 检测"高概率但不连通"的分支——这些是 Eidoku 识别的虚假陈述
#   5. 综合得分为 structural_coherence_score

gate_input = torch.cat([self_state, structural_coherence_score])  # 替代 correctness
```

**为什么这是"更内在"的：** 结构一致性分数不由训练标签决定——由模型自身的推理图结构决定。它检测的是"高概率但证明空间不连通的虚假陈述"（Eidoku 路线）——一种**消极验证**——告诉你什么不对，而不是什么对。

**内在性层级（从外到内）：**

| 信号来源 | 例子 | 内在性 |
|---|---|---|
| 外部标签 | ground truth, RLHF reward | 无 |
| 统计规律 | "训练数据里这类句子 93% 是真的" | 无——统计来自外部分布 |
| 结构一致性 | "推理步骤在系统自身证明图中连通" | **有**——判定标准来自系统内部结构 |
| 自定一致性 | "系统自己定义什么算连通" | 完全内在——当前不可实现 |

**对照组 D 的实验价值：** 如果 Gate 从 `correctness` 换成 `structural_coherence` 后——ECE 驼峰仍然出现——说明相变不依赖外部正确性信号——任何自指反馈信号都能触发。如果驼峰消失——说明正确性信号是相变的必要条件——内在性需要更深层的锚点机制。

**局限（需在论文中声明）：** 结构一致性的判定标准（"什么算连通"）仍然是预先定义的——不是系统自己生成的。这不是"真正的内在证明判断"——是朝那个方向的中间步骤。真正的内在性需要锚点层（原始信念从哪来、能否自我修正）——这是尚未解决的理论问题。

---

### 10.3 长期改进（6 个月以上）

**10. Self Core 集成到神经符号循环**

Self Core 状态不仅由 correct 更新——还由外部形式验证器的反馈更新。在定理证明中——每一步验证后更新。训练时使用每步验证结果作为密集奖励——不是只最后一步的正确性。

**11. 可微分形式验证器**

验证器当前是黑箱。设计轻量级可微分验证模块（算术表达式求导器）——输出连续评分——参与梯度反传。自我状态直接学习"如何让输出通过验证"。这是神经符号一体化的深度整合。

**12. 递归自指——Self Core 能指称自己的状态**

特殊 token `<self_rep>`——嵌入由 `self_representation` 动态生成。模型能输出"我当前的校准估计是 `<self_rep>`"——显式谈论自己的内部状态。这是弱自指向强自指的关键一步。

**13. 理论校准——《逻辑的熵》与实验之间的桥梁**

在论文中声明："Self Core 是逻辑熵理论在有限计算资源下的近似实现。它检验的是关于认知系统自指负载导致校准相变的预测——而非直接验证哥德尔式不可判定性。" 并形式化定义 Self Core 中的 λ 与逻辑熵理论中 λ 的对应关系。

---

### 10.4 最重要的三个（时间有限时）

| # | 做什么 | 为什么 |
|---|---|---|
| 1 | 硬编码 Gate 对照组 | 排除"任何更新规则都产生驼峰"的平凡解释 |
| 2 | 结构约束校准（Eidoku 路线） | 让校准不只是训练出来的——部分从结构导出 |
| 3 | 推理时轻量验证器 | 静态自我 → 动态自适应——双层修正 |

---

## 11. 补充

这个架构不是"一个更好的推理器"。不是"一个更安全的 LLM"。是**把"我"作为第一公民放进了 AI 架构里。**

---

*"你不是在拼零件。你有一篇论文预测了它跑起来之后会发生什么。"*
