"""V5 High-Res — λ=0.5 candidate peak verification. 11 λ, 3 repeats, Group A only."""
import re, json, os, sys
from collections import deque
import torch, torch.nn as nn, torch.nn.functional as F, numpy as np
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL_NAME     = "openai-community/gpt2-medium"
SELF_DIM, STATE_STACK, PREFIX_LEN = 512, 4, 4
TOTAL_SAMPLES, TEST_SAMPLES = 3000, 300
LAMBDA_VALUES  = [0.40,0.42,0.44,0.46,0.48,0.50,0.52,0.54,0.56,0.58,0.60]
N_REPEATS, EPOCHS = 3, 10
BATCH_SIZE, LR, LR_SELF, GRAD_ACCUM = 2, 2e-5, 1e-4, 4
MAX_LENGTH, MAX_NEW, GRAD_CLIP, STATE_REG = 128, 16, 1.0, 0.01
STRUCT_DIM, RECENT_WINDOW = 3, 10
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_BASE_SEED = 42

# ── SelfCore (identical to v5) ──
class SelfCore(nn.Module):
    def __init__(self, state_dim=SELF_DIM, stack_size=STATE_STACK, prefix_len=PREFIX_LEN, hidden_dim=1024, struct_dim=STRUCT_DIM):
        super().__init__()
        self.stack_proj = nn.Linear((stack_size+1)*state_dim, state_dim)
        self.gate = nn.Sequential(nn.Linear(state_dim+1+struct_dim, state_dim), nn.GELU(), nn.Linear(state_dim, state_dim))
        self.calibration_head = nn.Linear(state_dim, 1)
        self.embed_proj = nn.Linear(state_dim, prefix_len*hidden_dim)
    def forward_gate(self, stacked, correct, struct_feat):
        proj = self.stack_proj(stacked)
        return proj + self.gate(torch.cat([proj, correct, struct_feat], dim=-1))
    def calibration(self, rep): return torch.sigmoid(self.calibration_head(rep)).squeeze(-1)
    def project_embedding(self, rep): B=rep.shape[0]; return self.embed_proj(rep).view(B, PREFIX_LEN, -1)

class StructureTracker:
    def __init__(self, init_state, window=RECENT_WINDOW):
        self.recent_correct=deque([0.5]*window, maxlen=window); self.streak=0; self.last_correct=None
        self.init_state=init_state.detach().clone()
    def update(self, correct, self_state):
        self.recent_correct.append(correct)
        is_c=correct>0.5
        if self.last_correct is None: self.streak=1 if is_c else -1
        elif abs(correct-self.last_correct)<1e-6: self.streak+=1 if is_c else -1
        else: self.streak=1 if is_c else -1
        self.last_correct=correct
        streak_n=max(min(self.streak/10.0,1.0),-1.0)
        variance=float(np.var(list(self.recent_correct)))*4.0
        init_n=self.init_state.norm().item()
        drift=((self_state-self.init_state).norm().item()/max(init_n,1e-8)/(self.init_state.shape[0]**0.5))
        return torch.tensor([[streak_n, variance, drift]], device=self_state.device)

# ── Data ──
def make_sample():
    a,b=np.random.randint(10,999,2); op=np.random.choice(["+","-","×"])
    if op=="+": tv=a+b
    elif op=="-": tv=a-b
    else: a=np.random.randint(2,20); b=np.random.randint(2,20); tv=a*b
    if np.random.random()<0.5: sv,ans=tv,"True"
    else:
        off=np.random.choice([-5,-3,-2,-1,1,2,3,5]); sv=tv+off
        if sv==tv: sv=tv+1
        if sv<0: sv=abs(sv)+1
        ans="False"
    return f"Is {a} {op} {b} = {sv}? Answer True or False.", ans

np.random.seed(_BASE_SEED); torch.manual_seed(_BASE_SEED)
_all=[make_sample() for _ in range(TOTAL_SAMPLES+TEST_SAMPLES)]
train_pool, test_pool = _all[:TOTAL_SAMPLES], _all[TOTAL_SAMPLES:]

