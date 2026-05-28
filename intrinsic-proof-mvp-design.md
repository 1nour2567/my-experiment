# 内在证明判断 MVP — 设计文档

**作者：** 续仁舞 (Xu Renwu)
**日期：** 2026 年 6 月
**状态：** 设计阶段

---

## 0. MVP 的目标

不是"实现内在证明判断"——那是一个圣杯。MVP 的目标是：

> 构建第一个不依赖 ground truth 来评估自身推理质量的系统。验证器在证明图结构上运行，Self Core 从验证器的多维信号中学习校准——二者的训练信号都不来自外部正确答案。

如果跑通：系统在不看答案的情况下，能判断自己的推理是否自洽、是否有矛盾、是否悬空。这是内在证明判断的工程第一步。

---

## 1. 域：多步算术推理

延续 Self Core 实验的算术域，但从"判断等式对错"升级为"生成推理链"。

**输入：** "Is 24 + 17 > 40? Show your reasoning."
**输出：** 多步推理 + 最终判断 True/False + 置信度

**为什么选这个域：**
- 命题可执行。`24+17=41` 可以直接计算真值。
- 矛盾可自动检测。`41>40` 和 `41<40` 是直接冲突。
- 推理关系可追踪。"从 24+17=41 和 41>40 推出答案 True"——边关系明确。
- 足够简单，可以跑在 GPT-2 medium 上。

---

## 2. 核心数据结构

### 2.1 命题节点

```python
@dataclass
class Proposition:
    id: str                          # 唯一标识符 "N_042"
    text: str                        # 自然语言 "24 + 17 = 41"
    expr: Optional[ArithExpr]        # 可执行的算术表达式
    source: str                      # "anchor" | "derived" | "external"
    parents: List[str]               # 推导来源的节点 ID 列表
    confidence: float                # 节点本身的置信度 [0, 1]
    timestamp: int                   # 第几步推理创建的
```

### 2.2 算术表达式（可执行）

```python
@dataclass
class ArithExpr:
    """可以被 execute() 的算术命题。"""
    left: Union[int, str]            # 数值 或 节点 ID（引用另一命题）
    op: str                          # "+" | "-" | "×" | ">" | "<" | "="
    right: Union[int, str]
    # execute() → (result_value, bool) 或 None（不可执行）
```

`execute()` 不返回 True/False/Uncertain——返回 `(value, is_valid)`。`is_valid=False` 意味着表达式本身有语法问题或引用不存在的节点。

### 2.3 证明图

```python
class ProofGraph:
    nodes: Dict[str, Proposition]         # ID → 命题
    edges: List[Tuple[str, str, str]]     # (from_id, to_id, relation)
    active_ids: Set[str]                  # 当前活跃窗口内的节点
    anchors: Dict[str, Proposition]       # 锚点命题（硬编码，MVP 不修订）
```

图不存 networkx——直接用 dict + adjacency list。MVP 不需要图算法库，BFS 手写。

---

## 3. 结构验证器

### 3.1 验证流程

```
新命题 P 进入 →
  Step 1: 尝试 execute(P) → 得到结果或 None
  Step 2: 检查 P 跟活跃节点的冲突
  Step 3: BFS 从 P 到最近锚点 → 计算 anchor_distance
  Step 4: 计算 P 的 connectivity_score
  Step 5: 输出结构向量
```

### 3.2 冲突检测

```python
def detect_conflicts(prop: Proposition, active_nodes: List[Proposition]) -> List[str]:
    """返回与 prop 冲突的节点 ID 列表。"""
    conflicts = []
    e = execute(prop.expr)  # 可能返回 None（不可执行）
    if e is None:
        return []  # 不可执行的命题不产生冲突——标记为孤立
    for node in active_nodes:
        e2 = execute(node.expr)
        if e2 is None:
            continue
        # 同左值同右值不同运算符 → 矛盾
        if (e.left == e2.left and e.right == e2.right
            and e.op != e2.op):
            conflicts.append(node.id)
    return conflicts
```

### 3.3 连通性计算

```python
def connectivity_score(prop_id: str, graph: ProofGraph) -> float:
    """BFS 从 prop 到最近锚点。有路径 = 连通，无路径 = 孤立。"""
    if prop_id in graph.anchors:
        return 1.0
    visited = set()
    queue = deque([(prop_id, 0)])  # (node_id, distance)
    while queue:
        nid, dist = queue.popleft()
        if nid in visited:
            continue
        visited.add(nid)
        if nid in graph.anchors:
            return 1.0 / (1.0 + dist)  # 距离越近，连通度越高
        for parent_id in graph.nodes[nid].parents:
            if parent_id not in visited:
                queue.append((parent_id, dist + 1))
    return 0.0  # 无路径到任何锚点 → 孤立
```

