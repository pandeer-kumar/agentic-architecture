#!/usr/bin/env python3
"""Offline simulation of the map phase (docs/05 §6) with a fault-injecting stub model.

No API calls. It checks that the batch contract + validators V1-V5 + retry policy
guarantee 100 % coverage and exact counts even when the "model" misbehaves:
drops ids, duplicates ids, invents ids, fabricates evidence quotes, truncates
(max_tokens), and flips labels. Ground truth is known, so count accuracy of the
pipeline (as opposed to the stub's own accuracy) can be asserted exactly.

Stage 2 adds the recall net + second opinion + adjudication and asserts the residual
label error rate is <= 0.2 % (it is a property of the stubs' error rates, so this
checks the *mechanism*, not real model accuracy, which docs/11 measures).

Usage: python3 validation/simulate_map_phase.py [n_records] [seed]
"""
import random, sys, collections

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 7
BATCH = 25
MAX_RETRIES = 2
rng = random.Random(SEED)

LABELS = ("network_change", "not_network_change", "insufficient_information")

# ---- synthetic records with ground truth --------------------------------
records = {}
for i in range(1, N + 1):
    net = rng.random() < 0.35
    text = (f"Updated firewall rule on edge router RTR-{i}" if net else f"Patched application server APP-{i}")
    records[f"NET-{i}"] = {"text": text, "truth": "network_change" if net else "not_network_change"}

# ---- fault-injecting stub model ----------------------------------------
FAULTS = {"drop": 0.03, "dup": 0.02, "invent": 0.01, "fake_quote": 0.03, "truncate_batch": 0.02, "flip": 0.01}
fault_counts = collections.Counter()

def stub_model(batch):
    """batch: list of (rid, text). Returns list of result dicts and stop_reason."""
    if rng.random() < FAULTS["truncate_batch"]:
        fault_counts["truncate_batch"] += 1
        return [], "max_tokens"
    out = []
    for rid, text in batch:
        if rng.random() < FAULTS["drop"]:
            fault_counts["drop"] += 1
            continue
        is_net = "firewall" in text or "router" in text
        label = "network_change" if is_net else "not_network_change"
        if rng.random() < FAULTS["flip"]:
            fault_counts["flip"] += 1
            label = "not_network_change" if is_net else "network_change"
        quote = "firewall rule" if is_net else "application server"
        if label == "network_change" and not is_net:
            quote = "core switch replaced"            # a flipped positive cannot quote real evidence
        if rng.random() < FAULTS["fake_quote"]:
            fault_counts["fake_quote"] += 1
            quote = "BGP session reset on core"        # not present in the text
        out.append({"rid": rid, "label": label, "evidence_quote": quote, "confidence": "high"})
        if rng.random() < FAULTS["dup"]:
            fault_counts["dup"] += 1
            out.append({"rid": rid, "label": LABELS[rng.randrange(2)], "evidence_quote": quote, "confidence": "low"})
    if rng.random() < FAULTS["invent"]:
        fault_counts["invent"] += 1
        out.append({"rid": "R999", "label": "network_change", "evidence_quote": "x", "confidence": "high"})
    return out, "end_turn"

# ---- harness: batch contract + validators -------------------------------
def norm(s):
    return " ".join(s.lower().split())

final = {}            # source_id -> label
needs_review = set()
calls = 0
validator_hits = collections.Counter()

def run_batch(ids, attempt):
    """Run ids through the stub, validate, return ids that must be retried."""
    global calls
    calls += 1
    local = {f"R{k+1}": sid for k, sid in enumerate(ids)}          # short local ids (docs/05 §6.1)
    results, stop = stub_model([(r, records[s]["text"]) for r, s in local.items()])
    if stop != "end_turn":                                          # V4
        validator_hits["V4_stop_reason"] += 1
        return list(ids)
    seen = collections.Counter(r["rid"] for r in results)
    retry = set()
    for r in results:
        if r["rid"] not in local:                                   # V1 extra id
            validator_hits["V1_invented_id"] += 1
            continue
        sid = local[r["rid"]]
        if seen[r["rid"]] > 1:                                      # V1 duplicate
            validator_hits["V1_duplicate"] += 1
            retry.add(sid)
            continue
        if r["label"] != "insufficient_information" and norm(r["evidence_quote"]) not in norm(records[sid]["text"]):
            validator_hits["V2_ungrounded"] += 1                     # V2 grounding
            retry.add(sid)
            continue
        final[sid] = r["label"]
    missing = [local[r] for r in local if seen[r] == 0]              # V1 missing
    validator_hits["V1_missing"] += len(missing)
    retry.update(missing)
    return sorted(retry)

pending = list(records)
for attempt in range(0, MAX_RETRIES + 1):
    size = BATCH if attempt == 0 else max(1, BATCH // (2 ** attempt))   # smaller batches on retry
    nxt = []
    for k in range(0, len(pending), size):
        nxt += run_batch(pending[k:k + size], attempt)
    pending = [p for p in nxt if p not in final]
    if not pending:
        break
for sid in pending:                                                  # V5 retry cap
    needs_review.add(sid)

# ---- stage 2: recall net + second opinion + adjudication (docs/05 §6.3) ---
LEXICON = ("firewall", "router", "switch", "vlan", "bgp", "dns", "subnet", "vpn", "load balancer", "network")
def recall_net(text):
    t = text.lower()
    return any(w in t for w in LEXICON)

def second_opinion(text):                      # independent stub, own 1 % error rate
    is_net = "firewall" in text or "router" in text
    lab = "network_change" if is_net else "not_network_change"
    if rng.random() < 0.01:
        lab = "not_network_change" if is_net else "network_change"
    return lab

def adjudicate(text):                          # stronger model, 0.2 % error rate
    is_net = "firewall" in text or "router" in text
    lab = "network_change" if is_net else "not_network_change"
    if rng.random() < 0.002:
        lab = "not_network_change" if is_net else "network_change"
    return lab

before_stage2 = [s for s, l in final.items() if l != records[s]["truth"]]
checked = disagreements = 0
for sid, lab in list(final.items()):
    text = records[sid]["text"]
    selected = (lab == "not_network_change" and recall_net(text)) or rng.random() < 0.05
    if not selected:
        continue
    checked += 1
    so = second_opinion(text)
    if so != lab:
        disagreements += 1
        final[sid] = adjudicate(text)

# ---- assertions --------------------------------------------------------
labelled = set(final) | needs_review
assert labelled == set(records), "coverage must be 100 % (labelled or needs_human_review)"
assert not (set(final) & needs_review), "a record cannot be both final and needs_review"
assert len(final) + len(needs_review) == N, "exactly one outcome per record"

truth_pos = sum(1 for r in records.values() if r["truth"] == "network_change")
pipe_pos = sum(1 for l in final.values() if l == "network_change")
wrong = [s for s, l in final.items() if l != records[s]["truth"]]

print(f"records={N} batches_called={calls} faults_injected={dict(fault_counts)}")
print(f"validator_hits={dict(validator_hits)}")
print(f"final={len(final)} needs_human_review={len(needs_review)} coverage=100%")
print(f"label_errors_after_validators={len(before_stage2)} second_opinions={checked} disagreements={disagreements}")
print(f"truth_network={truth_pos} pipeline_network={pipe_pos} label_errors_after_stage2={len(wrong)}")
assert len(wrong) <= max(1, N // 500), "stage 2 must cut residual label errors to <= 0.2 %"
print("SIMULATION OK")
