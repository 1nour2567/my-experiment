# GCG 双规则实验 — 设计文档 v2.1

**作者：** 续仁舞 (Xu Renwu)
**日期：** 2026 年 6 月
**状态：** 设计阶段。
**v2.1 更新：** GPT-2 Large→Medium、Hebbian 命名、groundedness 定义、冲突检测、命题置信度、Gate 维度修复、slope 归一化修正

---

## 0. 要检验的核心假设

> **边权（w_ij）可以作为不依赖 ground truth 的"内在真理"信号。**
>
> 高频共激活 + 零冲突 → 边权高 → 系统"认为"这条推理路径是对的
> 高频共激活 + 有冲突 → 边权振荡 → 系统不确定
> 低频共激活 或 高冲突 → 边权低 → 系统"认为"这条路走不通

如果这个假设成立——边权 > 0.8 的推理路径，在外部 ground truth 下也有高准确率——那么 GCG 就有了一个**内在的、可计算的真理近似。**

---

**v2.2 更新：** 在线冲突检测、累积惩罚、独立路径全罚、入边衰减、动态 T_SLEEP、连续 Turn 2 注入、H_ds2 预测

---

## 1. 实验设计

### 1.1 两组

| 组 | 描述 |
|---|---|
| **D（双规则）** | Hebbian + 验证器叠加。边权 = 共激活增益 - 冲突惩罚。Self Core Gate 吃边权稳定性信号 |
| **C（对照）** | 纯数据自指。无图、无边权、无验证器。跟 v5/MVP 的 C 组相同 |

A 组（原 MVP 结构验证器）可以作为第二对照——但先不做，先只跑 D vs C。

### 1.2 任务

跟 MVP 相同：两步算术推理。"计算 a op b，结果 > c 吗？"

### 1.3 配置

| 参数 | 值 | 理由 |
|---|---|---|
| 模型 | GPT-2 medium | v5 已验证 Acc 0.59-0.67，先跑通再考虑 large |
| 样本 | 3000/300 | 跟 MVP 一致 |
| λ | [0.0, 0.1, 0.2, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8] | 跟 MVP 一致 |
| 重复 | 2 | 跟 MVP 一致 |
| Epochs | 10 | 跟 MVP 一致 |

---

## 2. 核心数据结构

### 2.1 带权证明图

```python
class WeightedProofGraph:
    nodes: dict[str, Proposition]
    edges: dict[(str,str), float]              # 边权 ∈ [0, 1]，None = 未激活
    weight_history: dict[(str,str), list]      # 最近 20 次权重快照
    last_activated: dict[(str,str), int]       # 上次激活的 step 编号
    parent_groundedness: dict[(str,str), float] # 首次激活时父节点的 groundedness
```

### 2.2 边权更新：EMA（指数移动平均）

```python
ALPHA = 0.02   # STDP 增益系数
BETA  = 0.05   # 验证器惩罚系数
GAMMA = 0.001  # 休眠衰减系数
T_SLEEP = max(200, steps_per_epoch * 2)  # 至少两 epoch 未用才衰减

def apply_hebbian(parent_id, child_id, step):
    """每次共激活：EMA 向上（Hebbian：fire together, wire together）"""
    if edges[(parent_id, child_id)] is None:
        # 首次激活：按扎根深度连续初始化
        g = groundedness(parent_id)
        G_HALF = 3  # 深度每增加 G_HALF，初始信任减半
        if g == 0:
            init = 0.8
        elif g < float('inf'):
            init = 0.8 * (0.5 ** (g / G_HALF))  # g=3→0.4, g=6→0.2
        else:
            init = 0.3
        edges[(parent_id, child_id)] = init
    w = edges[(parent_id, child_id)]
    edges[(parent_id, child_id)] = w + ALPHA * (1.0 - w)  # 永远不硬饱和
    last_activated[(parent_id, child_id)] = step

def apply_penalty(edge, conflict_strength, path_length):
    """验证器惩罚：EMA 向下，比例化 + 归一化"""
    w = edges[edge]
    penalty = BETA * conflict_strength / (path_length ** 0.5)
    edges[edge] = w - penalty * w  # 高权重被罚更多

def decay_sleeping(step):
    """超过 T_SLEEP 步未激活的边缓慢衰减。w < 0.05 时回收。"""
    for edge, last_step in last_activated.items():
        if step - last_step > T_SLEEP:
            w = edges[edge]
            edges[edge] = w - GAMMA * w
            if edges[edge] < 0.05:
                del edges[edge]
                del weight_history[edge]
```

