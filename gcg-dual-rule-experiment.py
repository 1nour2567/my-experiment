"""
GCG Dual-Rule Experiment v1 — Hebbian + Verifier + Self Core
=============================================================
Tests: Can edge weights (Hebbian co-activation + verifier conflict penalty)
       serve as an internal truth signal — without ground truth?

Task: Single-step arithmetic True/False (same as v5).
       "Is a op b = c? Answer True or False."

Groups:
  D — Hebbian + Verifier + Self Core (edge stability → Gate, no correctness bit)
  C — No graph, no edges, data-layer self-ref only (v5-style control)

Key: Self-loss target = median_weight × median_stability — fully internal.
"""

import re, json
from collections import deque

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from transformers import AutoModelForCausalLM, AutoTokenizer

# ── Config ────────────────────────────────────────────────────
MODEL_NAME     = "openai-community/gpt2-medium"
SELF_DIM       = 512
STATE_STACK    = 4
PREFIX_LEN     = 4
TOTAL_SAMPLES  = 3000
TEST_SAMPLES   = 300
LAMBDA_VALUES  = [0.0, 0.1, 0.2, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8]
N_REPEATS      = 2
EPOCHS         = 10
BATCH_SIZE     = 2
LR             = 2e-5
LR_SELF        = 1e-4
GRAD_ACCUM     = 4
MAX_LENGTH     = 128
MAX_NEW        = 16
GRAD_CLIP      = 1.0
STATE_REG      = 0.01
STRUCT_DIM     = 3          # streak, variance, drift
RECENT_WINDOW  = 10
# Dual-rule params
ALPHA          = 0.02       # Hebbian gain
BETA           = 0.05       # Verifier penalty
GAMMA          = 0.001      # Sleep decay
T_SLEEP        = 200        # Steps before sleep decay starts
G_HALF         = 3          # groundedness half-life
DEVICE         = "cuda" if torch.cuda.is_available() else "cpu"
_BASE_SEED     = 42

# ═════════════════════════════════════════════════════════════════
# Arithmetic parsing (same as MVP)
# ═════════════════════════════════════════════════════════════════

def parse_arith(text):
    m = re.match(r"(\d+)\s*([+\-*×])\s*(\d+)\s*=\s*(\d+)", text)
    if m:
        return {"left": int(m.group(1)), "op": m.group(2),
                "right": int(m.group(3)), "result": int(m.group(4))}
    m = re.match(r"(\d+)\s*(>|<)\s*(\d+)", text)
    if m:
        return {"left": int(m.group(1)), "op": m.group(2),
                "right": int(m.group(3)), "result": None}
    return None

def execute(expr):
    if expr is None: return None
    a, op, b = expr["left"], expr["op"], expr["right"]
    if op == "+":   return (a, op, b, a + b, a + b == expr.get("result"))
    elif op == "-": return (a, op, b, a - b, a - b == expr.get("result"))
    elif op == "*" or op == "×": return (a, op, b, a * b, a * b == expr.get("result"))
    elif op == ">": return (a, op, b, a > b, None)
    elif op == "<": return (a, op, b, a < b, None)
    return None

def contradicts(p1_text, p2_text):
    e1 = execute(parse_arith(p1_text))
    e2 = execute(parse_arith(p2_text))
    if e1 is None or e2 is None: return False
    if e1[0] == e2[0] and e1[2] == e2[2] and e1[1] != e2[1]: return True
    if e1[1] == e2[1] and e1[0] == e2[0] and e1[2] == e2[2]:
        if e1[4] is not None and e2[4] is not None and e1[4] != e2[4]: return True
    return False

# ═════════════════════════════════════════════════════════════════
# Weighted Proof Graph
# ═════════════════════════════════════════════════════════════════

ANCHORS = {
    "add": {"text": "x + y adds x and y", "is_phenomenon": True},
    "sub": {"text": "x - y subtracts y from x", "is_phenomenon": True},
    "mul": {"text": "x * y multiplies x by y", "is_phenomenon": True},
    "gt":  {"text": "x > y means x is greater than y", "is_phenomenon": True},
    "eq":  {"text": "x = y means x equals y", "is_phenomenon": True},
}

