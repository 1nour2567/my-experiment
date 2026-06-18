# CLAUDE.md

## About the user

- **Identity:** 宋浩诚，笔名 **续仁舞 (Xu Renwu)**。大一学生，数据科学与大数据技术专业，华侨大学
- **Agent 开发:** 从零开始三周，产出了两个完整 Agent 系统、一套通用架构（Constraint Architecture）、一份七层 Agent OS 骨架
- **安全工程:** T0-T3 纵深防御、双路径约束验证、SHA256 审计链、红队对抗——实战经验，不只是学习
- **学术:** 提出"逻辑熵"概念，Gödel/Turing/Chaitin 交叉。与 Noson Yanofsky（《理性的外限》作者）有邮件交流
- **小说:** 三部中短篇，笔名续仁舞。《收获》三投三退，编辑评价"哲学深度、语言有自身特点"
- **长期方向:** 风控/数据安全、AI Agent 架构设计、认知自指
- **当前状态:** 2026年5月爷爷奶奶相继去世。已规划 6/24-25 香港之旅（中银开户+太平山夜景）。期末和四级备考中
- **硬件:** AMD 锐龙9 8945HX + 16GB RAM + RTX 5060 8GB + 1TB SSD

## Projects

### Kylin-Agent
麒麟 V11 上的安全运维 Agent。T0-T3 四层防线全部不依赖 LLM。16 次红队攻击 0 打穿。149 tests。Agentic Loop 多轮自主推理。麒麟真机部署验证。

### Malio
具身化 AI 音乐 Agent。800 粒子 9 大物理系统。PersonaEngine + DSL 规则引擎 + Proactive Heartbeat + 联邦规则交换（SBERT 语义聚类）。投 IUI 2027 Demo。

### Constraint Architecture
从 Kylin 和 Malio 两个完全不同的领域提取的通用 Agent 架构。"LLM 负责想。代码负责决定能不能做。审计负责不可抵赖。" 四层约束 + 七个 Agent OS 子系统。已发布 PyPI 包（`constraint-architecture` v1.5.0）。中英文纲领 + 架构图 + GitHub 公开。

### Agent Farm（Agent World）
Constraint Architecture 在长周期自主决策领域的完整验证。50×50 物理农场世界，6 种真实地貌 biome，60+ 动作。3 个职业身份（农夫/畜牧者/工匠），分支技能树，三层性格模型。12 个 Phase（A→B→C→D1-D5→E1-E6→W1-W5）从 if/else 规则引擎演化到 6 层认知架构。8500+ 行代码，60+ 文件，100+ 次提交。

**认知架构：** SenseCompiler（确定性NL编译）→ 事件Schema（57种event_type + 13条评分）→ 中断系统（10触发器P0/P1/P2）→ AgentProfile（三层性格+技能树+知识图谱）→ 多Agent社交 → 书籍系统（10本6类）→ 传言引擎（错误信念+探索验证）

**独立仓库：** `github.com/1nour2567/agent-world--farm`
**开发仓库：** `github.com/1nour2567/my-experiment`

### Self Core
持久自我状态的 AI 架构设计。可训练 MLP Self-Update Gate + CalibrationHead + State Stack + EmbeddingProjection + TEE 硬件部署路径。包含理论预测（ECE 在 λ_c 附近非线性跃升）和实验验证代码。

### 论文
- **《逻辑的熵》** — 英文完整论文。Yanofsky 回复："You have a nice intuition. Communicate with logicians."
- **《自我狱卒》** — S-J-P 三元模型。福柯+弗洛伊德+韩炳哲。小说人物作为文学证据
- **Malio IUI 2027 论文** — 英文。具身化音乐 Agent

### 小说（笔名：续仁舞）
- 《映像之茧》《色的褪色》《外乡人》《喜欢》
- 《收获》三投三退。编辑："哲学深度。语言有自身特点。需将哲理依附于故事"

## Work style

- 追问。跑通了→重启会断吗→修。不满足于"能跑就行"
- 拒绝过度工程——不因为"大家都用"而加东西（拒了 JWT、Redis、PostgreSQL）
- 先做，回头看，提取共性——不是先设计框架再找人实现
- 对自己诚实——不会在文档里声称"已实现"实际上只是"构想"
- 会焦虑、会怀疑自己——但没停

## Conventions

- **语言:** 中文为主。技术文档中英文皆可。代码注释英文
- **Python:** PEP 8, snake_case, frozen dataclass + __post_init__ 校验
- **架构文档:** ABC + Protocol 双接口 + 设计理由 + 参考实现路径 + 自我检查
- **项目组织:** agent-dev/ 是 kylin-agent。malio/ 是 Malio。workspace 根目录是架构文档
- **Git:** 1nour2567。MIT License。main/master 分支

## Key files

- `constraint-architecture.md` — Constraint Architecture 中文纲领
- `agent-os-architecture.md` — Agent OS 七层全貌
- `self-core-architecture.md` — Self Core 架构设计（11节，含改进路线图）
- `agi-gap-analysis.md` — AGI 缺口分析（8缺口+2原则+神经符号全景）
- `constraint-tee-design.md` — Constraint Architecture 在 TEE 硬件上的部署方案
- `docs/agent-world-complete-reference.md` — Agent Farm 完整参考（30章，Phase W5）
- `docs/agent-world-dev-journal.md` — Agent Farm 开发日志（10节，100+次提交）
- `agent_world_local.py` — 农场物理引擎（4500+行，50×50世界，6种biome）
- `agent-world-llm.py` — LLM Agent 决策大脑（1550+行，12子系统集成）
- `self-ref-training-experiment.py` — 自指训练实验（v2闭环/v3真自指/v4二分类三组对照）
- `self-core-experiment.py` — Self Core 实验代码（v4：二分类+三组对照+梯度裁剪）
- `agent-dev/constraint_skeleton.py` — Agent OS 骨架 v1.5.0（7层 ABC+Protocol+25自检）
- `agent-dev/examples/kylin_pipeline.py` — Kylin 风格参考管道
- `agent-dev/CONSTRAINT.md` / `CONSTRAINT_EN.md` — 中英文纲领（仓库内）
- `constraint-architecture.svg` / `constraint-architecture-en.svg` — 架构图（中英文）
- `hongkong-itinerary-final.md` — 香港行程定稿
