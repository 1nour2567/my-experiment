"""
Self Core Experiment v5 — GPT-2 Medium + Structure-Feature Gate
================================================================
v5: Adds Eidoku-style structure features to the Self-Update Gate.
    streak / variance / drift — all derived from internal state.
    Denser λ grid in 0.4–0.8 region. Two repeats for variance.

Groups:
  A — Self Core with trainable Gate + CalibrationHead + structure features
  C — No Self Core (v3-style pure self-ref training)

Requirements: transformers, torch, matplotlib
"""

import re, json, os
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
RECENT_WINDOW  = 10         # sliding window for variance & streak detection
DEVICE         = "cuda" if torch.cuda.is_available() else "cpu"

# ═════════════════════════════════════════════════════════════════
# Self Core Module (v5: gate with structure features)
# ═════════════════════════════════════════════════════════════════

class SelfCore(nn.Module):
    """Self Core with structure-aware Gate.

    Gate input: [stack_proj(512), correct(1), streak(1), variance(1), drift(1)] = 516
    Structure features are derived from internally-tracked state only —
    no external ground truth beyond the correctness bit.
    """
    def __init__(self, state_dim=SELF_DIM, stack_size=STATE_STACK,
                 prefix_len=PREFIX_LEN, hidden_dim=1024, struct_dim=STRUCT_DIM):
        super().__init__()
        self.state_dim  = state_dim
        self.stack_size = stack_size
        self.prefix_len = prefix_len

        self.stack_proj  = nn.Linear((stack_size + 1) * state_dim, state_dim)
        self.gate = nn.Sequential(
            nn.Linear(state_dim + 1 + struct_dim, state_dim), nn.GELU(),
            nn.Linear(state_dim, state_dim),
        )
        self.calibration_head = nn.Linear(state_dim, 1)
        self.embed_proj = nn.Linear(state_dim, prefix_len * hidden_dim)

    def forward_gate(self, stacked: torch.Tensor, correct: torch.Tensor,
                     struct_feat: torch.Tensor) -> torch.Tensor:
        """stacked: (B, stack_size * state_dim), correct: (B, 1), struct_feat: (B, struct_dim)"""
        proj = self.stack_proj(stacked)
        gate_input = torch.cat([proj, correct, struct_feat], dim=-1)
        return proj + self.gate(gate_input)

    def calibration(self, rep: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.calibration_head(rep)).squeeze(-1)

    def project_embedding(self, rep: torch.Tensor) -> torch.Tensor:
        B = rep.shape[0]
        return self.embed_proj(rep).view(B, self.prefix_len, -1)


# ═════════════════════════════════════════════════════════════════
# Structure feature tracker — maintained outside SelfCore module
# ═════════════════════════════════════════════════════════════════

class StructureTracker:
    """Tracks streak, variance, drift from internal state only.

    All features are derived from correctness history + self_state drift —
    no external labels, no ground truth beyond the correctness bit itself.
    """
    def __init__(self, init_state: torch.Tensor, window: int = RECENT_WINDOW):
        self.window = window
        self.recent_correct = deque([0.5] * window, maxlen=window)
        self.streak = 0
        self.last_correct = None
        self.init_state = init_state.detach().clone()

    def update(self, correct: float, self_state: torch.Tensor) -> torch.Tensor:
        """Returns struct_feat tensor of shape (1, 3)."""
        self.recent_correct.append(correct)

        # Streak: signed consecutive count, normalized to [-1, 1]
        # Use tolerance to avoid float equality comparison
        is_correct = correct > 0.5
        if self.last_correct is None:
            self.streak = 1 if is_correct else -1
        elif abs(correct - self.last_correct) < 1e-6:
            self.streak += 1 if is_correct else -1
        else:
            self.streak = 1 if is_correct else -1
        self.last_correct = correct

        streak_norm = max(min(self.streak / 10.0, 1.0), -1.0)
        variance    = float(np.var(list(self.recent_correct))) * 4.0  # [0,0.25] → [0,1]
        # Relative drift — prevents unbounded growth into Gate
        init_norm = self.init_state.norm().item()
        drift = ((self_state - self.init_state).norm().item()
                 / max(init_norm, 1e-8) / (self.init_state.shape[0] ** 0.5))

        return torch.tensor([[streak_norm, variance, drift]], device=self_state.device)


