import os
import sys
import json
import re
import subprocess
from agent_core import AgentState, run_cmd, run_git, parse_args, ask_llm

def cleanup_worktree(worktree_path, branch_name, cwd):
    if os.path.exists(worktree_path):
        run_git(["worktree", "remove", "--force", worktree_path], cwd=cwd)
    run_git(["branch", "-D", branch_name], cwd=cwd)

def extract_patch(ai_response):
    try:
        payload = json.loads(ai_response)
        if isinstance(payload, dict):
            patch = payload.get("patch", "")
            if isinstance(patch, str):
                return patch.strip() + ("\n" if patch.strip() else "")
    except Exception:
        pass
    lines = ai_response.splitlines()
    patch_lines = []
    in_patch = False
    for line in lines:
        if line.startswith("```diff") or line.startswith("```patch"):
            in_patch = True
            continue
        elif line.startswith("```") and in_patch:
            break
        if in_patch or line.startswith("--- a/") or line.startswith("+++ b/"):
            in_patch = True
            patch_lines.append(line)
    return "\n".join(patch_lines) + "\n" if patch_lines else ai_response


def summarize_test_output(stdout, stderr, limit=400):
    combined = "\n".join(part for part in [stdout, stderr] if part).strip()
    return combined[:limit]


def run_git_capture_raw(args, cwd="."):
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    return result.stdout, result.stderr, result.returncode


def severity_rank(value):
    return {"low": 1, "medium": 2, "high": 3}.get((value or "").lower(), 0)


def looks_speculative(text):
    normalized = (text or "").lower()
    speculative_markers = [
        "가능성", "우려", "검증 필요", "권장", "추정", "명확하지", "might", "may", "could",
        "possible", "potential", "needs verification", "unclear", "suspicion",
    ]
    return any(marker in normalized for marker in speculative_markers)


def looks_concrete_action(text):
    normalized = (text or "").lower()
    concrete_markers = [
        "add ", "use ", "check ", "handle ", "return ", "close ", "remove ", "restore ",
        "wrap ", "set ", "pass ", "guard ", "validate ", "replace ",
        "추가", "사용", "확인", "처리", "반환", "닫", "제거", "복원", "설정", "전달", "검증", "교체",
    ]
    return any(marker in normalized for marker in concrete_markers)


def min_severity_for_level(level, config):
    phase2_conf = config.get("phase2", {})
    level_map = phase2_conf.get("min_severity_by_level", {})
    return level_map.get(str(level)) or level_map.get(int(level)) or phase2_conf.get("min_severity", "high")


def is_actionable_issue(issue, config, review_level):
    fix_conf = config.get("phase2", {})
    min_severity = min_severity_for_level(review_level, config)
    if severity_rank(issue.get("severity")) < severity_rank(min_severity):
        return False, f"severity_below_threshold:{min_severity}"

    anchor = issue.get("anchor") or {}
    target_files = issue.get("target_files") or []
    if len(target_files) != 1:
        return False, "requires_single_target_file"
    if not anchor.get("file") or not anchor.get("function"):
        return False, "missing_anchor_context"

    description = " ".join([
        issue.get("title", ""),
        issue.get("what_is_wrong", ""),
        issue.get("why_dangerous", ""),
    ])
    if looks_speculative(description):
        return False, "issue_is_speculative"

    action_text = issue.get("suggested_action", "")
    if not looks_concrete_action(action_text):
        return False, "suggested_action_not_concrete"

    if review_level <= 1 and looks_speculative(action_text):
        return False, "action_is_speculative"

    return True, ""


def load_anchor_context(issue, cwd, context_lines=60):
    anchor = issue.get("anchor") or {}
    rel_path = anchor.get("file")
    if not rel_path:
        return ""

    abs_path = os.path.join(cwd, rel_path)
    if not os.path.exists(abs_path):
        return ""

    try:
        with open(abs_path, "r") as f:
            lines = f.readlines()
    except Exception:
        return ""

    func_name = anchor.get("function", "")
    start_idx = 0
    if func_name:
        patterns = [
            re.compile(rf"^\s*func\s+\([^)]*\)\s*{re.escape(func_name)}\b"),
            re.compile(rf"^\s*func\s+{re.escape(func_name)}\b"),
        ]
        for idx, line in enumerate(lines):
            if any(p.search(line) for p in patterns):
                start_idx = idx
                break

    end_idx = min(len(lines), start_idx + context_lines)
    excerpt = "".join(lines[start_idx:end_idx]).rstrip()
    return f"File: {rel_path}\nExcerpt:\n{excerpt}" if excerpt else ""