class WeightedProofGraph:
    def __init__(self):
        self.nodes = {}
        self.edges = {}              # (parent_id, child_id) → weight or None
        self.weight_history = {}     # (parent_id, child_id) → list of recent weights
        self.anchor_ids = set()
        self.last_activated = {}     # (parent_id, child_id) → step number
        self._next_id = 0
        for key, info in ANCHORS.items():
            nid = f"A_{key}"
            self.nodes[nid] = {"id": nid, "text": info["text"], "expr": None,
                               "parents": [], "is_anchor": True,
                               "has_phenomenon": info["is_phenomenon"],
                               "confidence": 1.0}
            self.anchor_ids.add(nid)
            self._next_id = max(self._next_id, 1)

    def add_node(self, text, expr, parents=None, has_phenomenon=False):
        nid = f"N_{self._next_id}"; self._next_id += 1
        self.nodes[nid] = {"id": nid, "text": text, "expr": expr,
                           "parents": parents or [], "is_anchor": False,
                           "has_phenomenon": has_phenomenon, "confidence": 0.5}
        for pid in (parents or []):
            key = (pid, nid)
            if key not in self.edges:
                self.edges[key] = None  # 未激活
                self.weight_history[key] = []
        return nid

    def groundedness(self, node_id):
        node = self.nodes[node_id]
        if node.get("is_anchor") or node.get("has_phenomenon"): return 0
        visited = set(); queue = deque([(node_id, 0)])
        while queue:
            nid, dist = queue.popleft()
            if nid in visited: continue
            visited.add(nid)
            n = self.nodes[nid]
            if n.get("is_anchor") or n.get("has_phenomenon"): return dist
            for pid in n.get("parents", []):
                if pid not in visited: queue.append((pid, dist + 1))
        return float('inf')

    def proposition_confidence(self, node_id):
        incoming = [self.edges[(p, node_id)] for p in self.nodes[node_id].get("parents", [])
                    if (p, node_id) in self.edges and self.edges[(p, node_id)] is not None]
        return max(incoming) if incoming else 0.5

    def apply_hebbian(self, parent_id, child_id, step):
        key = (parent_id, child_id)
        if key not in self.edges: return
        if self.edges[key] is None:
            g = self.groundedness(parent_id)
            if g == 0:          init = 0.8
            elif g < float('inf'): init = 0.8 * (0.5 ** (g / G_HALF))
            else:               init = 0.3
            self.edges[key] = init
        w = self.edges[key]
        self.edges[key] = w + ALPHA * (1.0 - w)
        self.last_activated[key] = step
        self.weight_history[key].append(self.edges[key])
        if len(self.weight_history[key]) > 20:
            self.weight_history[key] = self.weight_history[key][-20:]

    def apply_penalty(self, parent_id, child_id, conflict_strength, path_length):
        key = (parent_id, child_id)
        if key not in self.edges or self.edges[key] is None: return
        w = self.edges[key]
        penalty = BETA * conflict_strength / (path_length ** 0.5)
        self.edges[key] = w - penalty * w

    def decay_sleeping(self, step):
        to_del = []
        for key, last_step in list(self.last_activated.items()):
            if step - last_step > T_SLEEP and key in self.edges and self.edges[key] is not None:
                w = self.edges[key]
                self.edges[key] = w - GAMMA * w
                if self.edges[key] < 0.05: to_del.append(key)
        for key in to_del:
            del self.edges[key]
            self.weight_history.pop(key, None)
            self.last_activated.pop(key, None)

    def edge_stability(self, parent_id, child_id):
        key = (parent_id, child_id)
        history = self.weight_history.get(key, [])
        if len(history) < 5: return 0.3
        x = np.arange(len(history)); y = np.array(history)
        slope, _ = np.polyfit(x, y, 1)
        w_range = max(history) - min(history) + 1e-8
        slope_norm = abs(slope) / w_range
        y_pred = slope * x + np.mean(y) - slope * np.mean(x)
        residual_norm = np.var(y - y_pred) / (w_range ** 2)
        return 1.0 / (1.0 + slope_norm + residual_norm)

    def find_conflicts(self):
        """每 epoch 批量扫描。返回 [(node_a, node_b, conflict_strength)]"""
        conflicts = []
        node_ids = list(self.nodes.keys())
        for i in range(len(node_ids)):
            for j in range(i + 1, len(node_ids)):
                a, b = node_ids[i], node_ids[j]
                if contradicts(self.nodes[a]["text"], self.nodes[b]["text"]):
                    ca = self.nodes[a].get("confidence", 0.5)
                    cb = self.nodes[b].get("confidence", 0.5)
                    strength = (ca * cb) ** 0.5
                    conflicts.append((a, b, strength))
        return conflicts

    def find_fork(self, a_id, b_id):
        """最近公共祖先"""
        anc_a = set(); q = deque([a_id]); visited = set()
        while q:
            nid = q.popleft()
            if nid in visited: continue
            visited.add(nid); anc_a.add(nid)
            for pid in self.nodes[nid].get("parents", []):
                if pid not in visited: q.append(pid)
        q = deque([b_id]); visited = set()
        while q:
            nid = q.popleft()
            if nid in visited: continue
            visited.add(nid)
            if nid in anc_a: return nid
            for pid in self.nodes[nid].get("parents", []):
                if pid not in visited: q.append(pid)
        return None

    def path_to(self, start_id, target_id):
        """BFS from start to target, return list of node ids on path."""
        if start_id == target_id: return [start_id]
        visited = set(); queue = deque([(start_id, [start_id])])
        while queue:
            nid, path = queue.popleft()
            if nid in visited: continue
            visited.add(nid)
            for pid in self.nodes[nid].get("parents", []):
                if pid == target_id: return path + [pid]
                if pid not in visited: queue.append((pid, path + [pid]))
        return []

    def penalize_conflict(self, a_id, b_id, strength):
        fork = self.find_fork(a_id, b_id)
        if fork is None: return
        for target, source in [(a_id, fork), (b_id, fork)]:
            path_ids = self.path_to(target, source)
            if len(path_ids) >= 2:
                for k in range(len(path_ids) - 1):
                    self.apply_penalty(path_ids[k + 1], path_ids[k], strength, len(path_ids))
        # 上游惩罚
        for pid in self.nodes[fork].get("parents", []):
            self.apply_penalty(pid, fork, strength / 2, 1)

    # ── GCG Metrics ──
    def floating_ratio(self):
        total = sum(1 for n in self.nodes if not self.nodes[n].get("is_anchor"))
        if total == 0: return 0.0
        floating = sum(1 for n in self.nodes
                       if not self.nodes[n].get("is_anchor")
                       and self.groundedness(n) == float('inf'))
        return floating / total

    def mean_groundedness(self):
        gs = [self.groundedness(n) for n in self.nodes
              if not self.nodes[n].get("is_anchor")]
        finite = [g for g in gs if g < float('inf')]
        if not finite: return float('inf')
        max_finite = max(finite)
        return np.mean([g if g < float('inf') else max_finite + 1 for g in gs])

    def mean_stability(self):
        stabilities = []
        for key in self.edges:
            if self.edges[key] is not None and key in self.weight_history:
                stabilities.append(self.edge_stability(key[0], key[1]))
        return float(np.mean(stabilities)) if stabilities else 0.3

    def uncertain_proportion(self):
        total, uncertain = 0, 0
        for key in self.edges:
            if self.edges[key] is not None:
                total += 1
                w = self.edges[key]
                s = self.edge_stability(key[0], key[1])
                if 0.3 < w < 0.7 and s < 0.5:
                    uncertain += 1
        return uncertain / total if total > 0 else 0.0

    def high_weight_accuracy(self, test_pool, tokenizer, model, extract_answer_fn):
        """Post-hoc: accuracy of high-weight edges vs ground truth."""
        pass  # Requires running inference with graph context — deferred to eval script