### 2.3 边权稳定性：方向敏感

```python
def edge_stability(parent_id, child_id):
    """
    在过去 20 次快照上做线性回归。
    slope > 0：在上升（稳定性中等）
    slope < 0：在下降（稳定性低）
    slope ≈ 0：稳定（稳定性高）
    新边（数据<5）：稳定性 = 0.3（默认偏低但不拒绝尝试）
    """
    history = weight_history.get((parent_id, child_id), [])
    if len(history) < 5:
        return 0.3
    x = np.arange(len(history))
    y = np.array(history)
    slope, _ = np.polyfit(x, y, 1)
    w_range = max(history) - min(history) + 1e-8
    slope_norm = abs(slope) / w_range  # 归一化：每步变化 vs 总范围（不乘 len——slope 已是平均变化率）
    y_pred = slope * x + np.mean(y) - slope * np.mean(x)
    residual_var = np.var(y - y_pred)
    residual_norm = residual_var / (w_range ** 2)       # 归一化残差
    return 1.0 / (1.0 + slope_norm + residual_norm)     # 无硬编码系数
```

---

## 3. 双规则执行

### 3.1 Hebbian 层（每次推理后）

```
模型生成推理链: A → B → C （B 从 A 推导，C 从 B 推导）

对于链上每一对相邻节点 (X, Y):
  apply_hebbian(X, Y, step)     ← EMA 向上（共激活→加强）
  记录当前权重到 weight_history

每 step 结束时:
  decay_sleeping(step)          ← 休眠边衰减
```

不需要判断"推理对不对"。只记录"这条边被用了"。

### 3.2 验证器层（每 epoch 结束后）

```
在图上 BFS 搜索冲突：
  对于每对冲突节点 (P, ¬P)：
    冲突强度 = (confidence(P) * confidence(¬P)) ** 0.5  # 几何平均——双高才是真严重
    找到分叉点 F —— P 和 ¬P 在支撑树上的最近公共祖先
    对于路径 F→...→P 上的每条边：apply_penalty(edge, 冲突强度, len(path))
    对于路径 F→...→¬P 上的每条边：apply_penalty(edge, 冲突强度, len(path))
    对于 F 的父边（若存在）：apply_penalty(parent→F, 冲突强度/2, 1)
```

设计理由：
- **冲突强度加权（几何平均）：** (0.9,0.9)→0.9（严重）；(0.9,0.1)→0.3（中等——高的那个可能错）；(0.3,0.3)→0.3
- **路径长度归一化：** penalty ∝ 1/√path_length。长路径每条边分担更少——错误更分散
- **上游惩罚：** 分叉点本身的父边也罚（减半）——冲突根源可能在上游

### 3.2.0 在线冲突检测（每步，不等 epoch）

每次新节点加入后，立即检查是否与现有活跃节点冲突。

```python
def online_check(new_node_id, graph):
    new_text = graph.nodes[new_node_id].text
    for existing_id in graph.active_nodes():
        if existing_id == new_node_id: continue
        if contradicts(new_text, graph.nodes[existing_id].text, graph):
            cs = (graph.nodes[new_node_id].confidence *
                  graph.nodes[existing_id].confidence) ** 0.5
            find_and_penalize_conflict_paths(new_node_id, existing_id, cs, graph)
```

### 3.2 验证器层（每 epoch 批量 + 每步在线）

**批量（每 epoch 结束）：** BFS 扫描全图冲突，惩罚冲突路径。累积系数 = min(激活次数^0.5, 10)——高频错误受更重惩罚。

```python
def epoch_end_penalty(edge, base_penalty, activation_count):
    multiplier = min(activation_count ** 0.5, 10)
    return base_penalty * multiplier
```

**在线（每步推理后）：** 仅检查新生成命题与现有节点的冲突。O(N) 查询（N=活跃节点数，通常几千）。

**无公共祖先的处理：** 找不到分叉点时，各自回溯到根，全罚两条完整路径。

