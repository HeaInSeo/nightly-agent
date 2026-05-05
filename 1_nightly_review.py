import os
import sys
import json
import re
import glob
from jinja2 import Environment, FileSystemLoader
from agent_core import AgentState, run_cmd, run_git, parse_args, ask_llm, now_iso, elapsed_seconds, load_last_reviewed, save_last_reviewed


def collect_command_results(agent, cwd):
    """빌드/테스트/린트/검증 결과를 한 번만 실행해 리포트용으로 정리한다."""
    detailed_results = {}
    for cmd_type in ["build", "test", "lint", "validate"]:
        cmd = agent.merged_commands.get(cmd_type)
        if not cmd:
            continue
        out, err, code = run_cmd(cmd, cwd=cwd)
        if code == 0:
            detailed_results[cmd_type] = {"status": "SUCCESS", "output": ""}
        else:
            combined = "\n".join(part for part in [out, err] if part).strip()
            detailed_results[cmd_type] = {"status": "FAILED", "output": combined[-1000:]}
    return detailed_results

def get_filtered_diff(agent, state, cwd):
    # B방식: last_reviewed_commit이 있으면 그 이후부터만 diff
    last_reviewed = load_last_reviewed(agent.project_name)
    last_commit = last_reviewed.get("last_reviewed_commit")

    if last_commit:
        _, _, code = run_git(["cat-file", "-t", last_commit], cwd=cwd)
        if code != 0:
            last_commit = None  # 커밋이 사라졌으면 fallback

    if last_commit:
        base_ref = last_commit
        base_mode = "last_reviewed"
    else:
        # 첫 실행 fallback: base_branch (없으면 HEAD~3)
        base_branch = agent.project_context.get("base_branch", "HEAD~3")
        merge_base_out, _, code = run_cmd(
            f"git merge-base {base_branch} {state['target_commit']}", cwd=cwd
        )
        if code != 0:
            return "", "", f"Merge base failed. Make sure {base_branch} exists locally."
        base_ref = merge_base_out.strip()
        base_mode = "base_branch_fallback"

    state["diff_base_mode"] = base_mode
    state["diff_base_ref"] = base_ref

    review_conf = agent.project_context.get("review", {})
    includes = review_conf.get("include", [])
    excludes = review_conf.get("exclude", [])

    pathspec = []
    if includes:
        pathspec.extend(includes)
    if excludes:
        for ex in excludes:
            pathspec.append(f"':!{ex}'")
    paths_arg = " ".join(pathspec)

    diff_cmd = f"git diff {base_ref}..{state['target_commit']}"
    if paths_arg:
        diff_cmd += f" -- {paths_arg}"

    diff_text, _, _ = run_cmd(diff_cmd, cwd=cwd)
    return diff_text, base_ref, ""


def extract_changed_files(diff_text):
    files = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
    return files


def extract_go_function_names(diff_text):
    names = []
    pattern = re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\b")
    for line in diff_text.splitlines():
        if not line.startswith("@@"):
            continue
        match = pattern.search(line)
        if match:
            names.append(match.group(1))
    return names


def find_go_function_excerpt(path, func_name, max_lines=60):
    if not os.path.exists(path):
        return ""
    with open(path, "r") as f:
        lines = f.readlines()

    header_pattern = re.compile(rf"^\s*func\s+(?:\([^)]*\)\s*)?{re.escape(func_name)}\b")
    start_idx = None
    for idx, line in enumerate(lines):
        if header_pattern.search(line):
            start_idx = idx
            break
    if start_idx is None:
        return ""

    comment_start = start_idx
    while comment_start > 0:
        prev = lines[comment_start - 1]
        if prev.startswith("//") or not prev.strip():
            comment_start -= 1
            continue
        break

    end_idx = min(len(lines), start_idx + max_lines)
    return "".join(lines[comment_start:end_idx]).rstrip()


