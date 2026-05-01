"""Phase 0.5 — 이슈 연속성 체크

매 실행 전 issues_db의 open 이슈를 확인한다.
  1. git pickaxe로 anchor 스니펫이 마지막 확인 이후 변경됐는지 감지
  2. 변경 없음 → recurring 확정 (LLM 불필요)
  3. 변경 있음 → LLM 재판단 (git 증거 + LLM 둘 다 충족해야 resolved)
  4. LLM이 잘못된 픽스로 판단 → 파생 이슈 생성
  5. 최신 리뷰에서 더 이상 재현되지 않는 과거 오탐은 false_positive로 정리
"""
import os
import sys
import json
import datetime
import re
from agent_core import (
    AgentState, run_git, ask_llm, parse_args,
    load_issues_db, save_issues_db, make_issue_record,
)

SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def canonical_issue_key(issue):
    anchor = issue.get("anchor", {}) or {}
    file_key = anchor.get("file", "") or ",".join(issue.get("target_files", []))
    func_key = anchor.get("function", "")
    title_key = normalize_text(issue.get("title", ""))
    return f"{file_key}|{func_key}|{title_key}"


def dedupe_issues(issues):
    resolved = []
    unresolved_by_key = {}

    def sort_key(item):
        return (
            item.get("first_seen_date", ""),
            item.get("first_seen_run", ""),
            item.get("id", ""),
        )

    for issue in sorted(issues, key=sort_key):
        if issue.get("status") in ("resolved", "false_positive"):
            resolved.append(issue)
            continue
        key = canonical_issue_key(issue)
        unresolved_by_key.setdefault(key, issue)

    return resolved + list(unresolved_by_key.values())


def migrate_false_positive_statuses(issues):
    migrated = 0
    for issue in issues:
        if issue.get("status") != "resolved":
            continue
        reason = issue.get("resolved_reason", "")
        if reason != "Not reproduced in latest review after prompt/filter cleanup.":
            continue
        issue["status"] = "false_positive"
        issue["false_positive_date"] = issue.get("resolved_date") or issue.get("last_seen_date")
        issue["false_positive_reason"] = reason
        issue.pop("resolved_date", None)
        issue.pop("resolved_reason", None)
        migrated += 1
    return migrated


def pickaxe_changed(snippet, filepath, project_path, since_date):
    """git log -S로 snippet이 since_date 이후 변경됐는지 확인한다."""
    if not snippet or not filepath:
        return False
    since = f"--since={since_date}"
    args = ["log", "-S", snippet, since, "--oneline", "--", filepath]
    out, _, code = run_git(args, cwd=project_path)
    return bool(out.strip())


def get_current_function_code(filepath, function_name, project_path):
    """현재 HEAD에서 해당 파일의 function_name 주변 코드를 추출한다."""
    abs_path = os.path.join(project_path, filepath)
    if not os.path.exists(abs_path):
        return ""
    try:
        with open(abs_path, "r", errors="ignore") as f:
            lines = f.readlines()
        result = []
        in_func = False
        for line in lines:
            if function_name and function_name in line:
                in_func = True
            if in_func:
                result.append(line)
                if len(result) > 40:
                    break
        return "".join(result[:40])
    except Exception:
        return ""


def llm_recheck(issue, current_code, config):
    """LLM에게 이슈가 해결됐는지 재판단을 요청한다.
    반환: 'resolved' | 'recurring' | 'wrong_fix'
    """
    prompt = (
        f"You are a senior code reviewer. A previously flagged issue may have been fixed.\n\n"
        f"Original issue:\n"
        f"  Title: {issue['title']}\n"
        f"  What was wrong: {issue.get('what_is_wrong', '')}\n"
        f"  Why dangerous: {issue.get('why_dangerous', '')}\n"
        f"  Original problematic snippet:\n{issue.get('anchor', {}).get('snippet', '')}\n\n"
        f"Current code in that area:\n{current_code}\n\n"
        f"Determine ONE of:\n"
        f"  - 'resolved': issue is fully and correctly fixed\n"
        f"  - 'wrong_fix': an attempt was made but introduced new problems\n"
        f"  - 'recurring': issue still exists (code not meaningfully changed)\n\n"
        f"Output ONLY valid JSON: {{\"verdict\": \"resolved|wrong_fix|recurring\", \"reason\": \"\", "
        f"\"new_issue_title\": \"\", \"new_issue_what_is_wrong\": \"\", "
        f"\"new_issue_why_dangerous\": \"\", \"new_issue_suggested_action\": \"\", "
        f"\"new_issue_snippet\": \"\"}}"
    )
    raw = ask_llm(
        prompt,
        config,
        max_tokens=config.get("llm", {}).get("continuity_max_tokens", 600),
    )
    try:
        data = json.loads(raw.replace("```json", "").replace("```", "").strip())
        return data
    except Exception:
        return {"verdict": "recurring", "reason": "LLM 파싱 실패"}