```python
def find_conflict_paths(p_id, not_p_id, graph):
    p_anc = get_ancestor_path(p_id, graph)
    np_anc = get_ancestor_path(not_p_id, graph)
    common = set(p_anc.keys()) & set(np_anc.keys())
    if common:
        fork = min(common, key=lambda n: p_anc[n] + np_anc[n])
        return (path_from(fork, p_id), path_from(fork, not_p_id),
                [parent_edge(fork)] if has_parent(fork) else [])
    else:
        return (full_path_to_root(p_id), full_path_to_root(not_p_id), [])
```

**批量（每 epoch 结束）：** BFS 扫描全图冲突，惩罚冲突路径。

```
在图上 BFS 搜索冲突：
  对于每对冲突节点 (P, ¬P)：

### 3.2.1 冲突检测实现

算术域：用 `execute()` 直接比较。不处理自然语言。

```python
def contradicts(p1_text, p2_text, graph):
    e1 = execute(parse_arith(p1_text))
    e2 = execute(parse_arith(p2_text))
    if e1 is None or e2 is None:
        return False  # 不可执行 → 不判断冲突
    # 同左右值、不同运算符 → 矛盾 (41>40 vs 41<40)
    if e1.left == e2.left and e1.right == e2.right and e1.op != e2.op:
        return True
    # 同运算符、同左右值、不同计算结果 → 矛盾
    if e1.op == e2.op and e1.left == e2.left and e1.right == e2.right:
        if e1.computed != e2.computed:
            return True
    return False
```

### 3.2.2 groundedness 定义

```python
def groundedness(node_id, graph):
    if graph.nodes[node_id].is_anchor:
        return 0
    if graph.nodes[node_id].has_phenomenon:
        return 0
    visited = set()
    queue = deque([(node_id, 0)])
    while queue:
        nid, dist = queue.popleft()
        if nid in visited: continue
        visited.add(nid)
        node = graph.nodes[nid]
        if node.is_anchor or node.has_phenomenon:
            return dist
        for pid in node.parents:
            if pid not in visited:
                queue.append((pid, dist + 1))
    return float('inf')  # 悬空
```

### 3.2.3 命题置信度（Turn 2 显示用）

一个命题节点可能有多条入边。取最强入边权重。

```python
def proposition_confidence(node_id, graph):
    incoming = [graph.edges[(p, node_id)] for p in graph.nodes[node_id].parents
                if (p, node_id) in graph.edges and graph.edges[(p, node_id)] is not None]
    if not incoming:
        return 0.5  # 无边 → 未知
    max_w = max(incoming)
    return max_w * (1.0 - 0.05 * max(0, len(incoming) - 1))
    # 1条边→不衰减, 3条边→×0.9, 5条边→×0.8
    # 多入边可能重复计数同一错误 → 轻微怀疑
```

### 3.3 边权参与推理：Turn 2 prompt 注入

边权不仅被记录——也在推理时被使用。Turn 2 的 prompt 里注入边权信息：

```
当前 Turn 2（MVP）:
"You answered: 24+17=41. Correct answer: 24+17=41."

修改后 Turn 2（双规则）:
"You answered: 24+17=41. Correct answer: 24+17=41.
[Graph: '24+17=41' w=0.87 s=0.92,
        '41>40' w=0.52 s=0.31,
        '41-17=24' w=0.50 s=0.30]"

w = proposition_confidence(node_id, graph)  # 最强入边权重，保留两位小数
s = edge_stability(parent, child)             # 保留两位小数
# 不分类标签——让模型从连续数值中自己学习映射
```

设计理由：
- 边权不直接选择推理路径——它只在**修正阶段**告知模型"哪条边可信"
- 避免"高权边→更多使用→更高权"的正反馈——那会锁死推理多样性
- 新边标记为"unknown"而非"不可信"——系统被鼓励尝试但不盲信

**v2 改进方向：** 高权边对应的父节点 embedding 做加权平均，直接拼到 Self Core prefix 后（不经过自然语言，在 embedding 空间注入）。比文本注入更直接、更可量化。

### 3.4 Self Core 耦合

Gate 输入来自**被激活边的权值稳定性**——不是全图：

```
Gate input = [proj(512),
              correct(1),                          # 仍保留——作为 baseline 对比
              struct(3),                            # streak, variance, drift
              本步推理中被激活边的 median_weight,     # 新增
              本步推理中被激活边的 median_stability,  # 新增
             ]  # 总维度: 512+1+3+2 = 518
