import os
import sys
import json
from jinja2 import Environment, FileSystemLoader
from agent_core import AgentState, parse_args

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

def main():
    args = parse_args()
    agent = AgentState(run_id=args.run_id if args else "latest")

    env = Environment(loader=FileSystemLoader('.'))

    candidates = []

    run_dir = agent.run_dir
    if not os.path.exists(run_dir):
        print("Run directory not found.")
        sys.exit(0)

    for item in sorted(os.listdir(run_dir)):
        p_dir = os.path.join(run_dir, item)
        if not os.path.isdir(p_dir):
            continue
        s_file = os.path.join(p_dir, "state.json")
        if not os.path.exists(s_file):
            continue

        with open(s_file, "r") as f:
            st = json.load(f)

        issues = []
        issues_file = st.get("issues_file")
        if issues_file and os.path.exists(issues_file):
            with open(issues_file, "r") as isf:
                issues = json.load(isf)

        candidates.append({
            "project_name": item,
            "target_branch": st.get("target_branch", ""),
            "target_commit": st.get("target_commit", "")[:8] if st.get("target_commit") else "",
            "review_status": st.get("status_p1_review", "unknown"),
            "fix_status": st.get("status_p2_fix", "pending"),
            "attempt_count": st.get("attempt_count", 0),
            "best_patch_path": st.get("best_patch_path", ""),
            "fix_note": st.get("fix_note", ""),
            "baseline_test_code": st.get("baseline_test_code"),
            "issue_count": len(issues),
            "issues": sorted(issues, key=lambda x: SEVERITY_ORDER.get(x.get("severity", "low"), 2)),
            "one_line_summary": st.get("one_line_summary", ""),
            "test_results": st.get("test_results", {}),
            "categorize": st.get("categorize", {}),
            "llm_review": st.get("llm_review", {}),
            "llm_parse_ok": st.get("llm_parse_ok", True),
            "llm_parse_error": st.get("llm_parse_error", ""),
            "error_message": st.get("error_message", ""),
        })

    lang = agent.config.get("language", "ko")
    model_name = agent.config.get("llm", {}).get("model_name", "Unknown")
    summary_template = env.get_template(f"templates/{lang}/summary.md.j2")
    final_summary = summary_template.render(
        fix_candidates=candidates,
        model_name=model_name,
    )

    summary_path = os.path.join(run_dir, "summary.md")
    with open(summary_path, "w") as f:
        f.write(final_summary)
    print(f"summary.md 생성 완료: {summary_path}")

    # GitHub 리포트 push
    run_states = {c["project_name"]: c for c in candidates}
    try:
        from github_reporter import push_reports
        push_reports(agent.config, run_states)
    except Exception as e:
        print(f"GitHub 리포팅 실패: {e}")

    print(f"리포트 경로: {os.path.abspath(summary_path)}")

if __name__ == "__main__":
    main()
