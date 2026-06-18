"""
multi_agent_launcher.py — Spawn multiple AI agents sharing one world
=====================================================================
Phase W2: Starts the physics server, then launches N agent processes
with different profiles. Each agent has its own farm and independent
decision-making, but shares the same world economy and can interact.

Usage:
    python multi_agent_launcher.py                  # all 3 agents
    python multi_agent_launcher.py --agents xu_renwu,old_wang  # specific
"""
import subprocess, sys, time, os, json, threading
from datetime import datetime

PY = r"C:\Users\m1916\AppData\Local\Programs\Python\Python313\python.exe"
LOCAL = r"C:\Users\m1916\agent-brain"
AGENT_SCRIPT = "agent-world-llm.py"

# Default agents to launch (all 3 profiles)
DEFAULT_AGENTS = [
    {"profile": "xu_renwu",  "color": "92", "icon": "🌾"},  # green
    {"profile": "old_wang",  "color": "94", "icon": "🐄"},  # blue
    {"profile": "iron_lady", "color": "91", "icon": "🔨"},  # red
]


def colored_print(color_code, prefix, text):
    """Print with ANSI color prefix for log differentiation."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"\033[{color_code}m[{prefix} {timestamp}]\033[0m {text.rstrip()}")


def stream_agent_output(proc, profile_name, color, icon, log_file):
    """Stream one agent's stdout to both the terminal (colored) and a log file."""
    prefix = f"{icon}{profile_name[:8]}"
    try:
        with open(log_file, "a", encoding="utf-8") as lf:
            for line in proc.stdout:
                if line.strip():
                    colored_print(color, prefix, line.rstrip())
                    lf.write(f"[{datetime.now().isoformat()}] {line}")
                    lf.flush()
    except Exception:
        pass  # process died


def launch_agent(profile_name, color, icon):
    """Launch one agent process. Returns (process, thread)."""
    log_path = os.path.join(LOCAL, "agents", profile_name, f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    env = os.environ.copy()
    env["DEEPSEEK_KEY"] = env.get("DEEPSEEK_KEY", "")

    proc = subprocess.Popen(
        [PY, "-u", os.path.join(LOCAL, AGENT_SCRIPT), "--profile", profile_name],
        cwd=LOCAL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    thread = threading.Thread(
        target=stream_agent_output,
        args=(proc, profile_name, color, icon, log_path),
        daemon=True,
    )
    thread.start()

    return proc, thread, log_path


def main():
    print("\033[1m" + "=" * 60 + "\033[0m")
    print("\033[1mAGENT WORLD — Multi-Agent Farm Launcher (Phase W2)\033[0m")
    print("\033[1m" + "=" * 60 + "\033[0m")

    # Parse --agents flag
    agents_to_launch = DEFAULT_AGENTS
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--agents" and i < len(sys.argv) - 2:
            names = sys.argv[i + 2].split(",")
            agents_to_launch = [a for a in DEFAULT_AGENTS if a["profile"] in names]
        elif arg.startswith("--agents="):
            names = arg.split("=", 1)[1].split(",")
            agents_to_launch = [a for a in DEFAULT_AGENTS if a["profile"] in names]

    if not agents_to_launch:
        agents_to_launch = DEFAULT_AGENTS

    print(f"Launching {len(agents_to_launch)} agent(s): "
          f"{', '.join(a['profile'] for a in agents_to_launch)}")
    print()

    # 1. Start physics server
    print("Starting Agent World server...")
    server = subprocess.Popen(
        [PY, "-u", os.path.join(LOCAL, "agent_world_local.py")],
        cwd=LOCAL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Wait for server ready
    for line in server.stdout:
        if "All 3 sites running" in line:
            break

    time.sleep(1.5)
    print("Server ready. Starting agents...\n")

    # 2. Launch all agents
    processes = []
    log_paths = []
    for agent_cfg in agents_to_launch:
        proc, thread, logp = launch_agent(
            agent_cfg["profile"], agent_cfg["color"], agent_cfg["icon"]
        )
        processes.append((proc, agent_cfg))
        log_paths.append(logp)
        time.sleep(1.5)  # stagger starts to avoid registration races

    print(f"\n\033[1mAll {len(processes)} agents running. Logs:\033[0m")
    for lp, (_, cfg) in zip(log_paths, processes):
        print(f"  {cfg['icon']} {cfg['profile']:12s} → {lp}")
    print("\nPress Ctrl+C to stop all agents.\n")

    # 3. Monitor: wait for any agent to die, or Ctrl+C
    try:
        while True:
            all_alive = True
            for proc, cfg in processes:
                if proc.poll() is not None:
                    colored_print("93", "⚠", f"{cfg['profile']} exited (code {proc.returncode})")
                    all_alive = False
            if not all_alive:
                print("\nOne or more agents have stopped. Shutting down...")
                break
            time.sleep(3)
    except KeyboardInterrupt:
        print("\n\nShutting down by user request...")

    # 4. Cleanup
    for proc, cfg in processes:
        if proc.poll() is None:
            proc.terminate()
            colored_print("90", "✕", f"Terminated {cfg['profile']}")
    server.terminate()
    time.sleep(1)
    server.kill()  # force kill if still alive
    print("All processes stopped. Goodbye.")


if __name__ == "__main__":
    main()