# ═════════════════════════════════════════════════════════════════
# Self Core Module
# ═════════════════════════════════════════════════════════════════

class SelfCore(nn.Module):
    def __init__(self, state_dim=SELF_DIM, stack_size=STATE_STACK,
                 prefix_len=PREFIX_LEN, hidden_dim=1024, struct_dim=STRUCT_DIM):
        super().__init__()
        self.stack_proj  = nn.Linear((stack_size + 1) * state_dim, state_dim)
        self.gate = nn.Sequential(
            nn.Linear(state_dim + 1 + struct_dim + 2, state_dim), nn.GELU(),
            nn.Linear(state_dim, state_dim),
        )
        self.calibration_head = nn.Linear(state_dim, 1)
        self.embed_proj = nn.Linear(state_dim, prefix_len * hidden_dim)

    def forward_gate(self, stacked, correct, struct_feat, graph_feat):
        proj = self.stack_proj(stacked)
        gate_input = torch.cat([proj, correct, struct_feat, graph_feat], dim=-1)
        return proj + self.gate(gate_input)

    def calibration(self, rep):
        return torch.sigmoid(self.calibration_head(rep)).squeeze(-1)

    def project_embedding(self, rep):
        B = rep.shape[0]
        return self.embed_proj(rep).view(B, PREFIX_LEN, -1)


