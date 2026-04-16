import os
import sys
import argparse
import yaml
from agent_core import AgentState, run_cmd

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="특정 프로젝트만 실행 (미지정 시 전체 실행)", required=False)
    parser.add_argument("--run-id", help="run_id 직접 지정", required=False)
    return parser.parse_args()

def main():
    args = parse_args()

    if not os.path.exists("configs/projects.yaml"):
        print("configs/projects.yaml missing. Exiting.")
        return

    with open("configs/projects.yaml", "r") as f:
        registry = yaml.safe_load(f)

    projects = registry.get("projects", [])
    if not projects:
        print("No projects registered. Exiting.")
        return

    # --project 옵션으로 특정 프로젝트만 필터링
    if args.project:
        projects = [p for p in projects if p.get("name") == args.project]
        if not projects:
            print(f"Project '{args.project}' not found in configs/projects.yaml.")
            return

    agent = AgentState(run_id=args.run_id)
    run_id = agent.run_id

    print(f"Starting Nightly Run: {run_id}")
    if args.project:
        print(f"(단일 프로젝트 모드: {args.project})")

    for idx, proj in enumerate(projects):
        pname = proj.get("name")
        print(f"\n--- [{idx+1}/{len(projects)}] Processing Project: {pname} ---")

        print(f"[{pname}] Running Phase 1 (Review)...")
        run_cmd(f"python3 1_nightly_review.py --project {pname} --run-id {run_id}")

        print(f"[{pname}] Running Phase 2 (Fix)...")
        run_cmd(f"python3 2_nightly_fix_candidate.py --project {pname} --run-id {run_id}")

    print(f"\n--- Generating Morning Summary ---")
    run_cmd(f"python3 3_morning_summary.py --run-id {run_id}")

    print("All projects processed.")

if __name__ == "__main__":
    main()