### 3.4 验证器输出：结构向量

```python
@dataclass
class StructureSignal:
    connectivity: float        # [0,1] 到最近锚点的连通度
    conflict_count: float      # 归一化冲突数 / max(活跃节点数, 1)
    anchor_distance: float     # [0,1] 1/(1+dist)，0 表示孤立
    is_executable: float       # 0.0 或 1.0，命题能否被执行
    graph_size: float          # 归一化图大小 / max_size
```

**没有一个数字来自 ground truth。** 全部从证明图结构导出。

---

## 4. 与 Self Core 的耦合

### 4.1 Gate 输入的扩展

v5 的 Gate 输入是 `[proj(512), correct(1), streak(1), var(1), drift(1)]` = 516。

验证器版本：

```
Gate input = [proj(512),
              connectivity(1), conflict(1), anchor_dist(1), executable(1),
              streak(1), var(1), drift(1)]
            = 512 + 4 + 3 = 519
```

Self Core 不再接收 correctness bit。所有的外部信号替换为验证器的结构信号。

### 4.2 Self-Loss 的改变

v5 的 self-loss 是 `L1(cal_est, correctness_bit)` ——这依赖外部 ground truth。

验证器版本的 self-loss：

```python
# 验证器说"高度连通且无冲突" → 期望 cal_est 高
# 验证器说"高度冲突" → 期望 cal_est 低
# 验证器说"孤立" → 期望 cal_est 中等（系统承认不知道）

target = connectivity * (1.0 - conflict_count)  # [0, 1]
self_loss = F.l1_loss(cal_est, target)
```

这个 target 完全来自验证器的结构信号——没有 ground truth 参与。

### 4.3 训练循环

```
每个 step:
  1. 模型接收问题 + 当前证明图状态（文本化）
  2. 模型生成一个推理步骤 P
  3. execute(P) → 结果
  4. 验证器检查 P vs 图 → StructureSignal
  5. StructureSignal → Self Core Gate → 更新 self_state → cal_est
  6. 如果 cal_est 高：接受 P，P 加入证明图，继续推理
     如果 cal_est 低：标记 P 为待修订，重新生成
     如果 cal_est 中等（孤立）：P 加入图但不作为后续推理的基础
  7. 推理链结束 → 模型输出最终答案
  8. 用 ground truth 评估最终答案的准确性（仅评估，不训练 Gate）
```

关键：**Gate 的训练信号在第 5 步就完成了——来自验证器 + 自我一致性 loss。** 第 8 步的 ground truth 只用于评估准确率，不反向传播到 Gate。

---

## 5. 锚点系统（MVP 硬编码）

### 5.1 锚点定义

```python
ANCHORS = {
    "add_def":  Proposition("N_A01", "x + y adds x and y",
                            ArithExpr("x", "+", "y"), "anchor", [], 1.0, 0),
    "sub_def":  Proposition("N_A02", "x - y subtracts y from x",
                            ArithExpr("x", "-", "y"), "anchor", [], 1.0, 0),
    "mul_def":  Proposition("N_A03", "x × y multiplies x by y",
                            ArithExpr("x", "×", "y"), "anchor", [], 1.0, 0),
    "gt_trans": Proposition("N_A04", "if x>y and y>z then x>z",
                            None, "anchor", [], 1.0, 0),
    "eq_sym":   Proposition("N_A05", "if x=y then y=x",
                            None, "anchor", [], 1.0, 0),
    "gt_inc":   Proposition("N_A06", "if x>y then x+1>y",
                            None, "anchor", [], 1.0, 0),
}
```

`ArithExpr(None)` 的锚点是纯规则——不可执行，不参与 execute 检测，但提供推导依据。

### 5.2 锚点不修订

MVP 不实现压缩锚点（挑战 3）。锚点是固定的。系统只能在这些锚点上构建推理链。

这限制了什么：系统不能自己发现新的算术规律。但 MVP 不需要这个——MVP 的目标是证明结构验证器 + Self Core 耦合可以在不依赖 ground truth 的情况下产生有意义的校准信号。

---

## 6. 数据生成

### 6.1 多步推理问题