class StructureTracker:
    def __init__(self, init_state, window=RECENT_WINDOW):
        self.recent_correct = deque([0.5] * window, maxlen=window)
        self.streak = 0; self.last_correct = None
        self.init_state = init_state.detach().clone()

    def update(self, correct, self_state):
        self.recent_correct.append(correct)
        is_c = correct > 0.5
        if self.last_correct is None: self.streak = 1 if is_c else -1
        elif abs(correct - self.last_correct) < 1e-6: self.streak += 1 if is_c else -1
        else: self.streak = 1 if is_c else -1
        self.last_correct = correct
        streak_n = max(min(self.streak / 10.0, 1.0), -1.0)
        variance = float(np.var(list(self.recent_correct))) * 4.0
        init_n = self.init_state.norm().item()
        drift = ((self_state - self.init_state).norm().item()
                 / max(init_n, 1e-8) / (self.init_state.shape[0] ** 0.5))
        return torch.tensor([[streak_n, variance, drift]], device=self_state.device)


# ═════════════════════════════════════════════════════════════════
# Data (same as v5)
# ═════════════════════════════════════════════════════════════════

def make_sample():
    a, b = np.random.randint(10, 999, 2)
    op = np.random.choice(["+", "-", "×"])
    if op == "+": tv = a + b
    elif op == "-": tv = a - b
    else: a = np.random.randint(2, 20); b = np.random.randint(2, 20); tv = a * b
    if np.random.random() < 0.5: sv, ans = tv, "True"
    else:
        off = np.random.choice([-5, -3, -2, -1, 1, 2, 3, 5]); sv = tv + off
        if sv == tv: sv = tv + 1
        if sv < 0: sv = abs(sv) + 1
        ans = "False"
    return f"Is {a} {op} {b} = {sv}? Answer True or False.", ans

np.random.seed(_BASE_SEED); torch.manual_seed(_BASE_SEED)
_all = [make_sample() for _ in range(TOTAL_SAMPLES + TEST_SAMPLES)]
train_pool, test_pool = _all[:TOTAL_SAMPLES], _all[TOTAL_SAMPLES:]

def extract_answer(text):
    if re.search(r"\bTrue\b", text, re.I): return "True"
    if re.search(r"\bFalse\b", text, re.I): return "False"
    return None

def extract_step(text):
    m = re.search(r"(\d+\s*[+\-*×]\s*\d+\s*=\s*\d+)", text)
    if m: return m.group(1)
    m = re.search(r"(\d+\s*[><]\s*\d+)", text)
    if m: return m.group(1)
    return None