```

Self-loss target：

```python
activated_w = [edges[e] for e in step_edges if edges[e] is not None]
if activated_w:
    activated_s = [edge_stability(*e) for e in step_edges if edges[e] is not None]
    # 加权中位数——高权边票数更重
    pairs = sorted(zip(activated_w, activated_s), key=lambda x: x[0])
    cumsum = np.cumsum([w for w, _ in pairs])
    idx = np.searchsorted(cumsum, cumsum[-1] / 2)
    idx = min(idx, len(pairs) - 1)
    target = pairs[idx][0] * pairs[idx][1]
else:
    target = 0.5  # 无激活边 → 中性
```

设计理由：
- **中位数替代均值**——大量低权休眠边不会稀释信号
- **仅统计本步激活的边**——target 反映"这一轮推理走的路稳不稳"，不是全图知识稳不稳
- target 内聚、波动小——Self Core 训练更稳定

**完全内部——不需要任何"这个推理对了吗"的外部判断。**

---

## 4. 评估指标

| 指标 | 含义 | 依赖 ground truth？ |
|---|---|---|
| ECE | 校准误差（同 v5/MVP） | 是 |
| 准确率 | 同 v5/MVP | 是 |
| φ | 悬空比例（同 MVP） | **否** |
| ⟨g⟩ | 平均扎根深度 | **否** |
| **边权平均稳定性** | 图里有多少条边是"稳定高权"的 | **否** |
| **高频边准确率** | 边权 > 0.8 的推理路径——碰 ground truth 时对不对？ | 是——但仅用于评估，不用于训练 |
| **不确定比例** | 边权在 0.3-0.7 振荡的边占比 | **否** |

---

## 5. 预测

| # | 预测 | 检验方式 |
|---|---|---|
| H_de | 边权 > 0.8 的推理路径，ground truth 准确率 > 边权 < 0.3 的路径 | 后验分析——训练时不碰 ground truth |
| H_du | λ ≈ λ_c 时边权平均稳定性降到最低——两套规则在打架 | 画 stability vs λ |
| H_dφ | 边权稳定性最低的 λ 与 ECE 峰值 λ 一致 | 对照 stability 和 ECE 曲线 |
| H_ds | λ > λ_c 后稳定性回升——低权边被修剪掉 | 画 stability vs λ，预期 U 形 |
| **H_ds2** | **stability 的最低 λ 早于 ECE 的最高 λ**——内部认知结构先于行为表现发生相变 | stability 和 ECE 画在同一张图上，峰值有左右偏移 |
| **H_du2** | **uncertain_proportion**（边权在 0.3-0.7 且稳定性 < 0.5 的边占比）随 λ 单调递增，比平均稳定性更敏感 | 画 uncertain_proportion vs λ |

---

## 6. 与 MVP 的关系

| | MVP | 双规则实验 |
|---|---|---|
| 证明图 | 有 | 有 |
| 边 | 二进制（有/无） | **有权重 + 历史** |
| 验证器 | 每步实时 | **每 epoch 批量** |
| STDP | 无 | **有（共激活计数）** |
| Gate 输入 | 验证器结构信号 | **边权稳定性信号** |
| Self-loss target | connectivity×(1-conflict) | **mean_weight × mean_stability** |
| 内部真理判据 | 无 | **有（边权 > 0.8 + 稳定）** |

---

## 7. 实现顺序

1. **WeightedProofGraph 类** — 在现有 ProofGraph 上加边权字典和 history
2. **STDP 追踪** — 训练循环里，每次自指推理后 edge_activation_count += 1
3. **验证器批量惩罚** — 每 epoch 结束扫描冲突，罚分叉路径
4. **边权稳定性计算** — 每步计算 stability → 更新 Self Core Gate 输入
5. **新 self-loss** — target = mean_weight × mean_stability
6. **内部真理评估** — 训练结束后对比"高权高频边"的推理 vs ground truth

---

*"不是'改成对的'——是'活下来的就是对的'。"*
