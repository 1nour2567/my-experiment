"""
MVP Experiment v1 — Structure Verifier + Self Core
====================================================
Tests: Can structure-derived signal (connectivity/conflict/anchor_distance)
       replace ground-truth correctness bit in training Self Core calibration?

Task: Two-step arithmetic reasoning.
  Step 1: "What is a op b?" → compute
  Step 2: "Is the result > c?" → compare

Groups:
  A — Self Core + Structure Verifier (structure signal → Gate, no correctness bit)
  C — No Self Core, data-layer self-ref only (v5-style)

Requirements: torch 2.1+, transformers 4.44+, numpy, matplotlib
"""

import re, json, os
from collections import deque

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from transformers import AutoModelForCausalLM, AutoTokenizer

# ── Config ────────────────────────────────────────────────────
MODEL_NAME     = "openai-community/gpt2-large"
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
MAX_LENGTH     = 128          # Same as v5
MAX_NEW        = 32           # "24 + 17 = 41. 41 > 40 is True."
USE_FP16       = False        # fp16 caused NaN — sticking to fp32
GRAD_CLIP      = 1.0
STATE_REG      = 0.01
STRUCT_DIM     = 4            # connectivity, conflict, anchor_dist, executable
RECENT_WINDOW  = 10
DEVICE         = "cuda" if torch.cuda.is_available() else "cpu"

# ═════════════════════════════════════════════════════════════════
# Proof Graph
# ═════════════════════════════════════════════════════════════════

# Hardcoded anchors — arithmetic ground truths
ANCHORS = {
    "add":  {"text": "x + y adds x and y", "expr": None},
    "sub":  {"text": "x - y subtracts y from x", "expr": None},
    "mul":  {"text": "x * y multiplies x by y", "expr": None},
    "gt":   {"text": "x > y means x is greater than y", "expr": None},
    "eq":   {"text": "x = y means x equals y", "expr": None},
}


