import os
import sys
from agent_core import AgentState

def main():
    agent = AgentState("latest")
    if not agent.acquire_lock():
        sys.exit(0)

    try:
        state = agent.load_state()
        
        if state.get("status_p3_summary") == "success":
            return
            
        summary_lines = []
        summary_lines.append(f"# Morning Agent Summary")
        summary_lines.append(f"**Run ID**: {agent.run_id}")
        summary_lines.append(f"**Base Branch**: {state.get('base_branch', 'Unknown')} (Merge Base: {state.get('merge_base', 'Unknown')[:7]})")
        summary_lines.append(f"**Target Commit**: {state.get('target_commit', 'Unknown')[:7]}")
        summary_lines.append(f"**Branch**: {state.get('target_branch', 'Unknown')}\n")
        
        summary_lines.append(f"## Phase 1: Review Status [{state.get('status_p1_review', 'unknown')}]")
        if state.get("status_p1_review") == "success":
            summary_lines.append(f"- Issues Found: {state.get('issues_count', 0)}")
            summary_lines.append(f"- Report Path: {state.get('review_report_path', 'N/A')}\n")
        else:
            summary_lines.append(f"- Details: {state.get('error_message', 'No details available.')}\n")
            
        summary_lines.append(f"## Phase 2: Fix Candidate Status [{state.get('status_p2_fix', 'unknown')}]")
        if state.get("status_p2_fix") == "success":
            summary_lines.append(f"- A valid patch has been generated on attempt {state.get('attempt_count', 1)}.")
            summary_lines.append(f"- Check best patch safely at: {state.get('best_patch_path', 'N/A')}\n")
        elif state.get("status_p2_fix") == "max_retries_reached":
            summary_lines.append(f"- Agent could not fix the issues within the retry limit ({state.get('attempt_count', 3)}).\n")
        else:
            summary_lines.append(f"- Details: {state.get('error_message', 'No patch generated or fix skipped.')}\n")
            
        summary_path = os.path.join(agent.get_run_dir(), "summary.md")
        with open(summary_path, "w") as f:
            f.write("\n".join(summary_lines))
            
        state["status_p3_summary"] = "success"
        agent.save_state(state)
        
    except Exception as e:
        state["status_p3_summary"] = "failed"
        state["error_message"] = str(e)
        agent.save_state(state)
    finally:
        agent.release_lock()

if __name__ == "__main__":
    main()