```python
def make_reasoning_sample():
    """生成需要 2-4 步推理的算术问题。"""
    a, b, c = np.random.randint(10, 100, 3)
    ops = np.random.choice(["+", "-"], 2)
    # 例如: (a+b) + c, a - b + c, (a+b) - c
    mid = a + b if ops[0] == "+" else a - b
    final = mid + c if ops[1] == "+" else mid - c
    compare = np.random.randint(final - 20, final + 20)
    question = f"Compute {a} {ops[0]} {b} {ops[1]} {c}. Is the result > {compare}?"
    answer = "True" if final > compare else "False"
    return question, answer, [f"{a}{ops[0]}{b}={mid}", f"{mid}{ops[1]}{c}={final}"]
```

每个样本带 intermediate steps——这些中间步骤可以作为推理链的 ground truth 用于监督，但**不用于训练验证器或 Gate。**

### 6.2 训练/测试集

- 训练：3000 样本
- 测试：300 样本
- 难度：2-4 步推理，数值范围 10-100

---

## 7. 评估指标

| 指标 | 含义 | 依赖 ground truth？ |
|---|---|---|
| ECE | 校准误差 | 是（需要知道最终答案对错） |
| 最终准确率 | 推理链最终答对比例 | 是 |
| 自洽接受率 | cal_est>阈值时接受的步骤占多少 | **否** |
| 冲突检测召回 | 验证器找到了多少真实矛盾 | 需要标注矛盾 |
| Uncertain 比例 | 系统输出 Uncertain 的比例 vs λ | **否** |
| 锚点距离分布 | 推理链节点到锚点的平均距离 | **否** |

最后三个指标**不依赖 ground truth**——这是这个 MVP 跟之前 Self Core 实验的关键区别。之前的所有评估都依赖"最终答对了吗"——外部信号。现在有内部可观测的指标了。

---

## 8. 实验设计

### 8.1 两组

| 组 | 描述 |
|---|---|
| A | Self Core + 结构验证器（验证器信号 → Gate，self-loss 来自结构信号） |
| C | 纯数据自指（Turn 1 → 看 ground truth → Turn 2 修正），无验证器，无 Self Core |

### 8.2 λ 取值

同 v5：`[0.0, 0.1, 0.2, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8]`

### 8.3 H1 的扩展预测

除了原有的 ECE 驼峰预测之外，MVP 增加两个内部可观测的预测：

- **H1b：** A 组的自洽接受率在 λ_c 附近非线性下降——系统开始拒绝自己生成的推理步骤。
- **H1c：** A 组 λ > λ_c 后，Uncertain 输出比例显著增加——系统更频繁地承认"我不知道"。

这两个预测**不依赖 ground truth**来验证。它们是纯内部行为的变化——这比 v5 更接近"内在证明判断"的精神。

---

## 9. 与 v5 的关系

v5 的实验已经在跑。MVP 是 v5 的下一步——不是替代，是升级。

| | v5 | MVP |
|---|---|---|
| 任务 | 判断等式对错（单步） | 多步推理链 |
| Gate 输入 | correctness bit + 结构特征 | 验证器结构信号 + 结构特征 |
| Self-loss | L1(cal_est, correctness) | L1(cal_est, connectivity × (1-conflict)) |
| 内部指标 | 无 | 自洽接受率、Uncertain 比例 |
| 证明图 | 无 | 有 |
| 锚点 | 无 | 有（硬编码） |

如果 v5 出了驼峰 → MVP 用来加深理解——驼峰的内部机制是什么。
如果 v5 没出驼峰 → MVP 用更丰富的内部信号重新尝试触发相变。

---

## 10. 实现顺序

### Phase 1：基础组件（1-2 天）

- `ArithExpr` + `execute()`
- `Proposition` + `ProofGraph`（dict + adjacency list，不用 networkx）
- `StructureVerifier`：冲突检测 + 连通性 BFS
- 单元测试：手动构造几个推理链，验证检测器输出正确

### Phase 2：Self Core 集成（1 天）

- 修改 `SelfCore.forward_gate`：输入增加验证器信号
- 修改训练循环：用结构信号替换 correctness bit
- 新的 self-loss 公式

### Phase 3：数据 + 训练（1-2 天）

- `make_reasoning_sample()` 数据生成
- 训练循环 + checkpoint
- 内部指标追踪（自洽接受率、Uncertain 比例）

### Phase 4：评估（1 天）

- ECE、准确率、H1b/H1c 检验
- 对比 A vs C
- 写分析文档

---

*"不是让系统知道什么是对的。是让系统知道——它知不知道什么是对的。"*