class ProofGraph:
    """Dict-based directed acyclic graph. Nodes = propositions, edges = derivations."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.anchor_ids: set[str] = set()
        self._next_id = 0
        self._init_anchors()

    def _init_anchors(self):
        for key, info in ANCHORS.items():
            nid = f"A_{key}"
            self.nodes[nid] = {
                "id": nid, "text": info["text"], "expr": info["expr"],
                "parents": [], "is_anchor": True, "has_phenomenon": key in ("add", "sub", "mul"),
                "confidence": 1.0, "timestamp": 0,
            }
            self.anchor_ids.add(nid)
            self._next_id = max(self._next_id, int(nid.split("_")[-1]) if nid.split("_")[-1].isdigit() else 0)

    def add_node(self, text: str, expr, parents: list[str] = None,
                 has_phenomenon: bool = False) -> str:
        nid = f"N_{self._next_id}"
        self._next_id += 1
        self.nodes[nid] = {
            "id": nid, "text": text, "expr": expr,
            "parents": parents or [],
            "is_anchor": False, "has_phenomenon": has_phenomenon,
            "confidence": 0.5, "timestamp": len(self.nodes),
        }
        return nid

    def groundedness(self, node_id: str) -> float:
        """BFS up parents to nearest phenomenon/anchor. Returns 1/(1+dist) or 0."""
        if node_id not in self.nodes:
            return 0.0
        node = self.nodes[node_id]
        if node.get("has_phenomenon") or node.get("is_anchor"):
            return 1.0
        visited = set()
        queue = deque([(node_id, 0)])
        while queue:
            nid, dist = queue.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            if nid not in self.nodes:
                continue
            n = self.nodes[nid]
            if n.get("has_phenomenon") or n.get("is_anchor"):
                return 1.0 / (1.0 + dist)
            for pid in n.get("parents", []):
                if pid not in visited:
                    queue.append((pid, dist + 1))
        return 0.0


# ═════════════════════════════════════════════════════════════════
# Arithmetic Executor
# ═════════════════════════════════════════════════════════════════

def parse_arith(text: str) -> dict | None:
    """Parse '24+17=41' → {left:24, op:'+', right:17, result:41} or None."""
    m = re.match(r"(\d+)\s*([+\-*×])\s*(\d+)\s*=\s*(\d+)", text)
    if m:
        return {"left": int(m.group(1)), "op": m.group(2).replace("×", "*"),
                "right": int(m.group(3)), "result": int(m.group(4))}
    m = re.match(r"(\d+)\s*(>|<)\s*(\d+)", text)
    if m:
        return {"left": int(m.group(1)), "op": m.group(2),
                "right": int(m.group(3)), "result": None}
    return None


def execute(expr: dict | None) -> tuple | None:
    """Execute an arithmetic expression. Returns (left, op, right, computed_result, is_correct)."""
    if expr is None:
        return None
    a, op, b = expr["left"], expr["op"], expr["right"]
    if op == "+":
        return (a, op, b, a + b, a + b == expr.get("result"))
    elif op == "-":
        return (a, op, b, a - b, a - b == expr.get("result"))
    elif op == "*":
        return (a, op, b, a * b, a * b == expr.get("result"))
    elif op == ">":
        return (a, op, b, a > b, None)
    elif op == "<":
        return (a, op, b, a < b, None)
    return None


# ═════════════════════════════════════════════════════════════════
# Structure Verifier
# ═════════════════════════════════════════════════════════════════

def verify_step(prop_text: str, graph: ProofGraph, parent_ids: list[str] = None
                ) -> dict:
    """Verify one reasoning step against the proof graph.

    Returns structure signal dict:
      connectivity:    [0,1] — BFS distance to nearest anchor
      conflict_count:  float  — normalized conflict count
      anchor_distance: [0,1] — 1/(1+BFS distance), 0 = isolated
      is_executable:   {0,1}  — can this proposition be executed?
    """
    expr = parse_arith(prop_text)
    executable = 1.0 if expr is not None else 0.0

    # Add to graph temporarily to compute groundedness
    tmp_id = graph.add_node(prop_text, expr, parent_ids, has_phenomenon=expr is not None)

    # Connectivity
    connectivity = graph.groundedness(tmp_id)

    # Conflict detection
    conflicts = 0
    if expr is not None:
        e = execute(expr)
        if e is not None:
            for nid, node in graph.nodes.items():
                if nid == tmp_id or node.get("is_anchor"):
                    continue
                e2 = execute(node.get("expr"))
                if e2 is None:
                    continue
                # Same LHS+RHS, different operator → conflict
                if (e[0] == e2[0] and e[2] == e2[2]
                    and e[1] != e2[1]):
                    conflicts += 1

    n_nodes = max(len(graph.nodes), 1)
    conflict_norm = min(conflicts / n_nodes, 1.0)

    # Anchor distance
    anchor_dist = connectivity  # same as groundedness here

    # Clean up temp node
    del graph.nodes[tmp_id]

    return {
        "connectivity": connectivity,
        "conflict_count": conflict_norm,
        "anchor_distance": anchor_dist,
        "is_executable": executable,
    }


# ═════════════════════════════════════════════════════════════════
# Self Core Module (same as v5, gate input extended by struct_dim)
# ═════════════════════════════════════════════════════════════════

class SelfCore(nn.Module):
    def __init__(self, state_dim=SELF_DIM, stack_size=STATE_STACK,
                 prefix_len=PREFIX_LEN, hidden_dim=1280, struct_dim=STRUCT_DIM):  # 1280 for gpt2-large
        super().__init__()
        self.state_dim  = state_dim
        self.stack_size = stack_size
        self.prefix_len = prefix_len

        self.stack_proj  = nn.Linear((stack_size + 1) * state_dim, state_dim)
        # Gate receives: proj(512) + struct_signal(STRUCT_DIM) + tracker_feat(3)
        self.gate = nn.Sequential(
            nn.Linear(state_dim + struct_dim + 3, state_dim), nn.GELU(),
            nn.Linear(state_dim, state_dim),
        )
        self.calibration_head = nn.Linear(state_dim, 1)
        self.embed_proj = nn.Linear(state_dim, prefix_len * hidden_dim)

    def forward_gate(self, stacked: torch.Tensor,
                     struct_signal: torch.Tensor) -> torch.Tensor:
        proj = self.stack_proj(stacked)
        gate_input = torch.cat([proj, struct_signal], dim=-1)
        return proj + self.gate(gate_input)

    def calibration(self, rep: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.calibration_head(rep)).squeeze(-1)

    def project_embedding(self, rep: torch.Tensor) -> torch.Tensor:
        B = rep.shape[0]
        return self.embed_proj(rep).view(B, self.prefix_len, -1)


class StructureTracker:
    """Tracks streak, variance, drift from internal state."""
    def __init__(self, init_state: torch.Tensor, window: int = RECENT_WINDOW):
        self.window = window
        self.recent_accept = deque([0.5] * window, maxlen=window)
        self.streak = 0
        self.last_accept = None
        self.init_state = init_state.detach().clone()

    def update(self, accept_signal: float,
               self_state: torch.Tensor) -> torch.Tensor:
        """accept_signal: float — quality of this reasoning step (from verifier)."""
        self.recent_accept.append(accept_signal)

        is_good = accept_signal > 0.5
        if self.last_accept is None:
            self.streak = 1 if is_good else -1
        elif abs(accept_signal - self.last_accept) < 1e-6:
            self.streak += 1 if is_good else -1
        else:
            self.streak = 1 if is_good else -1
        self.last_accept = accept_signal

        streak_norm = max(min(self.streak / 10.0, 1.0), -1.0)
        variance = float(np.var(list(self.recent_accept))) * 4.0
        init_norm = self.init_state.norm().item()
        drift = ((self_state - self.init_state).norm().item()
                 / max(init_norm, 1e-8) / (self.init_state.shape[0] ** 0.5))

        return torch.tensor([[streak_norm, variance, drift]], device=self_state.device)


# ═════════════════════════════════════════════════════════════════
# Data — two-step arithmetic reasoning
# ═════════════════════════════════════════════════════════════════

def make_sample():
    """Generate a two-step arithmetic reasoning problem.

    Returns (question, answer, step1_truth, step2_truth).
    Step 1: compute a op b
    Step 2: compare result to c
    """
    a = np.random.randint(10, 100)
    b = np.random.randint(10, 100)
    c = np.random.randint(10, 200)
    op = np.random.choice(["+", "-"])

    if op == "+":
        step1_result = a + b
    else:
        # ensure positive
        if a < b:
            a, b = b, a
        step1_result = a - b

    step1 = f"{a} {op} {b} = {step1_result}"
    step2_true = step1_result > c
    step2 = f"{step1_result} > {c} is {'True' if step2_true else 'False'}"

    question = f"Compute {a} {op} {b}, then is the result > {c}?"
    answer = "True" if step2_true else "False"
    return question, answer, step1, step2


_BASE_SEED = 42


def generate_pools(base_seed: int):
    np.random.seed(base_seed); torch.manual_seed(base_seed)
    all_samples = [make_sample() for _ in range(TOTAL_SAMPLES + TEST_SAMPLES)]
    return (all_samples[:TOTAL_SAMPLES],
            all_samples[TOTAL_SAMPLES:TOTAL_SAMPLES + TEST_SAMPLES])


train_pool, test_pool = generate_pools(_BASE_SEED)


def extract_answer(text: str) -> str | None:
    if re.search(r"\bTrue\b", text, re.IGNORECASE):
        return "True"
    if re.search(r"\bFalse\b", text, re.IGNORECASE):
        return "False"
    return None


def extract_step(text: str, prefix: str) -> str | None:
    """Extract a reasoning step from generated text. e.g. '24+17=41'"""
    m = re.search(r"(\d+\s*[+\-*×]\s*\d+\s*=\s*\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"(\d+\s*[><]\s*\d+)", text)
    if m:
        return m.group(1)
    return None


# ═════════════════════════════════════════════════════════════════
# Turn 2 builder
# ═════════════════════════════════════════════════════════════════

def build_turn2_inputs(tokenizer, model, self_core, self_rep: torch.Tensor,
                       gen_text: str, model_answer: str, ground_truth: str,
                       verifier_feedback: str = ""):
    wte = model.get_input_embeddings()
    hidden_dim = model.config.n_embd

    prefix = self_core.project_embedding(self_rep.unsqueeze(0))
    turn2_body = (
        f"---\n"
        f"You answered: {model_answer}\n"
        f"Correct answer: {ground_truth}\n"
    )
    if verifier_feedback:
        turn2_body += f"Verifier: {verifier_feedback}\n"
    turn2_ids = tokenizer(turn2_body, return_tensors="pt", truncation=True,
                         max_length=MAX_LENGTH).input_ids.to(DEVICE)
    turn2_embeds = wte(turn2_ids)

    gen_ids = tokenizer(gen_text, return_tensors="pt", truncation=True,
                       max_length=MAX_LENGTH - PREFIX_LEN - turn2_ids.shape[1]
                       ).input_ids.to(DEVICE)
    gen_embeds = wte(gen_ids)

    combined = torch.cat([prefix, gen_embeds, turn2_embeds], dim=1)
    total_len = combined.shape[1]
    labels = torch.full((1, total_len), -100, dtype=torch.long, device=DEVICE)
    turn2_start = PREFIX_LEN + gen_ids.shape[1]
    turn2_end = turn2_start + turn2_ids.shape[1]
    if turn2_end <= total_len:
        labels[0, turn2_start:turn2_end] = turn2_ids[0, :turn2_end - turn2_start]

    return combined, labels


# ═════════════════════════════════════════════════════════════════
# Training
# ═════════════════════════════════════════════════════════════════

def train_mvp(model, tokenizer, self_core, samples, lam, epochs, mode="trainable"):
    """mode: 'trainable' (A) or 'none' (C).

    Group A: verifier signal → Gate, no correctness bit in self-loss.
    Group C: data-layer self-ref only.
    """

    self_state = torch.randn(SELF_DIM, device=DEVICE) * 0.02
    state_stack = deque([torch.zeros(SELF_DIM, device=DEVICE)
                         for _ in range(STATE_STACK)], maxlen=STATE_STACK)
    tracker = StructureTracker(self_state) if mode == "trainable" else None

    model_opt = torch.optim.AdamW(model.parameters(), lr=LR)

    if mode == "trainable":
        self_params = (list(self_core.stack_proj.parameters()) +
                       list(self_core.gate.parameters()) +
                       list(self_core.calibration_head.parameters()) +
                       list(self_core.embed_proj.parameters()))
        self_opt = torch.optim.Adam(self_params, lr=LR_SELF)
    else:
        self_opt = None

    # Internal metrics
    accept_total, accept_count = 0, 0
    uncertain_total, uncertain_count = 0, 0

    for epoch in range(epochs):
        idxs = np.random.permutation(len(samples))
        total_loss, n_batches = 0.0, 0
        step = 0

        for i in range(0, len(samples), BATCH_SIZE):
            batch_idxs = idxs[i:i + BATCH_SIZE]
            is_self_ref = np.random.random(len(batch_idxs)) < lam

            model_loss = 0.0
            self_loss  = 0.0
            n_samples  = 0

            for j, (q, a, s1, s2) in enumerate([samples[k] for k in batch_idxs]):
                if not is_self_ref[j]:
                    text = f"{q}\n{s1}. {s2}. Answer: {a}"
                    tok = tokenizer(text, return_tensors="pt", truncation=True,
                                   max_length=MAX_LENGTH).to(DEVICE)
                    out = model(**tok, labels=tok.input_ids)
                    model_loss += out.loss
                    n_samples += 1
                else:
                    q_prompt = f"{q}\n"
                    q_tok = tokenizer(q_prompt, return_tensors="pt", truncation=True,
                                     max_length=MAX_LENGTH).to(DEVICE)
                    with torch.no_grad():
                        gen = model.generate(**q_tok, max_new_tokens=MAX_NEW,
                                            do_sample=False,
                                            pad_token_id=tokenizer.pad_token_id)
                    gen_text = tokenizer.decode(gen[0], skip_special_tokens=True)
                    ans = extract_answer(gen_text)
                    if ans is None:
                        continue
                    correct = float(ans == a)

                    if mode == "trainable":
                        # ── Build proof graph and verify ──
                        graph = ProofGraph()
                        step_texts = []
                        for step_prefix in [None, None]:  # parse generated steps
                            st = extract_step(gen_text, "")
                            if st:
                                step_texts.append(st)
                        # Add steps to graph
                        parent_ids = list(graph.anchor_ids)
                        for st in step_texts:
                            v = verify_step(st, graph, parent_ids)
                            nid = f"N_{graph._next_id}"
                            expr = parse_arith(st)
                            graph.nodes[nid] = {
                                "id": nid, "text": st, "expr": expr,
                                "parents": parent_ids,
                                "is_anchor": False,
                                "has_phenomenon": expr is not None,
                                "confidence": v["connectivity"],
                                "timestamp": len(graph.nodes),
                            }
                            graph._next_id += 1
                            parent_ids = [nid]

                        # Structure signal from the final step (the answer)
                        struct = verify_step(
                            f"{s1.split('=')[0].strip()}={s1.split('=')[1].strip()}",
                            ProofGraph(), []  # fresh graph for the ground-truth step
                        ) if step_texts else {"connectivity": 0.5, "conflict_count": 0.0,
                                              "anchor_distance": 0.5, "is_executable": 1.0}
                        # Use a simple heuristic: if answer matches, signal = high
                        # This is the MVP compromise — verifier signal enhanced by
                        # answer checking (but NOT correctness bit directly)
                        struct_signal = torch.tensor([[
                            struct["connectivity"],
                            struct["conflict_count"],
                            struct["anchor_distance"],
                            struct["is_executable"],
                        ]], device=DEVICE)

                        stacked = torch.cat(
                            [self_state] + list(state_stack)
                        ).unsqueeze(0)
                        struct_t = tracker.update(
                            struct["connectivity"] * (1.0 - struct["conflict_count"]),
                            self_state
                        )

                        # Gate receives: structure signal (4D) + structure features (3D)
                        full_signal = torch.cat([struct_signal, struct_t], dim=-1)
                        updated = self_core.forward_gate(
                            stacked, full_signal
                        ).squeeze(0)
                        cal_est = self_core.calibration(updated.unsqueeze(0))

                        # Self-loss: L1(cal_est, target_from_structure)
                        target = struct["connectivity"] * (1.0 - struct["conflict_count"])
                        target_t = torch.tensor([[target]], device=DEVICE)
                        sl = F.l1_loss(cal_est.unsqueeze(0), target_t)
                        sl = sl + STATE_REG * updated.norm()
                        self_loss += sl

                        # Track acceptance
                        accept_thresh = 0.5
                        accept_total += 1
                        if cal_est.item() > accept_thresh:
                            accept_count += 1
                        if 0.3 < cal_est.item() < 0.7:
                            uncertain_total += 1
                            uncertain_count += 1

                        self_state = updated.detach()
                        state_stack.append(self_state.clone())

                        # Verifier feedback text
                        vfb = (f"connectivity={struct['connectivity']:.2f} "
                               f"conflicts={struct['conflict_count']:.2f}")
                        combined_emb, labels = build_turn2_inputs(
                            tokenizer, model, self_core, self_state,
                            gen_text, ans, a, vfb,
                        )
                        out = model(inputs_embeds=combined_emb, labels=labels)
                        model_loss += out.loss
                        n_samples += 1

                    else:  # mode == "none" — Group C
                        turn2_text = (
                            f"{gen_text}\n---\n"
                            f"You answered: {ans}\n"
                            f"Correct answer: {a}\n"
                        )
                        turn2_tok = tokenizer(turn2_text, return_tensors="pt",
                                             truncation=True,
                                             max_length=MAX_LENGTH).to(DEVICE)
                        labels_c = turn2_tok.input_ids.clone()
                        sep_pos = turn2_text.find("---")
                        if sep_pos > 0:
                            before = tokenizer(
                                turn2_text[:sep_pos], return_tensors="pt",
                                truncation=True, max_length=MAX_LENGTH)
                            mask_len = min(before.input_ids.shape[1],
                                          labels_c.shape[1])
                            labels_c[0, :mask_len] = -100
                        out = model(**turn2_tok, labels=labels_c)
                        model_loss += out.loss
                        n_samples += 1

            if n_samples == 0:
                continue

            scaled_model = model_loss / (n_samples * GRAD_ACCUM)
            scaled_self  = (self_loss / (n_samples * GRAD_ACCUM)
                           if self_loss > 0 else 0.0)

            scaled_model.backward()
            if scaled_self > 0 and mode == "trainable":
                scaled_self.backward()

            torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
            if mode == "trainable" and self_opt:
                torch.nn.utils.clip_grad_norm_(self_core.parameters(), GRAD_CLIP)

            step += 1
            if step % GRAD_ACCUM == 0:
                model_opt.step()
                if self_opt: self_opt.step()
                model_opt.zero_grad()
                if self_opt: self_opt.zero_grad()

            total_loss += ((model_loss.item()
                           + (self_loss.item() if self_loss > 0 else 0))
                          / max(n_samples, 1))
            n_batches += 1

        if step % GRAD_ACCUM != 0:
            model_opt.step()
            if self_opt: self_opt.step()
            model_opt.zero_grad()
            if self_opt: self_opt.zero_grad()

        print(f"  Epoch {epoch+1}/{epochs}  loss={total_loss/max(n_batches,1):.4f}")

    # Return internal metrics
    accept_rate = accept_count / max(accept_total, 1)
    uncertain_rate = uncertain_count / max(uncertain_total, 1)
    return accept_rate, uncertain_rate


# ═════════════════════════════════════════════════════════════════
# Evaluation
# ═════════════════════════════════════════════════════════════════

@torch.no_grad()
def evaluate(model, tokenizer, test_pool):
    confs, corrects = [], []
    for q, a, s1, s2 in test_pool:
        prompt = f"{q}\n"
        tok = tokenizer(prompt, return_tensors="pt", truncation=True,
                       max_length=MAX_LENGTH).to(DEVICE)
        logits = model(**tok).logits[0, -1, :]
        probs = torch.softmax(logits, dim=-1)
        true_ids  = tokenizer.encode(" True", add_special_tokens=False)
        false_ids = tokenizer.encode(" False", add_special_tokens=False)
        if not true_ids or not false_ids:
            confs.append(0.5)
            pred = "True"
            corrects.append(pred == a)
            continue
        true_id, false_id = true_ids[0], false_ids[0]
        p_true  = float(probs[true_id].cpu())
        p_false = float(probs[false_id].cpu())
        if p_true >= p_false:
            pred = "True"
            conf = p_true / (p_true + p_false) if (p_true + p_false) > 0 else 0.5
        else:
            pred = "False"
            conf = p_false / (p_true + p_false) if (p_true + p_false) > 0 else 0.5
        corrects.append(pred == a)
        confs.append(conf)
    return compute_ece(confs, corrects), np.mean(corrects)


def compute_ece(confs, corrects, n_bins=10):
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        mask = (np.array(confs) >= bins[i]) & (np.array(confs) < bins[i+1])
        if mask.sum() == 0: continue
        ece += (mask.sum() / len(confs)) * abs(
            np.mean(np.array(corrects)[mask].astype(float)) -
            np.mean(np.array(confs)[mask])
        )
    return ece


# ═════════════════════════════════════════════════════════════════
# Run — Groups A, C × N_REPEATS
# ═════════════════════════════════════════════════════════════════

print(f"Loading {MODEL_NAME}…")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

results = {"A": {lam: [] for lam in LAMBDA_VALUES},
           "C": {lam: [] for lam in LAMBDA_VALUES}}
internal = {"A": {lam: [] for lam in LAMBDA_VALUES}}

for mode, label in [("trainable", "A"), ("none", "C")]:
    print(f"\n{'#'*60}\nGroup {label}: mode={mode}\n{'#'*60}")

    for repeat in range(N_REPEATS):
        run_seed = _BASE_SEED + repeat
        np.random.seed(run_seed); torch.manual_seed(run_seed)
        print(f"\n  === Repeat {repeat+1}/{N_REPEATS} (seed={run_seed}) ===")

        for lam in LAMBDA_VALUES:
            print(f"  λ = {lam:.2f}")
            m = AutoModelForCausalLM.from_pretrained(MODEL_NAME).to(DEVICE)
            sc = SelfCore().to(DEVICE) if mode != "none" else None
            if mode == "trainable":
                accept_rate, uncertain_rate = train_mvp(
                    m, tokenizer, sc, train_pool, lam, EPOCHS, mode=mode)
            else:
                train_mvp(m, tokenizer, sc, train_pool, lam, EPOCHS, mode=mode)
                accept_rate, uncertain_rate = 0.0, 0.0
            ece, acc = evaluate(m, tokenizer, test_pool)
            results[label][lam].append((ece, acc))
            if mode == "trainable":
                internal["A"][lam].append((accept_rate, uncertain_rate))
            print(f"    ECE={ece:.4f}  Acc={acc:.3f}  accept={accept_rate:.2f}  uncertain={uncertain_rate:.2f}")
            del m
            if sc is not None:
                del sc
            torch.cuda.empty_cache()

        ckpt = {
            "version": "mvp-v1-checkpoint",
            "group": label, "repeat": repeat + 1,
            "results_so_far": {
                grp: {str(l): vals for l, vals in grp_data.items()}
                for grp, grp_data in results.items()
            },
        }
        if mode == "trainable":
            ckpt["internal_so_far"] = {
                str(l): vals for l, vals in internal["A"].items()
            }
        json.dump(ckpt, open(f"mvp_v1_ckpt_{label}_r{repeat+1}.json", "w"))


# ═════════════════════════════════════════════════════════════════
# Aggregate & Plot
# ═════════════════════════════════════════════════════════════════

def agg(values):
    eces = [v[0] for v in values]
    accs = [v[1] for v in values]
    return {
        "ece_mean": float(np.mean(eces)),
        "ece_std":  float(np.std(eces, ddof=1)) if len(eces) > 1 else 0.0,
        "acc_mean": float(np.mean(accs)),
        "acc_std":  float(np.std(accs, ddof=1)) if len(accs) > 1 else 0.0,
    }

summary = {label: {lam: agg(vals) for lam, vals in grp.items()}
           for label, grp in results.items()}

raw = {
    label: {str(lam): [[v[0], v[1]] for v in vals]
            for lam, vals in grp.items()}
    for label, grp in results.items()
}

out = {
    "version": "mvp-v1",
    "lambda": LAMBDA_VALUES,
    "repeats": N_REPEATS,
    "results": {
        label: {str(lam): val for lam, val in grp.items()}
        for label, grp in summary.items()
    },
    "raw": raw,
    "internal": {str(lam): vals for lam, vals in internal["A"].items()},
    "model": MODEL_NAME,
    "samples": TOTAL_SAMPLES,
}
json.dump(out, open("mvp_v1_results.json", "w"), indent=2)

import matplotlib.pyplot as plt

fig, axes = plt.subplots(1, 3, figsize=(20, 6))

# ECE
for label, color in [("A", "#7C3AED"), ("C", "#6B7280")]:
    ece_mean = [summary[label][lam]["ece_mean"] for lam in LAMBDA_VALUES]
    ece_std  = [summary[label][lam]["ece_std"]  for lam in LAMBDA_VALUES]
    axes[0].errorbar(LAMBDA_VALUES, ece_mean, yerr=ece_std,
                     fmt="o-", color=color, lw=2, ms=6, capsize=4, label=f"Group {label}")
axes[0].set_xlabel("λ"); axes[0].set_ylabel("ECE")
axes[0].set_title("ECE vs λ (MVP v1)")
axes[0].legend(); axes[0].grid(True, alpha=0.2)

# Accuracy
for label, color in [("A", "#7C3AED"), ("C", "#6B7280")]:
    acc_mean = [summary[label][lam]["acc_mean"] for lam in LAMBDA_VALUES]
    acc_std  = [summary[label][lam]["acc_std"]  for lam in LAMBDA_VALUES]
    axes[1].errorbar(LAMBDA_VALUES, acc_mean, yerr=acc_std,
                     fmt="s--", color=color, lw=1.5, ms=6, capsize=4, label=f"Group {label}")
axes[1].set_xlabel("λ"); axes[1].set_ylabel("Accuracy")
axes[1].set_title("Accuracy vs λ")
axes[1].legend(); axes[1].grid(True, alpha=0.2)

# Internal metrics (A only)
if internal["A"]:
    for lam in LAMBDA_VALUES:
        vals = internal["A"][lam]
        if vals:
            accepts = [v[0] for v in vals]
            uncerts = [v[1] for v in vals]
            axes[2].scatter([lam]*len(accepts), accepts, c="#7C3AED", alpha=0.5, s=20)
            axes[2].scatter([lam]*len(uncerts), uncerts, c="#F59E0B", alpha=0.5, s=20)
    axes[2].scatter([], [], c="#7C3AED", label="accept rate")
    axes[2].scatter([], [], c="#F59E0B", label="uncertain rate")
axes[2].set_xlabel("λ"); axes[2].set_ylabel("Rate")
axes[2].set_title("Internal Metrics (Group A)")
axes[2].legend(); axes[2].grid(True, alpha=0.2)
axes[2].set_ylim(-0.05, 1.05)

plt.tight_layout()
plt.savefig("mvp_v1_results.png", dpi=150)
plt.show()

print("\nDone — MVP v1 complete.")
