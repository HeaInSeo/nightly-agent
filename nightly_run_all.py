import os
import sys
import yaml
from agent_core import AgentState, run_cmd

def main():
    if not os.path.exists("configs/projects.yaml"):
        print("configs/projects.yaml missing. Exiting.")
        return

    with open("configs/projects.yaml", "r") as f:
        registry = yaml.safe_load(f)

    projects = registry.get("projects", [])
    if not projects:
        print("No projects registered. Exiting.")
        return

    agent = AgentState()
    run_id = agent.run_id # Generated once for the whole loop 

    print(f"🚀 Starting Nightly Run: {run_id}")

    for idx, proj in enumerate(projects):
        pname = proj.get("name")
        print(f"\n--- [{idx+1}/{len(projects)}] Processing Project: {pname} ---")
        
        # 1. Review Phase
        print(f"[{pname}] Running Phase 1 (Review)...")
        run_cmd(f"python3 1_nightly_review.py --project {pname} --run-id {run_id}")
        
        # 2. Fix Phase
        print(f"[{pname}] Running Phase 2 (Fix)...")
        run_cmd(f"python3 2_nightly_fix_candidate.py --project {pname} --run-id {run_id}")

    # 3. Summary Phase
    print(f"\n--- Generating Morning Summary ---")
    run_cmd(f"python3 3_morning_summary.py --run-id {run_id}")
    
    print("✅ All projects processed.")

if __name__ == "__main__":
    main()
