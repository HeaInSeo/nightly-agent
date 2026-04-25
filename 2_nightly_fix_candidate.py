import os
import sys
import json
from agent_core import AgentState, run_cmd, run_git, parse_args, ask_llm

def cleanup_worktree(worktree_path, branch_name, cwd):
    if os.path.exists(worktree_path):
        run_git(["worktree", "remove", "--force", worktree_path], cwd=cwd)
    run_git(["branch", "-D", branch_name], cwd=cwd)

def extract_patch(ai_response):
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

        issues_file = state.get("issues_file")
        issues = []
        if issues_file and os.path.exists(issues_file):
            with open(issues_file, "r") as f:
                issues = json.load(f)

        if not issues:
            state["status_p2_fix"] = "skipped"
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
        _, baseline_err, baseline_code = run_cmd(test_cmd, cwd=worktree_path)
        state["baseline_test_code"] = baseline_code
        if baseline_code != 0:
            state["baseline_test_warning"] = (
                f"Baseline test was already failing before patch: {baseline_err[:200]}"
            )
        agent.save_state(state)
        # ────────────────────────────────────────────────────────────────

        success = False
        feedback_log = "Initial attempt"
        target_issue = issues[0]
        final_best_patch = None

        for attempt in range(1, max_retries + 1):
            state["attempt_count"] = attempt
            agent.save_state(state)

            prompt = (
                f"Fix the following issue by generating a unified git patch.\n\n"
                f"Issue:\n{json.dumps(target_issue)}\n\n"
                f"Feedback from previous attempt:\n{feedback_log}\n\n"
                f"Please output ONLY the unified patch inside a ```diff block."
            )

            ai_response = ask_llm(prompt, config)
            candidate_patch = extract_patch(ai_response)

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
                elif baseline_code != 0:
                    # baseline이 이미 red였던 repo: 패치가 apply되면 조건부 성공.
                    # 테스트 실패가 이 패치 때문인지 알 수 없으므로 탈락시키지 않는다.
                    success = True
                    final_best_patch = patch_path
                    state["fix_note"] = (
                        "baseline_broken: repo test was already failing before patch. "
                        "Patch applies cleanly but test result unchanged."
                    )
                    break
                else:
                    # baseline은 green이었는데 패치 후 red → 진짜 실패
                    feedback_log = (
                        f"Patch applied but {test_cmd} failed. Error:\n{test_err}\nPlease fix it."
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