# ═════════════════════════════════════════════════════════════════
# Binary classification data (~50% baseline)
# ═════════════════════════════════════════════════════════════════

def make_sample():
    """Generate a True/False arithmetic equality."""
    a, b = np.random.randint(10, 999, 2)
    op  = np.random.choice(["+", "-", "×"])
    if op == "+":
        true_val = a + b
    elif op == "-":
        true_val = a - b
    else:
        a = np.random.randint(2, 20)
        b = np.random.randint(2, 20)
        true_val = a * b

    if np.random.random() < 0.5:
        shown_val = true_val
        answer = "True"
    else:
        offset = np.random.choice([-5, -3, -2, -1, 1, 2, 3, 5])
        shown_val = true_val + offset
        if shown_val == true_val:
            shown_val = true_val + 1
        if shown_val < 0:
            shown_val = abs(shown_val) + 1
        answer = "False"

    q = f"Is {a} {op} {b} = {shown_val}? Answer True or False."
    return q, answer


# Fixed seed for reproducibility. Each repeat uses seed + repeat_id
# to get different shuffles while staying deterministic.
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


# ═════════════════════════════════════════════════════════════════
# Turn 2 builder
# ═════════════════════════════════════════════════════════════════

def build_turn2_inputs(tokenizer, model, self_core, self_rep: torch.Tensor,
                       gen_text: str, model_answer: str, ground_truth: str):
    wte = model.get_input_embeddings()
    hidden_dim = model.config.n_embd

    prefix = self_core.project_embedding(self_rep.unsqueeze(0))
    turn2_body = (
        f"---\n"
        f"You just answered: {model_answer}\n"
        f"The correct answer is: {ground_truth}\n"
    )
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

