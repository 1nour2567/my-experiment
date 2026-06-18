import os, re
VAULT = r"C:\Users\m1916\agent-brain"

# Check a few decision files
for fn in ["2026-06-15-50.md", "2026-06-15-25.md", "2026-06-15-1.md"]:
    with open(os.path.join(VAULT, "decisions", fn), "r", encoding="utf-8") as f:
        content = f.read()
    links = re.findall(r"\[\[([^\]|#]+)(?:[|#][^\]]+)?\]\]", content)
    print(f"\n{fn}: {len(links)} links -> {links}")

    src_dir = "decisions"
    for target in links:
        tc = target.strip()
        resolved = os.path.normpath(os.path.join(src_dir, tc)).replace("\\", "/")
        full = os.path.join(VAULT, resolved)
        exists = os.path.exists(full)
        print(f"  {tc} -> {resolved} ({'OK' if exists else 'MISSING'})")

# Now build a mini graph to check node/edge counts
all_files = {}
for root, dirs, files in os.walk(VAULT):
    for fn in files:
        if fn.endswith(".md"):
            rel = os.path.relpath(os.path.join(root, fn), VAULT).replace("\\", "/")
            all_files[rel] = True
print(f"\nTotal vault files: {len(all_files)}")
print(f"Sample: {list(all_files.keys())[:5]}")

# Check if the graph builder's logic matches
edges = []
for root, dirs, files in os.walk(VAULT):
    for fn in files:
        if not fn.endswith(".md"):
            continue
        full = os.path.join(root, fn)
        rel = os.path.relpath(full, VAULT).replace("\\", "/")
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
        links = re.findall(r"\[\[([^\]|#]+)(?:[|#][^\]]+)?\]\]", content)
        src_dir = os.path.dirname(rel)
        for target in links:
            tc = target.strip()
            resolved = os.path.normpath(os.path.join(src_dir, tc)).replace("\\", "/")
            if resolved in all_files:
                edges.append((rel, resolved))
            elif resolved + ".md" in all_files:
                edges.append((rel, resolved + ".md"))

print(f"\nNodes: {len(all_files)}, Edges: {len(edges)}")
for e in edges[:5]:
    print(f"  {e[0]} -> {e[1]}")