def build_review_context(diff_text, cwd):
    changed_files = extract_changed_files(diff_text)[:3]
    function_names = extract_go_function_names(diff_text)[:3]
    blocks = []

    for rel_path in changed_files:
        abs_path = os.path.join(cwd, rel_path)
        if not os.path.exists(abs_path):
            continue

        excerpt = ""
        for func_name in function_names:
            excerpt = find_go_function_excerpt(abs_path, func_name)
            if excerpt:
                blocks.append(f"[File] {rel_path}\n[Function] {func_name}\n{excerpt}")
                break

        if excerpt:
            continue

        with open(abs_path, "r") as f:
            fallback = "".join(f.readlines()[:80]).rstrip()
        if fallback:
            blocks.append(f"[File] {rel_path}\n{fallback}")

    return "\n\n".join(blocks[:3])


def issue_looks_false_positive(issue, cwd):
    anchor = issue.get("anchor") or {}
    rel_path = anchor.get("file")
    func_name = anchor.get("function")
    if not rel_path or not func_name:
        return False

    excerpt = find_go_function_excerpt(os.path.join(cwd, rel_path), func_name, max_lines=80)
    if not excerpt:
        return False

    issue_text = " ".join([
        issue.get("title", ""),
        issue.get("what_is_wrong", ""),
        issue.get("suggested_action", ""),
    ]).lower()
    excerpt_lower = excerpt.lower()

    if (
        ("goroutine" in issue_text or "setlimit" in issue_text or "동시성 제한" in issue_text)
        and "without a goroutine limit" in excerpt_lower
        and "fan-in policy limits belong at the dag validation layer" in excerpt_lower
    ):
        return True

    if (
        (
            "컨텍스트" in issue_text or
            "취소" in issue_text or
            "ctx.done" in issue_text or
            "errgroup.withcontext" in issue_text or
            "context" in issue_text
        )
        and "case <-egctx.done()" in excerpt_lower
    ):
        return True

    return False


def recent_project_states(project_name, current_run_id, limit=6):
    pattern = os.path.join(".nightly_agent", "runs", "*", project_name, "state.json")
    states = []
    for path in sorted(glob.glob(pattern), reverse=True):
        if f"/{current_run_id}/" in path:
            continue
        try:
            with open(path, "r") as f:
                states.append(json.load(f))
        except Exception:
            continue
        if len(states) >= limit:
            break
    return states


def resolve_review_level(agent, config):
    review_conf = config.get("review", {})
    base_level = int(review_conf.get("level", 1))
    max_level = int(review_conf.get("max_level", 3))
    auto_promote = review_conf.get("auto_promote", True)
    level = max(1, min(base_level, max_level))

    if not auto_promote:
        return level

    states = recent_project_states(agent.project_name, agent.run_id, limit=6)
    clean_streak = 0
    for state in states:
        if state.get("status_p1_review") != "success":
            break
        issues_file = state.get("issues_file")
        issues = None
        if issues_file and os.path.exists(issues_file):
            try:
                with open(issues_file, "r") as f:
                    issues = json.load(f)
            except Exception:
                issues = None
        if issues is None or issues:
            break
        clean_streak += 1

    if clean_streak >= 4:
        level += 2
    elif clean_streak >= 2:
        level += 1
    return min(level, max_level)


def review_level_profile(level):
    if level <= 1:
        return {
            "max_issues": 2,
            "summary_chars": 80,
            "strict_mode": True,
        }
    if level == 2:
        return {
            "max_issues": 3,
            "summary_chars": 100,
            "strict_mode": True,
        }
    return {
        "max_issues": 5,
        "summary_chars": 120,
        "strict_mode": False,
    }

