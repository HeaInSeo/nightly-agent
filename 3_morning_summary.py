import os
import sys
import json
from jinja2 import Environment, FileSystemLoader
from agent_core import AgentState, parse_args

def main():
    args = parse_args()
    # If project is passed, summarize single. If not, summarize all in run_id
    agent = AgentState(run_id=args.run_id if args else "latest")
    
    env = Environment(loader=FileSystemLoader('.'))
    
    candidates = []
    
    # Iterate all projects in the run dir
    run_dir = agent.run_dir
    if not os.path.exists(run_dir):
        print("Run directory not found.")
        sys.exit(0)
        
    for item in os.listdir(run_dir):
        p_dir = os.path.join(run_dir, item)
        if os.path.isdir(p_dir):
            s_file = os.path.join(p_dir, "state.json")
            if os.path.exists(s_file):
                with open(s_file, "r") as f:
                    st = json.load(f)
                    
                issues_file = st.get("issues_file")
                first_issue_title = "Unknown"
                if issues_file and os.path.exists(issues_file):
                    with open(issues_file, "r") as isf:
                        iv = json.load(isf)
                        if iv and isinstance(iv, list):
                            first_issue_title = iv[0].get("title", first_issue_title)
                            
                candidates.append({
                    "target_issue": f"[{item}] {first_issue_title}",
                    "status": st.get("status_p2_fix"),
                    "reason": st.get("error_message", "Patched successfully" if st.get("status_p2_fix") == "success" else "Failed"),
                    "path": st.get("best_patch_path", "N/A")
                })
                
    lang = agent.config.get("language", "ko")
    summary_template = env.get_template(f"templates/{lang}/summary.md.j2")
    final_summary = summary_template.render(
        fix_candidates=candidates,
        model_name=agent.config.get("model_name", "Unknown")
    )
    
    with open(os.path.join(run_dir, "summary.md"), "w") as f:
        f.write(final_summary)

if __name__ == "__main__":
    main()