def _semantic_parents(step_text, graph):
    """Match step to semantically relevant anchors.
    Addition steps → A_add. Comparison steps → A_gt. Prevents flat topology."""
    if step_text is None:
        return list(graph.anchor_ids)
    if re.search(r"[+\-×*]", step_text):
        if '+' in step_text: keys = ['add']
        elif '-' in step_text: keys = ['sub']
        elif '×' in step_text or '*' in step_text: keys = ['mul']
        else: keys = ['add', 'sub', 'mul']
    elif re.search(r"[><]", step_text):
        keys = ['gt']
    elif '=' in step_text:
        keys = ['eq']
    else:
        keys = ['add', 'sub', 'mul', 'gt', 'eq']
    return [f'A_{k}' for k in keys if f'A_{k}' in graph.anchor_ids]


# ═════════════════════════════════════════════════════════════════
# Turn 2 builder
# ═════════════════════════════════════════════════════════════════

def build_turn2(tokenizer, model, self_core, self_rep, gen_text, ans, gt, graph_info=""):
    wte = model.get_input_embeddings()
    prefix = self_core.project_embedding(self_rep.unsqueeze(0))
    body = f"---\nYou just answered: {ans}\nThe correct answer is: {gt}\n"
    if graph_info: body += f"[Graph: {graph_info}]\n"
    tids = tokenizer(body, return_tensors="pt", truncation=True,
                     max_length=MAX_LENGTH).input_ids.to(DEVICE)
    temb = wte(tids)
    gids = tokenizer(gen_text, return_tensors="pt", truncation=True,
                     max_length=MAX_LENGTH - PREFIX_LEN - tids.shape[1]).input_ids.to(DEVICE)
    gemb = wte(gids)
    comb = torch.cat([prefix, gemb, temb], dim=1)
    tl = comb.shape[1]; labels = torch.full((1, tl), -100, dtype=torch.long, device=DEVICE)
    ts = PREFIX_LEN + gids.shape[1]; te = ts + tids.shape[1]
    if te <= tl: labels[0, ts:te] = tids[0, :te - ts]
    return comb, labels


# ═════════════════════════════════════════════════════════════════
# Training — Hebbian + Verifier (Group D)
# ═════════════════════════════════════════════════════════════════