def main():
    args = parse_args()
    if not args.project:
        print("Error: --project is required.")
        return

    agent = AgentState(project_name=args.project, run_id=args.run_id)
    if not agent.acquire_lock():
        sys.exit(0)

    state = {}
    try:
        config = agent.config

        cwd = agent.project_context['path']
        if not os.path.isdir(cwd):
            print(f"Error: project path does not exist: {cwd}", file=sys.stderr)
            sys.exit(1)

        state = agent.load_state()
        if state.get("status_p1_review") in ["success", "skipped", "failed"]:
            return

        state["status_p1_review"] = "running"
        state["review_started_at"] = now_iso()
        agent.save_state(state)
        
        # 1. Preflight Commands (Build, Test, Lint)
        detailed_test_results = collect_command_results(agent, cwd)

        # 2. Diff extraction
        diff_text, merge_base, err_msg = get_filtered_diff(agent, state, cwd)
        if err_msg or not diff_text.strip():
            state["review_finished_at"] = now_iso()
            state["review_duration_sec"] = elapsed_seconds(
                state.get("review_started_at"), state.get("review_finished_at")
            )
            state["status_p1_review"] = "skipped"
            state["error_message"] = err_msg or "No diff found."
            agent.save_state(state)
            return

        diff_lines = len(diff_text.splitlines())
        if diff_lines > config.get("max_diff_lines", 2000):
            state["review_finished_at"] = now_iso()
            state["review_duration_sec"] = elapsed_seconds(
                state.get("review_started_at"), state.get("review_finished_at")
            )
            state["status_p1_review"] = "skipped"
            state["error_message"] = "Diff too large limit exceeded."
            agent.save_state(state)
            return

        state["merge_base"] = merge_base
        state["base_branch"] = agent.project_context.get('base_branch')
        review_level = resolve_review_level(agent, config)
        level_profile = review_level_profile(review_level)
        state["review_level"] = review_level
        
        env = Environment(loader=FileSystemLoader('.'))

        # 3. Dynamic Prompting via Jinja — 첫 번째 매칭 타입 프롬프트 사용
        prompt_path = "prompts/review/generic.md.j2"
        for pt in getattr(agent, 'project_types', []):
            candidate = f"prompts/review/{pt}.md.j2"
            if os.path.exists(candidate):
                prompt_path = candidate
                break
        prompt_template = env.get_template(prompt_path)
        review_context = build_review_context(diff_text, cwd)
        rendered_prompt = prompt_template.render(
            heuristics=agent.heuristics,
            diff_text=diff_text,
            review_context=review_context,
            review_level=review_level,
        )
        
        lang = config.get("language", "ko")
        
        # 8B급 로컬 모델에서도 안정적으로 닫힌 JSON을 반환하도록
        # 리뷰 스키마를 최소 필드만 남긴다.
        json_directive = (
            "\n\nOutput ONLY valid JSON mapped to this exact struct:\n"
            '{"one_line_summary":"","top_issues":[{'
            '"title":"",'
            '"severity":"high|medium|low",'
            '"target_files":[""],'
            '"what_is_wrong":"",'
            '"suggested_action":"",'
            '"anchor":{"file":"","function":""}'
            '}]}'
        )
        json_directive += (
            "\nReturn raw JSON only on a single line."
            "\nDo not use markdown fences."
            f"\nReturn at most {level_profile['max_issues']} top_issues."
            f"\nKeep one_line_summary under {level_profile['summary_chars']} characters."
            "\nKeep each string field concise: 1 short sentence."
            "\nOmit fields that are not listed in the schema."
            "\nIf there is no meaningful issue, return an empty top_issues array."
        )
        if level_profile["strict_mode"]:
            json_directive += (
                "\nDo not report an issue when nearby comments or current code explicitly justify the behavior."
                "\nTreat design-rationale comments as strong evidence unless the diff clearly breaks that contract."
                "\nDo not invent concurrency bugs from heuristic suspicion alone."
            )
        else:
            json_directive += (
                "\nPrefer concrete bugs first, but you may include medium-confidence risks if they are technically grounded."
            )
        if lang == "ko":
            json_directive += "\nALL text inside the JSON values MUST be written in Korean."
        else:
            json_directive += "\nALL text inside the JSON values MUST be written in English."

        state["analysis_started_at"] = now_iso()
        agent.save_state(state)
        ai_res = ask_llm(
            rendered_prompt + json_directive,
            config,
            max_tokens=config.get("llm", {}).get("review_max_tokens", 1400),
            response_format={"type": "json_object"},
        )
        state["analysis_finished_at"] = now_iso()
        state["analysis_duration_sec"] = elapsed_seconds(
            state.get("analysis_started_at"), state.get("analysis_finished_at")
        )
        llm_parse_ok = False
        try:
            ai_data = json.loads(ai_res.replace("```json", "").replace("```", "").strip())
            llm_parse_ok = True
        except Exception as parse_err:
            ai_data = {"top_issues": [], "one_line_summary": "", "categorize": {}, "llm_review": {}}
            state["llm_parse_error"] = str(parse_err)
            state["llm_raw_snippet"] = ai_res[:300] if ai_res else ""

        ai_data.setdefault("top_issues", [])
        ai_data.setdefault("one_line_summary", "")
        ai_data.setdefault("categorize", {})
        ai_data.setdefault("llm_review", {})
        ai_data["top_issues"] = [
            issue for issue in ai_data.get("top_issues", [])
            if not issue_looks_false_positive(issue, cwd)
        ]

        state["report_started_at"] = now_iso()
        agent.save_state(state)

        def render_report():
            return report_template.render(
                project_name=agent.project_name,
                current_date=state["created_at"],
                target_branch=state["target_branch"],
                target_commit=state["target_commit"],
                merge_base=merge_base,
                changed_files_count=len([l for l in diff_text.splitlines() if l.startswith('+++')]),
                diff_lines_count=diff_lines,
                status_review="Degraded" if not llm_parse_ok else "Done",
                status_fix="Pending",
                review_level=state.get("review_level"),
                review_started_at=state.get("review_started_at"),
                review_finished_at=state.get("review_finished_at"),
                review_duration_sec=state.get("review_duration_sec"),
                analysis_started_at=state.get("analysis_started_at"),
                analysis_finished_at=state.get("analysis_finished_at"),
                analysis_duration_sec=state.get("analysis_duration_sec"),
                report_started_at=state.get("report_started_at"),
                report_finished_at=state.get("report_finished_at"),
                report_duration_sec=state.get("report_duration_sec"),
                one_line_summary=ai_data.get("one_line_summary", ""),
                top_issues=ai_data.get("top_issues", []),
                categorize=ai_data.get("categorize", {}),
                test_results=detailed_test_results,
                llm_review=ai_data.get("llm_review", {})
            )

        report_template = env.get_template(f"templates/{lang}/report.md.j2")

        report_path = os.path.join(agent.get_run_dir(), "review_report.md")
        with open(report_path, "w") as f:
            f.write(render_report())

        # issues.json: UUID + anchor 포함 확장 스키마로 저장
        from agent_core import make_issue_record
        run_issues = []
        for iss in ai_data.get("top_issues", []):
            run_issues.append(make_issue_record(iss, agent.run_id, state["target_commit"]))

        issues_path = os.path.join(agent.get_run_dir(), "issues.json")
        with open(issues_path, "w") as f:
            json.dump(run_issues, f, indent=4, ensure_ascii=False)

        state["review_report_path"] = report_path
        state["issues_file"] = issues_path
        state["one_line_summary"] = ai_data.get("one_line_summary", "")
        state["categorize"] = ai_data.get("categorize", {})
        state["llm_review"] = ai_data.get("llm_review", {})
        state["test_results"] = detailed_test_results
        state["llm_parse_ok"] = llm_parse_ok
        state["report_finished_at"] = now_iso()
        state["report_duration_sec"] = elapsed_seconds(
            state.get("report_started_at"), state.get("report_finished_at")
        )
        state["review_finished_at"] = state["report_finished_at"]
        state["review_duration_sec"] = elapsed_seconds(
            state.get("review_started_at"), state.get("review_finished_at")
        )
        state["status_p1_review"] = "success" if llm_parse_ok else "degraded"

        with open(report_path, "w") as f:
            f.write(render_report())

        agent.save_state(state)

        if llm_parse_ok:
            save_last_reviewed(agent.project_name, state["target_commit"], state.get("review_finished_at"))

    except Exception as e:
        state["analysis_finished_at"] = state.get("analysis_finished_at") or now_iso()
        if state.get("analysis_started_at") and not state.get("analysis_duration_sec"):
            state["analysis_duration_sec"] = elapsed_seconds(
                state.get("analysis_started_at"), state.get("analysis_finished_at")
            )
        if state.get("report_started_at") and not state.get("report_finished_at"):
            state["report_finished_at"] = now_iso()
            state["report_duration_sec"] = elapsed_seconds(
                state.get("report_started_at"), state.get("report_finished_at")
            )
        state["review_finished_at"] = now_iso()
        state["review_duration_sec"] = elapsed_seconds(
            state.get("review_started_at"), state.get("review_finished_at")
        )
        state["status_p1_review"] = "failed"
        state["error_message"] = str(e)
        agent.save_state(state)
    finally:
        agent.release_lock()

if __name__ == "__main__":
    main()
