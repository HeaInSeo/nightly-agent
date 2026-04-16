import os
import sys
import json
import datetime
import requests
from jinja2 import Environment, FileSystemLoader
from agent_core import AgentState, parse_args

def send_discord(webhook_url, content):
    # Discord max message length is 2000 chars
    if len(content) > 2000:
        content = content[:1997] + "..."
    try:
        response = requests.post(webhook_url, json={"content": content}, timeout=10)
        response.raise_for_status()
        print("Discord 전송 완료.")
    except Exception as e:
        print(f"Discord 전송 실패: {e}")

def build_discord_message(candidates, model_name, run_id):
    lines = [
        f"# Nightly Agent 리포트 - {run_id}",
        f"**모델**: `{model_name}`",
        ""
    ]
    if candidates:
        for c in candidates:
            lines.append(f"## {c['project_name']}")
            lines.append(f"- **리뷰 상태**: {c['review_status']}")
            issue_count = c.get('issue_count', 0)
            top_issues = c.get('top_issues', [])
            if top_issues:
                severity_str = ", ".join(i.get('severity', '') for i in top_issues)
                lines.append(f"- **발견 이슈**: {issue_count}건 ({severity_str})")
            else:
                lines.append(f"- **발견 이슈**: {issue_count}건")
            lines.append(f"- **패치 검증**: {c['status']} ({c['reason']})")
            lines.append("")
    else:
        lines.append("- 분석된 프로젝트가 없습니다.")
    return "\n".join(lines)

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

        first_issue_title = issues[0].get("title", "Unknown") if issues else "Unknown"

        candidates.append({
            "project_name": item,
            "target_issue": f"[{item}] {first_issue_title}",
            "review_status": st.get("status_p1_review", "unknown"),
            "issue_count": len(issues),
            "top_issues": issues[:3],
            "status": st.get("status_p2_fix", "unknown"),
            "reason": st.get("error_message", "패치 검증 성공" if st.get("status_p2_fix") == "success" else "실패"),
            "path": st.get("best_patch_path", "N/A")
        })

    lang = agent.config.get("language", "ko")
    summary_template = env.get_template(f"templates/{lang}/summary.md.j2")
    final_summary = summary_template.render(
        fix_candidates=candidates,
        model_name=agent.config.get("model_name", "Unknown")
    )

    summary_path = os.path.join(run_dir, "summary.md")
    with open(summary_path, "w") as f:
        f.write(final_summary)
    print(f"summary.md 생성 완료: {summary_path}")

    # Discord 전송
    webhook_url = agent.config.get("discord_webhook_url", "").strip()
    if webhook_url:
        discord_msg = build_discord_message(candidates, agent.config.get("model_name", "Unknown"), agent.run_id)
        send_discord(webhook_url, discord_msg)
    else:
        print("Discord webhook URL이 설정되지 않았습니다. (config.json의 discord_webhook_url)")
        print("\n[Discord 전송 미리보기]")
        print(build_discord_message(candidates, agent.config.get("model_name", "Unknown"), agent.run_id))

if __name__ == "__main__":
    main()