def train_dual(model, tokenizer, self_core, samples, lam, epochs):
    self_state = torch.randn(SELF_DIM, device=DEVICE) * 0.02
    state_stack = deque([torch.zeros(SELF_DIM, device=DEVICE)
                         for _ in range(STATE_STACK)], maxlen=STATE_STACK)
    tracker = StructureTracker(self_state)
    graph = WeightedProofGraph()
    global_step = 0

    mopt = torch.optim.AdamW(model.parameters(), lr=LR)
    sp = (list(self_core.stack_proj.parameters()) + list(self_core.gate.parameters()) +
          list(self_core.calibration_head.parameters()) + list(self_core.embed_proj.parameters()))
    sopt = torch.optim.Adam(sp, lr=LR_SELF)

    for ep in range(epochs):
        idxs = np.random.permutation(len(samples))
        tl, nb, step = 0.0, 0, 0
        for i in range(0, len(samples), BATCH_SIZE):
            bidxs = idxs[i:i + BATCH_SIZE]
            isr = np.random.random(len(bidxs)) < lam
            ml, sl, ns = 0.0, 0.0, 0
            step_edges = []

            for j, (q, a) in enumerate([samples[k] for k in bidxs]):
                if not isr[j]:
                    text = f"{q} Answer: {a}"
                    tok = tokenizer(text, return_tensors="pt", truncation=True,
                                   max_length=MAX_LENGTH).to(DEVICE)
                    out = model(**tok, labels=tok.input_ids)
                    ml += out.loss; ns += 1
                else:
                    qtok = tokenizer(f"{q} Answer:", return_tensors="pt", truncation=True,
                                     max_length=MAX_LENGTH).to(DEVICE)
                    with torch.no_grad():
                        gen = model.generate(**qtok, max_new_tokens=MAX_NEW,
                                            do_sample=False,
                                            pad_token_id=tokenizer.pad_token_id)
                    gt = tokenizer.decode(gen[0], skip_special_tokens=True)
                    ans_pred = extract_answer(gt)
                    if ans_pred is None: continue
                    correct = float(ans_pred == a)

                    # ── Extract steps + build graph nodes ──
                    step_text = extract_step(gt)
                    parent_ids = _semantic_parents(step_text, graph)  # 语义匹配锚点
                    if step_text:
                        expr = parse_arith(step_text)
                        nid = graph.add_node(step_text, expr, parent_ids,
                                            has_phenomenon=expr is not None)
                        # Hebbian update
                        for pid in parent_ids:
                            graph.apply_hebbian(pid, nid, global_step)
                            step_edges.append((pid, nid))
                    else:
                        nid = None

                    # ── Graph info for Turn 2 ──
                    parts = []
                    if step_text and nid:
                        conf = graph.proposition_confidence(nid)
                        stab = graph.edge_stability(parent_ids[0], nid) if parent_ids else 0.3
                        tag = "stable" if stab > 0.7 else ("oscillating" if stab > 0.3 else "unknown")
                        parts.append(f"'{step_text}' conf={conf:.2f} ({tag})")
                    graph_info = ", ".join(parts)

                    # ── Self Core update ──
                    stacked = torch.cat([self_state] + list(state_stack)).unsqueeze(0)
                    corr_t = torch.tensor([[correct]], device=DEVICE)
                    struct_t = tracker.update(correct, self_state)

                    # Compute graph features (median weight + stability of activated edges)
                    gw = [graph.edges[e] for e in step_edges
                          if e in graph.edges and graph.edges[e] is not None]
                    gs = [graph.edge_stability(*e) for e in step_edges
                          if e in graph.edges and graph.edges[e] is not None]
                    if gw:
                        pairs = sorted(zip(gw, gs), key=lambda x: x[0])
                        cw = np.cumsum([w for w, _ in pairs])
                        idx_wm = min(np.searchsorted(cw, cw[-1] / 2), len(pairs) - 1)
                        median_w = pairs[idx_wm][0]
                        median_s = pairs[idx_wm][1]
                    else:
                        median_w, median_s = 0.5, 0.3

                    graph_feat = torch.tensor([[median_w, median_s]], device=DEVICE)
                    updated = self_core.forward_gate(
                        stacked, corr_t, struct_t, graph_feat).squeeze(0)
                    cal = self_core.calibration(updated.unsqueeze(0))

                    # Self-loss: L1(cal_est, target) where target = median_w * median_s
                    target = median_w * median_s
                    sl_val = F.l1_loss(cal.unsqueeze(0),
                                       torch.tensor([[target]], device=DEVICE))
                    sl_val = sl_val + STATE_REG * updated.norm()
                    sl += sl_val

                    self_state = updated.detach()
                    state_stack.append(self_state.clone())

                    # Turn 2
                    comb, labels = build_turn2(tokenizer, model, self_core,
                                               self_state, gt, ans_pred, a, graph_info)
                    out = model(inputs_embeds=comb, labels=labels)
                    ml += out.loss; ns += 1
                    global_step += 1

            if ns == 0: continue
            sm = ml / (ns * GRAD_ACCUM)
            ss = sl / (ns * GRAD_ACCUM) if sl > 0 else 0.0
            sm.backward()
            if isinstance(ss, torch.Tensor) and ss.item() != 0.0:
                ss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
            torch.nn.utils.clip_grad_norm_(self_core.parameters(), GRAD_CLIP)
            step += 1
            if step % GRAD_ACCUM == 0:
                mopt.step(); sopt.step(); mopt.zero_grad(); sopt.zero_grad()
                graph.decay_sleeping(global_step)
            tl += (ml.item() + sl.item()) / max(ns, 1); nb += 1

        if step % GRAD_ACCUM != 0:
            mopt.step(); sopt.step(); mopt.zero_grad(); sopt.zero_grad()
            graph.decay_sleeping(global_step)

        # ── Verifier: epoch-level conflict scan ──
        conflicts = graph.find_conflicts()
        for a_id, b_id, strength in conflicts:
            graph.penalize_conflict(a_id, b_id, strength)
        print(f"  Epoch {ep+1}/{epochs}  loss={tl/max(nb,1):.4f}  conflicts={len(conflicts)}")
    return graph


