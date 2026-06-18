"""Start server + run LLM agent with Phase D1 features."""
import subprocess, sys, time, os

PY = r"C:\Users\m1916\AppData\Local\Programs\Python\Python313\python.exe"
LOCAL = r"C:\Users\m1916\agent-brain"

server = subprocess.Popen([PY, "-u", os.path.join(LOCAL, "agent_world_local.py")],
    cwd=LOCAL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

for line in server.stdout:
    if "All 3 sites running" in line:
        break

time.sleep(1)
print("Server ready. Starting LLM agent...")

brain = subprocess.Popen([PY, "-u", os.path.join(LOCAL, "agent-world-llm.py")],
    cwd=LOCAL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

for line in brain.stdout:
    print(line, end='')

brain.wait()
server.kill()
server.wait()