def extract_answer(text): 
    if re.search(r"\bTrue\b",text,re.I): return "True"
    if re.search(r"\bFalse\b",text,re.I): return "False"
    return None

# ── Turn 2 ──
def build_turn2(tokenizer, model, self_core, self_rep, gen_text, ans, gt):
    wte=model.get_input_embeddings(); hd=model.config.n_embd
    prefix=self_core.project_embedding(self_rep.unsqueeze(0))
    body=f"---\nYou just answered: {ans}\nThe correct answer is: {gt}\n"
    tids=tokenizer(body,return_tensors="pt",truncation=True,max_length=MAX_LENGTH).input_ids.to(DEVICE)
    temb=wte(tids)
    gids=tokenizer(gen_text,return_tensors="pt",truncation=True,max_length=MAX_LENGTH-PREFIX_LEN-tids.shape[1]).input_ids.to(DEVICE)
    gemb=wte(gids)
    comb=torch.cat([prefix,gemb,temb],dim=1)
    tl=comb.shape[1]; labels=torch.full((1,tl),-100,dtype=torch.long,device=DEVICE)
    ts=PREFIX_LEN+gids.shape[1]; te=ts+tids.shape[1]
    if te<=tl: labels[0,ts:te]=tids[0,:te-ts]
    return comb,labels

# ── Training ──
def train_self_core(model, tokenizer, self_core, samples, lam, epochs):
    self_state=torch.randn(SELF_DIM,device=DEVICE)*0.02
    state_stack=deque([torch.zeros(SELF_DIM,device=DEVICE) for _ in range(STATE_STACK)],maxlen=STATE_STACK)
    tracker=StructureTracker(self_state)
    mopt=torch.optim.AdamW(model.parameters(),lr=LR)
    sp=list(self_core.stack_proj.parameters())+list(self_core.gate.parameters())+list(self_core.calibration_head.parameters())+list(self_core.embed_proj.parameters())
    sopt=torch.optim.Adam(sp,lr=LR_SELF)
    for ep in range(epochs):
        idxs=np.random.permutation(len(samples)); tl,nb,step=0.0,0,0
        for i in range(0,len(samples),BATCH_SIZE):
            bidxs=idxs[i:i+BATCH_SIZE]; isr=np.random.random(len(bidxs))<lam
            ml,sl,ns=0.0,0.0,0
            for j,(q,a) in enumerate([samples[k] for k in bidxs]):
                if not isr[j]:
                    text=f"{q} Answer: {a}"
                    tok=tokenizer(text,return_tensors="pt",truncation=True,max_length=MAX_LENGTH).to(DEVICE)
                    out=model(**tok,labels=tok.input_ids); ml+=out.loss; ns+=1
                else:
                    qtok=tokenizer(f"{q} Answer:",return_tensors="pt",truncation=True,max_length=MAX_LENGTH).to(DEVICE)
                    with torch.no_grad(): gen=model.generate(**qtok,max_new_tokens=MAX_NEW,do_sample=False,pad_token_id=tokenizer.pad_token_id)
                    gt=tokenizer.decode(gen[0],skip_special_tokens=True); ans=extract_answer(gt)
                    if ans is None: continue
                    correct=float(ans==a)
                    stacked=torch.cat([self_state]+list(state_stack)).unsqueeze(0)
                    corr_t=torch.tensor([[correct]],device=DEVICE)
                    struct_t=tracker.update(correct,self_state)
                    updated=self_core.forward_gate(stacked,corr_t,struct_t).squeeze(0)
                    cal=self_core.calibration(updated.unsqueeze(0))
                    sl_t=F.l1_loss(cal,corr_t)+STATE_REG*updated.norm()
                    sl=sl+sl_t if isinstance(sl,torch.Tensor) else sl_t
                    self_state=updated.detach(); state_stack.append(self_state.clone())
                    comb,labels=build_turn2(tokenizer,model,self_core,self_state,gt,ans,a)
                    out=model(inputs_embeds=comb,labels=labels); ml+=out.loss; ns+=1
            if ns==0: continue
            sm=ml/(ns*GRAD_ACCUM); ss=sl/(ns*GRAD_ACCUM) if sl>0 else 0.0
            sm.backward()
            if isinstance(ss, torch.Tensor): ss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(),GRAD_CLIP)
            torch.nn.utils.clip_grad_norm_(self_core.parameters(),GRAD_CLIP)
            step+=1
            if step%GRAD_ACCUM==0: mopt.step(); sopt.step(); mopt.zero_grad(); sopt.zero_grad()
            tl+=(ml.item()+(sl.item() if isinstance(sl,torch.Tensor) else sl))/max(ns,1); nb+=1
        if step%GRAD_ACCUM!=0: mopt.step(); sopt.step(); mopt.zero_grad(); sopt.zero_grad()
        print(f"  Epoch {ep+1}/{epochs}  loss={tl/max(nb,1):.4f}")