def run_continuity_check(project_name, run_id, config):
    """issues_db의 open 이슈를 검증하고 상태를 갱신한다.
    파생 이슈가 생기면 issues_db에 추가하고 반환한다."""
    agent = AgentState(project_name=project_name, run_id=run_id)
    project_path = agent.project_context["path"]
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    issues = load_issues_db(project_name)
    if not issues:
        return []
    migrated = migrate_false_positive_statuses(issues)
    if migrated:
        save_issues_db(project_name, issues)

    new_derived = []
    for iss in issues:
        if iss.get("status") in ("resolved", "false_positive"):
            continue

        anchor = iss.get("anchor", {})
        snippet = anchor.get("snippet", "")
        filepath = anchor.get("file", "")
        function_name = anchor.get("function", "")
        last_seen = iss.get("last_seen_date", iss.get("first_seen_date", "2000-01-01"))

        # 1단계: git pickaxe
        changed = pickaxe_changed(snippet, filepath, project_path, last_seen)

        if not changed:
            # 변경 없음 → recurring 확정
            iss["status"] = "recurring"
            first_date = datetime.datetime.strptime(iss["first_seen_date"], "%Y-%m-%d")
            iss["days_elapsed"] = (datetime.datetime.now() - first_date).days + 1
            iss["last_seen_date"] = today
            print(f"  [{iss['id']}] recurring ({iss['days_elapsed']}일째): {iss['title']}")
            continue

        # 2단계: LLM 재판단
        current_code = get_current_function_code(filepath, function_name, project_path)
        verdict_data = llm_recheck(iss, current_code, config)
        verdict = verdict_data.get("verdict", "recurring")
        print(f"  [{iss['id']}] git 변경 감지 → LLM 판단: {verdict} — {iss['title']}")

        if verdict == "resolved":
            iss["status"] = "resolved"
            iss["resolved_date"] = today
            iss["resolved_reason"] = verdict_data.get("reason", "")

        elif verdict == "wrong_fix":
            iss["status"] = "recurring"
            iss["last_seen_date"] = today
            first_date = datetime.datetime.strptime(iss["first_seen_date"], "%Y-%m-%d")
            iss["days_elapsed"] = (datetime.datetime.now() - first_date).days + 1

            # 파생 이슈 생성
            derived_llm = {
                "title": verdict_data.get("new_issue_title", f"잘못된 픽스로 인한 파생 — {iss['title']}"),
                "severity": iss.get("severity", "high"),
                "target_files": [filepath],
                "anchor": {
                    "file": filepath,
                    "function": function_name,
                    "snippet": verdict_data.get("new_issue_snippet", ""),
                },
                "what_is_wrong": verdict_data.get("new_issue_what_is_wrong", ""),
                "why_dangerous": verdict_data.get("new_issue_why_dangerous", ""),
                "suggested_action": verdict_data.get("new_issue_suggested_action", ""),
            }
            derived = make_issue_record(derived_llm, run_id, "")
            derived["status"] = "derived"
            derived["parent_id"] = iss["id"]
            new_derived.append(derived)
            print(f"  → 파생 이슈 생성: {derived['title']}")

        else:
            iss["status"] = "recurring"
            iss["last_seen_date"] = today
            first_date = datetime.datetime.strptime(iss["first_seen_date"], "%Y-%m-%d")
            iss["days_elapsed"] = (datetime.datetime.now() - first_date).days + 1

    issues.extend(new_derived)
    save_issues_db(project_name, issues)
    return new_derived


def merge_new_issues(project_name, run_issues):
    """Phase 1에서 발견한 신규 이슈를 issues_db에 병합한다.
    anchor 기반으로 중복 감지 후 진짜 신규만 추가한다."""
    db = dedupe_issues(load_issues_db(project_name))
    migrate_false_positive_statuses(db)
    existing_keys = {
        canonical_issue_key(iss)
        for iss in db
        if iss.get("status") not in ("resolved", "false_positive")
    }

    added = 0
    for iss in run_issues:
        issue_key = canonical_issue_key(iss)
        if issue_key in existing_keys:
            continue
        db.append(iss)
        existing_keys.add(issue_key)
        added += 1

    save_issues_db(project_name, db)
    return added


def reconcile_missing_issues(project_name, run_issues):
    """이번 리뷰에서 재현되지 않은 open/recurring 이슈를 false_positive로 정리한다."""
    db = dedupe_issues(load_issues_db(project_name))
    migrate_false_positive_statuses(db)
    current_keys = {canonical_issue_key(iss) for iss in run_issues}
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    false_positive_count = 0
    for iss in db:
        if iss.get("status") in ("resolved", "false_positive"):
            continue
        if canonical_issue_key(iss) in current_keys:
            continue
        iss["status"] = "false_positive"
        iss["false_positive_date"] = today
        iss["false_positive_reason"] = "Not reproduced in latest review after prompt/filter cleanup."
        false_positive_count += 1

    save_issues_db(project_name, db)
    return false_positive_count


def main():
    args = parse_args()
    if not args.project:
        print("Error: --project is required.")
        sys.exit(1)

    agent = AgentState(project_name=args.project, run_id=args.run_id)
    config = agent.config

    print(f"[{args.project}] Phase 0.5: 이슈 연속성 체크 시작")
    run_continuity_check(args.project, agent.run_id, config)
    print(f"[{args.project}] Phase 0.5 완료")


if __name__ == "__main__":
    main()
