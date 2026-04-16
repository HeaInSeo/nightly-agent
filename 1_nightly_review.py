import os
import sys
import json
import requests
from jinja2 import Environment, FileSystemLoader
from agent_core import AgentState, run_cmd, parse_args

def ask_ollama(prompt, conf):
    url = conf.get("ollama_url", "http://localhost:11434/api/generate")
    model = conf.get("model_name", "gemma4:26b")
    try:
        response = requests.post(url, json={"model": model, "prompt": prompt, "stream": False}, timeout=600)
        response.raise_for_status()
        return response.json().get("response", "")
    except Exception as e:
        raise RuntimeError(f"Ollama API Call Failed: {e}")

def get_filtered_diff(agent, state, cwd):
    base_branch = agent.project_context.get("base_branch", "main")
    merge_base_out, err, code = run_cmd(f"git merge-base {base_branch} {state['target_commit']}", cwd=cwd)
    if code != 0:
        return "", "", f"Merge base failed. Make sure {base_branch} exists locally."
    
    merge_base = merge_base_out.strip()
    
    # Path filtering based on includes/excludes
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
    diff_strategy = f"git diff {base_branch}...{state['target_commit']}"
    if paths_arg:
        diff_strategy += f" -- {paths_arg}"
        
    diff_text, err, code = run_cmd(diff_strategy, cwd=cwd)
    return diff_text, merge_base, ""

def main():
    args = parse_args()
    if not args.project:
        print("Error: --project is required.")
        return

    agent = AgentState(project_name=args.project, run_id=args.run_id)
    if not agent.acquire_lock():
        sys.exit(0)

    try:
        config = agent.config
        state = agent.load_state()
        if state.get("status_p1_review") in ["success", "skipped", "failed"]:
            return

        state["status_p1_review"] = "running"
        agent.save_state(state)

        cwd = agent.project_context['path']
        
        # 1. Preflight Commands (Build, Test, Lint)
        test_results = {}
        for cmd_type in ["build", "test", "lint", "validate"]:
            cmd = agent.merged_commands.get(cmd_type)
            if cmd:
                out, err, code = run_cmd(cmd, cwd=cwd)
                res_str = "SUCCESS" if code == 0 else "FAILED"
                test_results[cmd_type] = f"[{res_str}]\n{err[-500:] if code != 0 else ''}"

        # 2. Diff extraction
        diff_text, merge_base, err_msg = get_filtered_diff(agent, state, cwd)
        if err_msg or not diff_text.strip():
            state["status_p1_review"] = "skipped"
            state["error_message"] = err_msg or "No diff found."
            agent.save_state(state)
            return

        diff_lines = len(diff_text.splitlines())
        if diff_lines > config.get("max_diff_lines", 2000):
            state["status_p1_review"] = "skipped"
            state["error_message"] = "Diff too large limit exceeded."
            agent.save_state(state)
            return

        state["merge_base"] = merge_base
        state["base_branch"] = agent.project_context.get('base_branch')
        
        env = Environment(loader=FileSystemLoader('.'))
        
        # 3. Dynamic Prompting via Jinja
        p_type = agent.project_context.get('type')
        prompt_template = env.get_template(f"prompts/review/{p_type}.md.j2")
        rendered_prompt = prompt_template.render(heuristics=agent.heuristics, diff_text=diff_text)
        
        lang = config.get("language", "ko")
        
        # Force JSON constraint
        json_directive = "\n\nOutput ONLY valid JSON string mapped to this exact struct: {\"one_line_summary\":\"\",\"top_issues\":[{\"id\":\"1\",\"title\":\"\",\"severity\":\"high\",\"target_files\":[\"\"],\"suggested_action\":\"\"}],\"categorize\":{\"features\":0,\"refactor\":0,\"config\":0,\"tests\":0,\"infra\":0},\"llm_review\":{\"bugs\":\"\",\"regressions\":\"\",\"missing_tests\":\"\",\"architecture\":\"\",\"performance\":\"\"}}"
        if lang == "ko":
            json_directive += "\nALL text inside the JSON values MUST be written in Korean."
        else:
            json_directive += "\nALL text inside the JSON values MUST be written in English."
        
        ai_res = ask_ollama(rendered_prompt + json_directive, config)
        try:
            ai_data = json.loads(ai_res.replace("```json", "").replace("```", "").strip())
        except Exception:
            ai_data = {"top_issues": []}

        report_template = env.get_template(f"templates/{lang}/report.md.j2")
        final_report = report_template.render(
            project_name=agent.project_name,
            current_date=state["created_at"],
            target_branch=state["target_branch"],
            target_commit=state["target_commit"],
            merge_base=merge_base,
            changed_files_count=len([l for l in diff_text.splitlines() if l.startswith('+++')]),
            diff_lines_count=diff_lines,
            status_review="Done",
            status_fix="Pending",
            one_line_summary=ai_data.get("one_line_summary", ""),
            top_issues=ai_data.get("top_issues", []),
            categorize=ai_data.get("categorize", {}),
            test_results=test_results,
            llm_review=ai_data.get("llm_review", {})
        )

        report_path = os.path.join(agent.get_run_dir(), "review_report.md")
        with open(report_path, "w") as f:
            f.write(final_report)
            
        issues_path = os.path.join(agent.get_run_dir(), "issues.json")
        with open(issues_path, "w") as f:
            json.dump(ai_data.get("top_issues", []), f, indent=4)

        state["review_report_path"] = report_path
        state["issues_file"] = issues_path
        state["status_p1_review"] = "success"
        agent.save_state(state)

    except Exception as e:
        state["status_p1_review"] = "failed"
        state["error_message"] = str(e)
        agent.save_state(state)
    finally:
        agent.release_lock()

if __name__ == "__main__":
    main()