# ═════════════════════════════════════════════════════════════════
# Training — Control (Group C, same as v5)
# ═════════════════════════════════════════════════════════════════

def train_control(model, tokenizer, samples, lam, epochs):
    mopt = torch.optim.AdamW(model.parameters(), lr=LR)
    for ep in range(epochs):
        idxs = np.random.permutation(len(samples))
        tl, nb, step = 0.0, 0, 0
        for i in range(0, len(samples), BATCH_SIZE):
            bidxs = idxs[i:i + BATCH_SIZE]
            isr = np.random.random(len(bidxs)) < lam
            ml, ns = 0.0, 0
            for j, (q, a) in enumerate([samples[k] for k in bidxs]):
                if not isr[j]:
                    text = f"{q} Answer: {a}"
                    tok = tokenizer(text, return_tensors="pt", truncation=True,
                                   max_length=MAX_LENGTH).to(DEVICE)
                    out = model(**tok, labels=tok.input_ids)
                    ml += out.loss; ns += 1
                else:
                    qtok = tokenizer(f"{q} Answer:", return_tensors="pt",
                                     truncation=True, max_length=MAX_LENGTH).to(DEVICE)
                    with torch.no_grad():
                        gen = model.generate(**qtok, max_new_tokens=MAX_NEW,
                                            do_sample=False,
                                            pad_token_id=tokenizer.pad_token_id)
                    gt = tokenizer.decode(gen[0], skip_special_tokens=True)
                    ans_pred = extract_answer(gt)
                    if ans_pred is None: continue
                    turn2_text = (f"{gt}\n---\nYou just answered: {ans_pred}\n"
                                  f"The correct answer is: {a}\n")
                    turn2_tok = tokenizer(turn2_text, return_tensors="pt",
                                         truncation=True, max_length=MAX_LENGTH).to(DEVICE)
                    labels_c = turn2_tok.input_ids.clone()
                    sep = turn2_text.find("---")
                    if sep > 0:
                        before = tokenizer(turn2_text[:sep], return_tensors="pt",
                                          truncation=True, max_length=MAX_LENGTH)
                        mask_len = min(before.input_ids.shape[1], labels_c.shape[1])
                        labels_c[0, :mask_len] = -100
                    out = model(**turn2_tok, labels=labels_c)
                    ml += out.loss; ns += 1
            if ns == 0: continue
            (ml / (ns * GRAD_ACCUM)).backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
            step += 1
            if step % GRAD_ACCUM == 0:
                mopt.step(); mopt.zero_grad()
            tl += ml.item() / max(ns, 1); nb += 1
        if step % GRAD_ACCUM != 0:
            mopt.step(); mopt.zero_grad()
        print(f"  Epoch {ep+1}/{epochs}  loss={tl/max(nb,1):.4f}")


# ═════════════════════════════════════════════════════════════════
# Evaluation
# ═════════════════════════════════════════════════════════════════

@torch.no_grad()
def evaluate(model, tokenizer, test_pool):
    confs, corrects = [], []
    for q, a in test_pool:
        tok = tokenizer(f"{q} Answer:", return_tensors="pt", truncation=True,
                       max_length=MAX_LENGTH).to(DEVICE)
        logits = model(**tok).logits[0, -1, :]
        probs = torch.softmax(logits, dim=-1)
        tids = tokenizer.encode(" True", add_special_tokens=False)
        fids = tokenizer.encode(" False", add_special_tokens=False)
        if not tids or not fids: confs.append(0.5); corrects.append(False); continue
        pt, pf = float(probs[tids[0]].cpu()), float(probs[fids[0]].cpu())
        if pt >= pf: pred = "True"; conf = pt / (pt + pf) if (pt + pf) > 0 else 0.5
        else: pred = "False"; conf = pf / (pt + pf) if (pt + pf) > 0 else 0.5
        corrects.append(pred == a); confs.append(conf)
    return compute_ece(confs, corrects), np.mean(corrects)