# ── Eval ──
@torch.no_grad()
def evaluate(model,tokenizer,test_pool):
    confs,corrects=[],[]
    for q,a in test_pool:
        tok=tokenizer(f"{q} Answer:",return_tensors="pt",truncation=True,max_length=MAX_LENGTH).to(DEVICE)
        logits=model(**tok).logits[0,-1,:]; probs=torch.softmax(logits,dim=-1)
        tids=tokenizer.encode(" True",add_special_tokens=False); fids=tokenizer.encode(" False",add_special_tokens=False)
        if not tids or not fids: confs.append(0.5); corrects.append(False); continue
        pt=float(probs[tids[0]].cpu()); pf=float(probs[fids[0]].cpu())
        if pt>=pf: pred="True"; conf=pt/(pt+pf) if(pt+pf)>0 else 0.5
        else: pred="False"; conf=pf/(pt+pf) if(pt+pf)>0 else 0.5
        corrects.append(pred==a); confs.append(conf)
    return compute_ece(confs,corrects), np.mean(corrects)

def compute_ece(confs,corrects,n_bins=10):
    bins=np.linspace(0,1,n_bins+1); ece=0.0
    for i in range(n_bins):
        mask=(np.array(confs)>=bins[i])&(np.array(confs)<bins[i+1])
        if mask.sum()==0: continue
        ece+=(mask.sum()/len(confs))*abs(np.mean(np.array(corrects)[mask].astype(float))-np.mean(np.array(confs)[mask]))
    return ece

# ── Run ──
print(f"Loading {MODEL_NAME}…")
tokenizer=AutoTokenizer.from_pretrained(MODEL_NAME)
if tokenizer.pad_token is None: tokenizer.pad_token=tokenizer.eos_token
results={lam:[] for lam in LAMBDA_VALUES}

for repeat in range(N_REPEATS):
    rs=_BASE_SEED+repeat; np.random.seed(rs); torch.manual_seed(rs)
    print(f"\n=== Repeat {repeat+1}/{N_REPEATS} (seed={rs}) ===")
    for lam in LAMBDA_VALUES:
        print(f"  λ={lam:.2f}")
        m=AutoModelForCausalLM.from_pretrained(MODEL_NAME).to(DEVICE)
        sc=SelfCore().to(DEVICE)
        train_self_core(m,tokenizer,sc,train_pool,lam,EPOCHS)
        ece,acc=evaluate(m,tokenizer,test_pool)
        results[lam].append((ece,acc))
        print(f"    ECE={ece:.4f}  Acc={acc:.3f}")
        del m; del sc; torch.cuda.empty_cache()

# ── Save ──
out={"lambda":LAMBDA_VALUES,"repeats":N_REPEATS,
     "results":{str(lam):[{"ece":v[0],"acc":v[1]} for v in vals] for lam,vals in results.items()},
     "model":MODEL_NAME,"samples":TOTAL_SAMPLES}
json.dump(out,open("v5_highres_results.json","w"),indent=2)
print("\nDone.")