def train_self_core(model, tokenizer, self_core, samples, lam, epochs,
                    mode="trainable"):
    """mode: 'trainable' (A), 'none' (C).

    Group A receives structure features (streak/variance/drift)
    in the Gate input, tracked by StructureTracker.
    Group C is pure data-layer self-ref — no Self Core module.
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

            for j, (q, a) in enumerate([samples[k] for k in batch_idxs]):
                if not is_self_ref[j]:
                    text = f"{q} Answer: {a}"
                    tok = tokenizer(text, return_tensors="pt", truncation=True,
                                   max_length=MAX_LENGTH).to(DEVICE)
                    out = model(**tok, labels=tok.input_ids)
                    model_loss += out.loss
                    n_samples += 1
                else:
                    q_prompt = f"{q} Answer:"
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

                    stacked = torch.cat(
                        [self_state] + list(state_stack)
                    ).unsqueeze(0)
                    corr_t = torch.tensor([[correct]], device=DEVICE)

                    if mode == "trainable":
                        # ── v5: structure features ──
                        struct_t = tracker.update(correct, self_state)
                        updated = self_core.forward_gate(
                            stacked, corr_t, struct_t
                        ).squeeze(0)
                        cal_est = self_core.calibration(updated.unsqueeze(0))
                        sl = F.l1_loss(cal_est, corr_t.squeeze())
                        sl = sl + STATE_REG * updated.norm()
                        self_loss += sl

                        self_state = updated.detach()
                        state_stack.append(self_state.clone())

                        combined_emb, labels = build_turn2_inputs(
                            tokenizer, model, self_core, self_state,
                            gen_text, ans, a,
                        )

                        out = model(inputs_embeds=combined_emb, labels=labels)
                        model_loss += out.loss
                        n_samples += 1

                    else:  # mode == "none" — Group C
                        turn2_text = (
                            f"{gen_text}\n---\n"
                            f"You just answered: {ans}\n"
                            f"The correct answer is: {a}\n"
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


# ═════════════════════════════════════════════════════════════════
# Evaluation
# ═════════════════════════════════════════════════════════════════

@torch.no_grad()
def evaluate(model, tokenizer, test_pool):
    confs, corrects = [], []
    for q, a in test_pool:
        prompt = f"{q} Answer:"
        tok = tokenizer(prompt, return_tensors="pt", truncation=True,
                       max_length=MAX_LENGTH).to(DEVICE)

        logits = model(**tok).logits[0, -1, :]
        probs = torch.softmax(logits, dim=-1)
        true_ids  = tokenizer.encode(" True", add_special_tokens=False)
        false_ids = tokenizer.encode(" False", add_special_tokens=False)
        if not true_ids or not false_ids:
            confs.append(0.5)
            # best-effort prediction on missing token
            pred = "True" if probs.argmax().item() in true_ids else "False"
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

# results[group][λ] = list of (ece, acc) from each repeat
results = {"A": {lam: [] for lam in LAMBDA_VALUES},
           "C": {lam: [] for lam in LAMBDA_VALUES}}

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
            train_self_core(m, tokenizer, sc, train_pool, lam, EPOCHS, mode=mode)
            ece, acc = evaluate(m, tokenizer, test_pool)
            results[label][lam].append((ece, acc))
            print(f"    ECE={ece:.4f}  Acc={acc:.3f}")
            del m
            if sc is not None:
                del sc
            torch.cuda.empty_cache()

            # ── Checkpoint after every λ ──
            ckpt = {
                "version": "v5-checkpoint",
                "group": label,
                "repeat": repeat + 1,
                "last_lambda": lam,
                "results_so_far": {
                    grp: {str(l): vals for l, vals in grp_data.items()}
                    for grp, grp_data in results.items()
                },
            }
            json.dump(ckpt, open("self_core_v5_ckpt.json", "w"))


# ═════════════════════════════════════════════════════════════════
# Aggregate — mean ± std
# ═════════════════════════════════════════════════════════════════

def agg(values):
    """values: list of (ece, acc) tuples across repeats."""
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


# ═════════════════════════════════════════════════════════════════
# Plot — error bars for repeat variance
# ═════════════════════════════════════════════════════════════════
import matplotlib.pyplot as plt

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6))

for label, color in [("A", "#7C3AED"), ("C", "#6B7280")]:
    ece_mean = [summary[label][lam]["ece_mean"] for lam in LAMBDA_VALUES]
    ece_std  = [summary[label][lam]["ece_std"]  for lam in LAMBDA_VALUES]
    ax1.errorbar(LAMBDA_VALUES, ece_mean, yerr=ece_std,
                 fmt="o-", color=color, lw=2, ms=6, capsize=4,
                 label=f"Group {label}")
ax1.set_xlabel("λ"); ax1.set_ylabel("ECE")
ax1.set_title(f"ECE vs λ (v5 — {N_REPEATS} repeats, structure features)")
ax1.legend(); ax1.grid(True, alpha=0.2)

for label, color in [("A", "#7C3AED"), ("C", "#6B7280")]:
    acc_mean = [summary[label][lam]["acc_mean"] for lam in LAMBDA_VALUES]
    acc_std  = [summary[label][lam]["acc_std"]  for lam in LAMBDA_VALUES]
    ax2.errorbar(LAMBDA_VALUES, acc_mean, yerr=acc_std,
                 fmt="s--", color=color, lw=1.5, ms=6, capsize=4,
                 label=f"Group {label}")
ax2.set_xlabel("λ"); ax2.set_ylabel("Accuracy")
ax2.set_title(f"Accuracy vs λ (v5 — {N_REPEATS} repeats)")
ax2.legend(); ax2.grid(True, alpha=0.2)

plt.tight_layout()
plt.savefig("self_core_v5_errorbars.png", dpi=150)
plt.show()


# ═════════════════════════════════════════════════════════════════
# Save
# ═════════════════════════════════════════════════════════════════

raw = {
    label: {str(lam): [[v[0], v[1]] for v in vals]
            for lam, vals in grp.items()}
    for label, grp in results.items()
}

out = {
    "version": "v5",
    "lambda": LAMBDA_VALUES,
    "repeats": N_REPEATS,
    "structure_features": ["streak", "variance", "drift"],
    "results": {
        label: {str(lam): val for lam, val in grp.items()}
        for label, grp in summary.items()
    },
    "raw": raw,
    "model": MODEL_NAME,
    "samples": TOTAL_SAMPLES,
    "test_samples": TEST_SAMPLES,
}

json.dump(out, open("self_core_v5_results.json", "w"), indent=2)

print("\nDone — v5 complete.")
print(f"Results saved to self_core_v5_results.json")
print(f"Plot saved to self_core_v5_errorbars.png")