def looks_like_unified_patch(text):
    stripped = (text or "").strip()
    if not stripped:
        return False
    return (
        "diff --git " in stripped and
        "--- a/" in stripped and
        "+++ b/" in stripped and
        "@@" in stripped
    )


def find_go_function_bounds(lines, func_name):
    start_idx = None
    header_pattern = re.compile(rf"^\s*func\s+(?:\([^)]*\)\s*)?{re.escape(func_name)}\b")
    for idx, line in enumerate(lines):
        if header_pattern.search(line):
            start_idx = idx
            break
    if start_idx is None:
        return None, None

    brace_depth = 0
    saw_open = False
    for idx in range(start_idx, len(lines)):
        brace_depth += lines[idx].count("{")
        if "{" in lines[idx]:
            saw_open = True
        brace_depth -= lines[idx].count("}")
        if saw_open and brace_depth == 0:
            return start_idx, idx + 1
    return None, None


def build_function_edit_prompt(issue, current_function, feedback_log):
    return (
        "Update the following function to address the issue.\n\n"
        f"Issue JSON:\n{json.dumps(issue, ensure_ascii=False)}\n\n"
        f"Previous attempt feedback:\n{feedback_log}\n\n"
        "Return raw JSON only with keys replacement and summary.\n"
        "- replacement must contain the full updated function source code only.\n"
        "- Keep the same function name and signature unless the issue requires a minimal change.\n"
        "- Preserve compilability. Do not invent APIs or change return types.\n"
        "- If previous feedback shows build errors, correct those exact errors before anything else.\n"
        "- If the current code already has comments or logic that justify the behavior, prefer an empty replacement.\n"
        "- Do not use markdown fences.\n"
        "- If no safe fix is possible, return an empty replacement and explain why in summary.\n\n"
        f"Current function:\n{current_function}"
    )


def try_function_replacement(issue, worktree_path, config, feedback_log):
    anchor = issue.get("anchor") or {}
    rel_path = anchor.get("file")
    func_name = anchor.get("function")
    if not rel_path or not func_name or not rel_path.endswith(".go"):
        return None, "function_replacement_not_applicable"

    abs_path = os.path.join(worktree_path, rel_path)
    if not os.path.exists(abs_path):
        return None, f"target file not found: {rel_path}"

    with open(abs_path, "r") as f:
        lines = f.readlines()

    start_idx, end_idx = find_go_function_bounds(lines, func_name)
    if start_idx is None or end_idx is None:
        return None, f"target function not found: {func_name}"

    current_function = "".join(lines[start_idx:end_idx]).rstrip()
    prompt = build_function_edit_prompt(issue, current_function, feedback_log)
    ai_response = ask_llm(
        prompt,
        config,
        max_tokens=config.get("llm", {}).get("fix_max_tokens", 1200),
        response_format={"type": "json_object"},
    )

    try:
        payload = json.loads(ai_response)
    except Exception as exc:
        return None, f"function replacement parse failed: {exc}"

    replacement = (payload.get("replacement") or "").strip()
    if not replacement:
        return None, f"unsafe_no_fix: {payload.get('summary', 'empty replacement')}"
    if not replacement.startswith("func "):
        return None, "replacement did not contain a full Go function"
    if replacement.strip() == current_function.strip():
        return None, "unsafe_no_fix: replacement was identical to current function"

    new_lines = list(lines)
    replacement_lines = [line + "\n" for line in replacement.splitlines()]
    if replacement_lines and replacement_lines[-1] != "\n":
        pass
    new_lines[start_idx:end_idx] = replacement_lines

    with open(abs_path, "w") as f:
        f.writelines(new_lines)

    run_cmd(f"gofmt -w {rel_path}", cwd=worktree_path)
    diff_out, diff_err, diff_code = run_git_capture_raw(["diff", "--", rel_path], cwd=worktree_path)
    if diff_code != 0 or not diff_out.strip():
        return None, f"no diff generated after replacement: {diff_err or payload.get('summary', '')}"
    return diff_out, ""

