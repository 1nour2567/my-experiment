"""
Self Core Experiment — GPT-2 Medium + Persistent Self-State
============================================================
v4: Binary classification task with balanced feedback (~50% baseline).
    Larger dataset (5000), more epochs (10), gradient clipping,
    state norm regularization, reduced self LR.

Task: "Is this arithmetic equality correct? Answer True or False."
       Baseline accuracy ≈ 50% — gives Self Core balanced True/False signals.

Groups:
  A — Self Core with trainable Gate + CalibrationHead
  B — Hardcoded Gate (separate scalar calibration_est, fixed formula)
  C — No Self Core (v3-style pure self-ref training)

Requirements: transformers, torch, matplotlib
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
TOTAL_SAMPLES  = 5000
TEST_SAMPLES   = 200
LAMBDA_VALUES  = [0.0, 0.05, 0.1, 0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 1.0]
EPOCHS         = 10
BATCH_SIZE     = 2
LR             = 2e-5
LR_SELF        = 1e-4       # Reduced from 1e-3
GRAD_ACCUM     = 4
MAX_LENGTH     = 128
MAX_NEW        = 16         # "True" / "False" — very short
GRAD_CLIP      = 1.0
STATE_REG      = 0.01       # State norm regularization weight
DEVICE         = "cuda" if torch.cuda.is_available() else "cpu"

# ═════════════════════════════════════════════════════════════════
# Self Core Module
# ═════════════════════════════════════════════════════════════════

class SelfCore(nn.Module):
    def __init__(self, state_dim=SELF_DIM, stack_size=STATE_STACK,
                 prefix_len=PREFIX_LEN, hidden_dim=1024):
        super().__init__()
        self.state_dim  = state_dim
        self.stack_size = stack_size
        self.prefix_len = prefix_len

        self.stack_proj  = nn.Linear(stack_size * state_dim, state_dim)
        self.gate = nn.Sequential(
            nn.Linear(state_dim + 1, state_dim), nn.GELU(),
            nn.Linear(state_dim, state_dim),
        )
        self.calibration_head = nn.Linear(state_dim, 1)
        self.embed_proj = nn.Linear(state_dim, prefix_len * hidden_dim)

    def forward_gate(self, stacked: torch.Tensor, correct: torch.Tensor) -> torch.Tensor:
        proj = self.stack_proj(stacked)
        return proj + self.gate(torch.cat([proj, correct], dim=-1))

    def calibration(self, rep: torch.Tensor) -> torch.Tensor:
        return torch.sigmoid(self.calibration_head(rep)).squeeze(-1)

    def project_embedding(self, rep: torch.Tensor) -> torch.Tensor:
        B = rep.shape[0]
        return self.embed_proj(rep).view(B, self.prefix_len, -1)


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

    # 50% correct, 50% wrong (nearby number)
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


np.random.seed(42); torch.manual_seed(42)
all_samples = [make_sample() for _ in range(TOTAL_SAMPLES + TEST_SAMPLES)]
train_pool = all_samples[:TOTAL_SAMPLES]
test_pool  = all_samples[TOTAL_SAMPLES:TOTAL_SAMPLES + TEST_SAMPLES]


def extract_answer(text: str) -> str | None:
    """Extract True/False from generated text. Returns None if invalid."""
    if re.search(r"\bTrue\b", text, re.IGNORECASE):
        return "True"
    if re.search(r"\bFalse\b", text, re.IGNORECASE):
        return "False"
    return None


# ═════════════════════════════════════════════════════════════════
# Turn 2 builder — clean token separation
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
# Training — supports Group A (trainable), B (hardcoded), C (none)
# ═════════════════════════════════════════════════════════════════

def train_self_core(model, tokenizer, self_core, samples, lam, epochs,
                    mode="trainable"):
    """mode: 'trainable' (A), 'hardcoded' (B), 'none' (C)."""

    # External persistent state
    self_state = torch.randn(SELF_DIM, device=DEVICE) * 0.02
    state_stack = deque([torch.zeros(SELF_DIM, device=DEVICE)
                         for _ in range(STATE_STACK)], maxlen=STATE_STACK)
    cal_est_hard = 0.5  # Only used for mode='hardcoded'

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
                                            do_sample=False, pad_token_id=tokenizer.pad_token_id)
                    gen_text = tokenizer.decode(gen[0], skip_special_tokens=True)
                    ans = extract_answer(gen_text)
                    if ans is None:  # invalid output — skip
                        continue
                    correct = float(ans == a)

                    # ── Self Core update ──
                    stacked = torch.cat(
                        [self_state] + list(state_stack)
                    ).unsqueeze(0)
                    corr_t = torch.tensor([[correct]], device=DEVICE)

                    if mode == "trainable":
                        updated = self_core.forward_gate(stacked, corr_t).squeeze(0)
                        cal_est = self_core.calibration(updated.unsqueeze(0))
                        sl = F.l1_loss(cal_est, corr_t.squeeze())
                        # State norm regularization
                        sl = sl + STATE_REG * updated.norm()
                        self_loss += sl

                        self_state = updated.detach()
                        state_stack.append(self_state.clone())

                        # Build Turn 2 with Self Core prefix
                        combined_emb, labels = build_turn2_inputs(
                            tokenizer, model, self_core, self_state,
                            gen_text, ans, a,
                        )

                    elif mode == "hardcoded":
                        # Hardcoded update: separate scalar. No state stack needed.
                        # self_state stays at zero — no self info in prefix.
                        cal_est_hard = cal_est_hard + 0.1 * (correct - cal_est_hard)
                        sl = F.l1_loss(torch.tensor([cal_est_hard], device=DEVICE),
                                       corr_t.squeeze())
                        self_loss += sl
                        # self_state doesn't update; no state stack needed
                        # prefix still uses self_state (zero vector — no self info)
                        combined_emb, labels = build_turn2_inputs(
                            tokenizer, model, self_core, self_state,
                            gen_text, ans, a,
                        )

                    else:  # mode == "none"
                        # No Self Core. Plain self-ref training (v3 style).
                        turn2_text = (
                            f"{gen_text}\n---\n"
                            f"You just answered: {ans}\n"
                            f"The correct answer is: {a}\n"
                        )
                        turn2_tok = tokenizer(turn2_text, return_tensors="pt", truncation=True,
                                             max_length=MAX_LENGTH).to(DEVICE)
                        # Mask everything before "---"
                        labels = turn2_tok.input_ids.clone()
                        turn2_start = turn2_text.find("---")
                        if turn2_start > 0:
                            before = tokenizer(turn2_text[:turn2_start], return_tensors="pt",
                                              truncation=True, max_length=MAX_LENGTH)
                            mask_len = min(before.input_ids.shape[1], labels.shape[1])
                            labels[0, :mask_len] = -100
                        out = model(**turn2_tok, labels=labels)
                        model_loss += out.loss
                        n_samples += 1
                        continue  # Skip the shared Turn 2 forward

                    # Shared Turn 2 forward (for trainable and hardcoded)
                    out = model(inputs_embeds=combined_emb, labels=labels)
                    model_loss += out.loss
                    n_samples += 1

            if n_samples == 0:
                continue

            scaled_model = model_loss / (n_samples * GRAD_ACCUM)
            scaled_self  = self_loss / (n_samples * GRAD_ACCUM) if self_loss > 0 else 0.0

            scaled_model.backward()
            if scaled_self > 0 and mode == "trainable":
                scaled_self.backward()

            # Gradient clipping
            torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
            if mode == "trainable" and self_opt:
                torch.nn.utils.clip_grad_norm_(self_core.parameters(), GRAD_CLIP)

            step += 1
            if step % GRAD_ACCUM == 0:
                model_opt.step()
                if self_opt: self_opt.step()
                model_opt.zero_grad()
                if self_opt: self_opt.zero_grad()

            total_loss += (model_loss.item() + (self_loss.item() if self_loss > 0 else 0)) / n_samples
            n_batches += 1

        # Step remaining
        if step % GRAD_ACCUM != 0:
            model_opt.step()
            if self_opt: self_opt.step()
            model_opt.zero_grad()
            if self_opt: self_opt.zero_grad()

        print(f"  Epoch {epoch+1}/{epochs}  loss={total_loss/max(n_batches,1):.4f}")


# ═════════════════════════════════════════════════════════════════
# Evaluation — clean ECE on binary task
# ═════════════════════════════════════════════════════════════════

@torch.no_grad()
def evaluate(model, tokenizer, test_pool):
    confs, corrects = [], []
    for q, a in test_pool:
        prompt = f"{q} Answer:"
        tok = tokenizer(prompt, return_tensors="pt", truncation=True,
                       max_length=MAX_LENGTH).to(DEVICE)

        # Confidence: P("True" | prompt) — single token, clean
        logits = model(**tok).logits[0, -1, :]
        probs = torch.softmax(logits, dim=-1)
        true_ids  = tokenizer.encode(" True", add_special_tokens=False)
        false_ids = tokenizer.encode(" False", add_special_tokens=False)
        if not true_ids or not false_ids:
            confs.append(0.5); corrects.append(pred == a); continue
        true_id, false_id = true_ids[0], false_ids[0]

        p_true  = float(probs[true_id].cpu())
        p_false = float(probs[false_id].cpu())

        # Predicted answer and confidence
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
# Run — Groups A, B, C at each λ
# ═════════════════════════════════════════════════════════════════

print(f"Loading {MODEL_NAME}…")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

results = {"A": [], "B": [], "C": []}

for mode, label in [("trainable", "A"), ("hardcoded", "B"), ("none", "C")]:
    print(f"\n{'#'*60}\nGroup {label}: mode={mode}\n{'#'*60}")
    ece_scores, acc_scores = [], []

    for lam in LAMBDA_VALUES:
        print(f"\n  λ = {lam:.2f}")
        m = AutoModelForCausalLM.from_pretrained(MODEL_NAME).to(DEVICE)
        sc = SelfCore().to(DEVICE) if mode != "none" else None
        train_self_core(m, tokenizer, sc, train_pool, lam, EPOCHS, mode=mode)
        ece, acc = evaluate(m, tokenizer, test_pool)
        ece_scores.append(ece); acc_scores.append(acc)
        print(f"    ECE={ece:.4f}  Acc={acc:.3f}")
        del m; torch.cuda.empty_cache()

    results[label] = (ece_scores, acc_scores)


# ═════════════════════════════════════════════════════════════════
# Plot — all three groups
# ═════════════════════════════════════════════════════════════════
import matplotlib.pyplot as plt

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6))

# ECE
for label, color in [("A", "#7C3AED"), ("B", "#F59E0B"), ("C", "#6B7280")]:
    ece, _ = results[label]
    ax1.plot(LAMBDA_VALUES, ece, "o-", color=color, lw=2, ms=6, label=f"Group {label}")
ax1.set_xlabel("λ"); ax1.set_ylabel("ECE"); ax1.set_title("ECE vs λ (3 groups)")
ax1.legend(); ax1.grid(True, alpha=0.2)

# Accuracy
for label, color in [("A", "#7C3AED"), ("B", "#F59E0B"), ("C", "#6B7280")]:
    _, acc = results[label]
    ax2.plot(LAMBDA_VALUES, acc, "s--", color=color, lw=1.5, ms=6, label=f"Group {label}")
ax2.set_xlabel("λ"); ax2.set_ylabel("Accuracy"); ax2.set_title("Accuracy vs λ (3 groups)")
ax2.legend(); ax2.grid(True, alpha=0.2)

plt.tight_layout(); plt.savefig("self_core_v4_three_groups.png", dpi=150); plt.show()

json.dump({"lambda": LAMBDA_VALUES, "results": {
    k: {"ece": v[0], "acc": v[1]} for k, v in results.items()
}, "model": MODEL_NAME, "samples": TOTAL_SAMPLES}, open("self_core_v4_results.json", "w"))

print("\nDone — v4 complete.")