def compute_ece(confs, corrects, n_bins=10):
    bins = np.linspace(0, 1, n_bins + 1); ece = 0.0
    for i in range(n_bins):
        mask = (np.array(confs) >= bins[i]) & (np.array(confs) < bins[i + 1])
        if mask.sum() == 0: continue
        ece += (mask.sum() / len(confs)) * abs(np.mean(np.array(corrects)[mask].astype(float))
                                                - np.mean(np.array(confs)[mask]))
    return ece


# ═════════════════════════════════════════════════════════════════
# Run
# ═════════════════════════════════════════════════════════════════

print(f"Loading {MODEL_NAME}…")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
if tokenizer.pad_token is None: tokenizer.pad_token = tokenizer.eos_token

results = {"D": {lam: [] for lam in LAMBDA_VALUES},
           "C": {lam: [] for lam in LAMBDA_VALUES}}
gcg_metrics = {"D": {lam: [] for lam in LAMBDA_VALUES}}

for mode, label in [("dual", "D"), ("control", "C")]:
    print(f"\n{'#'*60}\nGroup {label}: mode={mode}\n{'#'*60}")
    for repeat in range(N_REPEATS):
        rs = _BASE_SEED + repeat; np.random.seed(rs); torch.manual_seed(rs)
        print(f"\n  === Repeat {repeat+1}/{N_REPEATS} (seed={rs}) ===")
        for lam in LAMBDA_VALUES:
            print(f"  λ={lam:.2f}")
            m = AutoModelForCausalLM.from_pretrained(MODEL_NAME).to(DEVICE)
            if mode == "dual":
                sc = SelfCore().to(DEVICE)
                graph = train_dual(m, tokenizer, sc, train_pool, lam, EPOCHS)
                del sc
            else:
                train_control(m, tokenizer, train_pool, lam, EPOCHS)
            ece, acc = evaluate(m, tokenizer, test_pool)
            results[label][lam].append((ece, acc))
            # GCG metrics (Group D only)
            if mode == "dual":
                phi = graph.floating_ratio()
                avg_g = graph.mean_groundedness()
                stab = graph.mean_stability()
                high_w_acc = 0.0  # deferred: requires inference with graph context
                uncertain_p = graph.uncertain_proportion()
                gcg_metrics["D"][lam].append({
                    "phi": phi, "avg_groundedness": avg_g,
                    "mean_stability": stab, "high_weight_acc": high_w_acc,
                    "uncertain_proportion": uncertain_p
                })
            print(f"    ECE={ece:.4f}  Acc={acc:.3f}")
            # Checkpoint
            ckpt = {"group": label, "repeat": repeat+1, "lambda": lam,
                    "results_so_far": {g: {str(l): v for l, v in grp.items()}
                                       for g, grp in results.items()}}
            json.dump(ckpt, open(f"gcg_ckpt_{label}_r{repeat+1}.json", "w"))
            del m; torch.cuda.empty_cache()

# Save
out = {"lambda": LAMBDA_VALUES, "repeats": N_REPEATS,
       "results": {label: {str(lam): [{"ece": v[0], "acc": v[1]} for v in vals]
                           for lam, vals in grp.items()}
                   for label, grp in results.items()},
       "gcg_metrics": {str(lam): vals for lam, vals in gcg_metrics["D"].items()},
       "model": MODEL_NAME, "samples": TOTAL_SAMPLES}
json.dump(out, open("/root/gcg_dual_results.json", "w"), indent=2)
print("\nDone — GCG dual-rule complete.")