def main():
    args = parse_args()
    agent = AgentState(project_name=args.project, run_id=args.run_id)
    if not agent.acquire_lock():
        sys.exit(0)

    branch_name = f"auto-fix-{agent.run_id}"
    project_cwd = agent.project_context['path']
    worktree_path = os.path.abspath(os.path.join(agent.get_run_dir(), "sandbox_worktree"))

    try:
        config = agent.config
        state = agent.load_state()

        if state.get("status_p1_review") != "success" or state.get("status_p2_fix") not in ["pending"]:
            return

        state["status_p2_fix"] = "running"
        agent.save_state(state)
        review_level = int(state.get("review_level", config.get("review", {}).get("level", 1)))

        issues_file = state.get("issues_file")
        issues = []
        if issues_file and os.path.exists(issues_file):
            with open(issues_file, "r") as f:
                issues = json.load(f)

        if not issues:
            state["status_p2_fix"] = "skipped"
            agent.save_state(state)
            return

        actionable_issues = []
        skipped_reasons = []
        for issue in issues:
            ok, reason = is_actionable_issue(issue, config, review_level)
            if ok:
                actionable_issues.append(issue)
            else:
                skipped_reasons.append(f"{issue.get('title', '')}: {reason}")

        if not actionable_issues:
            state["status_p2_fix"] = "skipped"
            state["fix_note"] = "no_actionable_issue: " + "; ".join(skipped_reasons[:3])
            agent.save_state(state)
            return

        cleanup_worktree(worktree_path, branch_name, project_cwd)
        out, err, code = run_git(
            ["worktree", "add", worktree_path, "-b", branch_name, state.get("target_commit")],
            cwd=project_cwd
        )
        if code != 0:
            raise RuntimeError(f"git worktree add failed: {err}")

        max_retries = config.get("max_retries", 3)
        test_cmd = agent.merged_commands.get("test", "echo 'skip test'")

        # ── baseline 측정 ──────────────────────────────────────────────
        # 패치 적용 전 테스트를 먼저 실행해 기준값을 기록한다.
        # baseline이 이미 red인 경우, 패치 후 테스트도 red라고 해서
        # 정상 패치를 탈락시키지 않기 위한 안전장치다.
        baseline_out, baseline_err, baseline_code = run_cmd(test_cmd, cwd=worktree_path)
        baseline_output = "\n".join(part for part in [baseline_out, baseline_err] if part).strip()
        state["baseline_test_code"] = baseline_code
        if baseline_code != 0:
            state["baseline_test_warning"] = (
                f"Baseline test was already failing before patch: {summarize_test_output(baseline_out, baseline_err)}"
            )
        agent.save_state(state)
        # ────────────────────────────────────────────────────────────────

        success = False
        feedback_log = "Initial attempt"
        target_issue = actionable_issues[0]
        final_best_patch = None
        last_failed_patch = None
        last_failed_test_summary = None

        for attempt in range(1, max_retries + 1):
            state["attempt_count"] = attempt
            agent.save_state(state)

            candidate_patch, function_feedback = try_function_replacement(
                target_issue,
                worktree_path,
                config,
                feedback_log,
            )
            if candidate_patch is None:
                run_git(["reset", "--hard", "HEAD"], cwd=worktree_path)
                run_git(["clean", "-fd"], cwd=worktree_path)

                if function_feedback.startswith("unsafe_no_fix:"):
                    state["status_p2_fix"] = "skipped"
                    state["fix_note"] = function_feedback
                    agent.save_state(state)
                    return

                anchor_context = load_anchor_context(target_issue, worktree_path)
                prompt = (
                    "Fix the following issue by generating a minimal unified git patch.\n\n"
                    f"Issue JSON:\n{json.dumps(target_issue, ensure_ascii=False)}\n\n"
                    f"Current code context:\n{anchor_context or 'N/A'}\n\n"
                    f"Feedback from previous attempt:\n{feedback_log}\n\n"
                    f"Function replacement attempt feedback:\n{function_feedback}\n\n"
                    "Rules:\n"
                    "- Modify only the files listed in target_files.\n"
                    "- Return raw JSON only with keys patch and summary.\n"
                    "- patch must be a complete unified diff beginning with diff --git and containing at least one @@ hunk.\n"
                    "- Use the real current code context. Do not invent line numbers or fake index hashes.\n"
                    "- If the issue is not safely fixable from the provided context, return an empty patch and explain why in summary.\n"
                    "- Do not use markdown fences.\n"
                    "- Keep summary under 120 characters.\n"
                )

                ai_response = ask_llm(
                    prompt,
                    config,
                    max_tokens=config.get("llm", {}).get("fix_max_tokens", 1200),
                    response_format={"type": "json_object"},
                )
                candidate_patch = extract_patch(ai_response)

                if not looks_like_unified_patch(candidate_patch):
                    feedback_log = (
                        "Response did not contain a complete unified patch.\n"
                        f"Raw response:\n{ai_response[:1200]}\n\n"
                        "Return valid JSON with a complete patch string or an empty patch when unsafe."
                    )
                    continue
            else:
                run_git(["reset", "--hard", "HEAD"], cwd=worktree_path)
                run_git(["clean", "-fd"], cwd=worktree_path)

            patch_path = os.path.join(agent.get_run_dir(), f"candidate_{attempt}.patch")
            with open(patch_path, "w") as f:
                f.write(candidate_patch)

            _, apply_err, apply_code = run_git(["apply", "--check", patch_path], cwd=worktree_path)
            if apply_code == 0:
                run_git(["apply", patch_path], cwd=worktree_path)
                test_out, test_err, test_code = run_cmd(test_cmd, cwd=worktree_path)

                with open(os.path.join(agent.get_run_dir(), f"candidate_{attempt}_test.log"), "w") as tf:
                    tf.write(test_out + "\n" + test_err)

                if test_code == 0:
                    # 테스트 통과: 무조건 성공
                    success = True
                    final_best_patch = patch_path
                    break
                elif baseline_code != 0 and test_code == baseline_code:
                    test_output = "\n".join(part for part in [test_out, test_err] if part).strip()
                    if test_output != baseline_output:
                        feedback_log = (
                            "Patch changed the failing test output on a repo that was already red.\n"
                            f"Baseline output:\n{baseline_output[:1200]}\n\n"
                            f"Current output:\n{test_output[:1200]}\n\n"
                            "Do not introduce additional regressions. Keep the same failing baseline or make tests pass."
                        )
                        run_git(["reset", "--hard", "HEAD"], cwd=worktree_path)
                        run_git(["clean", "-fd"], cwd=worktree_path)
                        continue

                    # baseline이 이미 red였던 repo: 동일한 실패 상태/출력 유지 시에만 조건부 성공.
                    success = True
                    final_best_patch = patch_path
                    state["fix_note"] = (
                        "baseline_broken: repo test was already failing before patch. "
                        "Patch applies cleanly and the failing test output is unchanged."
                    )
                    break
                else:
                    # baseline은 green이었는데 패치 후 red → 진짜 실패
                    current_test_summary = summarize_test_output(test_out, test_err, limit=1200)
                    if candidate_patch == last_failed_patch or current_test_summary == last_failed_test_summary:
                        state["status_p2_fix"] = "skipped"
                        state["fix_note"] = (
                            "no_safe_patch: repeated fix attempts produced the same failing result. "
                            "The reviewed issue may be non-actionable or a false positive."
                        )
                        agent.save_state(state)
                        return

                    last_failed_patch = candidate_patch
                    last_failed_test_summary = current_test_summary
                    feedback_log = (
                        f"Patch applied but {test_cmd} failed. Error:\n{current_test_summary}\nPlease fix it."
                    )
                    run_git(["reset", "--hard", "HEAD"], cwd=worktree_path)
                    run_git(["clean", "-fd"], cwd=worktree_path)
            else:
                feedback_log = (
                    f"Patch rejected by check. Error:\n{apply_err}\nProvide correct diff format."
                )

        state["status_p2_fix"] = "success" if success else "max_retries_reached"
        if final_best_patch:
            state["best_patch_path"] = final_best_patch

        agent.save_state(state)

    except Exception as e:
        state["status_p2_fix"] = "failed"
        state["error_message"] = str(e)
        agent.save_state(state)
    finally:
        cleanup_worktree(worktree_path, branch_name, project_cwd)
        agent.release_lock()

if __name__ == "__main__":
    main()
