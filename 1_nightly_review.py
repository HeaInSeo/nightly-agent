import os
import sys
import json
import re
import glob
from jinja2 import Environment, FileSystemLoader
from agent_core import AgentState, run_cmd, run_git, parse_args, ask_llm, now_iso, elapsed_seconds, load_last_reviewed, save_last_reviewed

SOURCE_EXTENSIONS = {'.go', '.py', '.cs', '.ts', '.js', '.rs', '.sh'}
EXCLUDE_DIRS = {'.git', 'vendor', 'node_modules', 'venv', '__pycache__', '.nightly_agent', 'dist', 'build', '.build', 'obj', 'bin'}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# 리뷰 우선순위에서 맨 뒤로 보내는 파일들 (자동생성·의존성 잠금 파일)
SKIP_PATTERNS = [
    re.compile(r'\.pb\.go$'),                        # proto 생성 Go
    re.compile(r'\.(gen|generated|auto)\.(go|ts|cs)$'),
    re.compile(r'^go\.sum$'),                         # 의존성 잠금
    re.compile(r'(^|[\\/])vendor[\\/]'),
    re.compile(r'(^|[\\/])node_modules[\\/]'),
]

# 인터페이스/계약 변경 파일 — 파일 간 영향 범위가 넓어 HIGH보다 먼저 리뷰
CONTRACT_SENSITIVE_PATTERNS = [
    re.compile(r'\.proto$'),
    re.compile(r'^go\.mod$'),
    re.compile(r'(^|[\\/])migrations?[\\/]'),         # DB 스키마 변경
    re.compile(r'openapi\.(yaml|yml|json)$'),
    re.compile(r'swagger\.(yaml|yml|json)$'),
    re.compile(r'(^|[\\/])crd[\\/]'),                 # k8s CRD
    re.compile(r'(^|[\\/])rbac[\\/]'),
    re.compile(r'_schema\.(go|ts|json)$'),
]

HIGH_PRIORITY_PATTERNS = [
    re.compile(r'(^|[\\/])main\.(go|py|cs|ts|js)$'),
    re.compile(r'(^|[\\/])cmd[\\/]'),
    re.compile(r'(^|[\\/])api[\\/]'),
    re.compile(r'(^|[\\/])handler'),
    re.compile(r'(^|[\\/])controller'),
    re.compile(r'(^|[\\/])service[\\/]'),
    re.compile(r'(^|[\\/])auth[\\/]'),
    re.compile(r'(^|[\\/])security[\\/]'),
]

_TEST_GO_RE = re.compile(r'_test\.go$')


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


def get_full_code_content(agent, cwd):
    review_conf = agent.project_context.get("review", {})
    includes = review_conf.get("include", [])
    excludes = review_conf.get("exclude", [])

    files_to_read = []
    if includes:
        for pattern in includes:
            matched = glob.glob(os.path.join(cwd, pattern), recursive=True)
            files_to_read.extend(f for f in matched if os.path.isfile(f))
    else:
        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fname in files:
                if os.path.splitext(fname)[1] in SOURCE_EXTENSIONS:
                    files_to_read.append(os.path.join(root, fname))

    excluded = set()
    for ex in excludes:
        for matched in glob.glob(os.path.join(cwd, ex), recursive=True):
            excluded.add(os.path.abspath(matched))

    blocks = []
    files_included = []
    for fpath in sorted(set(files_to_read)):
        if os.path.abspath(fpath) in excluded:
            continue
        rel = os.path.relpath(fpath, cwd)
        try:
            with open(fpath, 'r', errors='replace') as f:
                content = f.read()
            if content.strip():
                blocks.append(f"=== {rel} ===\n{content}")
                files_included.append(rel)
        except Exception:
            continue
    return "\n\n".join(blocks), files_included


def run_full_code_review(agent, state, config, cwd, env, lang, level_profile):
    """전체 소스 파일 기반 LLM 리뷰. diff 없음 또는 diff 리뷰 clean 시 호출."""
    code_content, reviewed_files = get_full_code_content(agent, cwd)
    if not code_content.strip():
        return {"triggered": False, "issues": [], "summary": "", "parse_ok": False, "files": [], "truncated": False}

    state["full_review_started_at"] = now_iso()
    agent.save_state(state)

    full_prompt_template = env.get_template("prompts/review/fullcode.md.j2")
    full_rendered = full_prompt_template.render(
        heuristics=agent.heuristics,
        code_content=code_content,
    )
    full_json_directive = (
        "\n\nOutput ONLY valid JSON mapped to this exact struct:\n"
        '{"one_line_summary":"","top_issues":[{'
        '"title":"",'
        '"severity":"high|medium|low",'
        '"target_files":[""],'
        '"what_is_wrong":"",'
        '"suggested_action":"",'
        '"anchor":{"file":"","function":""}'
        '}]}'
        "\nReturn raw JSON only on a single line."
        "\nDo not use markdown fences."
        f"\nReturn at most {level_profile['max_issues']} top_issues."
        f"\nKeep one_line_summary under {level_profile['summary_chars']} characters."
        "\nKeep each string field concise: 1 short sentence."
        "\nOmit fields that are not listed in the schema."
        "\nIf there is no meaningful issue, return an empty top_issues array."
    )
    if lang == "ko":
        full_json_directive += "\nALL text inside the JSON values MUST be written in Korean."
    else:
        full_json_directive += "\nALL text inside the JSON values MUST be written in English."

    full_ai_res = ask_llm(
        full_rendered + full_json_directive,
        config,
        max_tokens=config.get("llm", {}).get("review_max_tokens", 1400),
        response_format={"type": "json_object"},
    )

    state["full_review_finished_at"] = now_iso()
    state["full_review_duration_sec"] = elapsed_seconds(
        state.get("full_review_started_at"), state.get("full_review_finished_at")
    )

    parse_ok = False
    issues = []
    summary = ""
    raw_snippet = ""
    try:
        full_ai_data = json.loads(full_ai_res.replace("```json", "").replace("```", "").strip())
        parse_ok = True
        issues = [
            iss for iss in full_ai_data.get("top_issues", [])
            if not issue_looks_false_positive(iss, cwd)
        ]
        summary = full_ai_data.get("one_line_summary", "")
    except Exception:
        raw_snippet = full_ai_res[:300] if full_ai_res else ""

    state["full_code_review"] = True
    state["full_review_parse_ok"] = parse_ok
    state["full_review_issues_count"] = len(issues)
    state["full_review_files"] = reviewed_files
    state["full_review_truncated"] = False
    if raw_snippet:
        state["full_review_raw_snippet"] = raw_snippet
    agent.save_state(state)

    return {
        "triggered": True,
        "issues": issues,
        "summary": summary,
        "parse_ok": parse_ok,
        "files": reviewed_files,
        "truncated": False,
    }


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


def sync_remote(cwd, project_name, remote_first=False):
    """git fetch origin 후 target_commit을 결정한다.

    remote_first=True : origin/{branch} HEAD 반환 (비파괴적, 로컬 미수정)
    remote_first=False: 로컬 HEAD 반환, 원격이 앞서면 경고만 출력

    반환: (target_commit: str, is_stale: bool)
    """
    local_head, _, _ = run_git(["rev-parse", "HEAD"], cwd=cwd)
    local_head = local_head.strip()

    _, err, code = run_git(["fetch", "origin"], cwd=cwd)
    if code != 0:
        print(f"[{project_name}] Warning: git fetch 실패: {err}")
        return local_head, False

    branch, _, _ = run_git(["branch", "--show-current"], cwd=cwd)
    branch = branch.strip()
    if not branch:
        print(f"[{project_name}] Warning: detached HEAD 상태, 원격 브랜치 확인 불가")
        return local_head, False

    remote_head, _, rcode = run_git(["rev-parse", f"origin/{branch}"], cwd=cwd)
    if rcode != 0:
        print(f"[{project_name}] Warning: origin/{branch} 없음")
        return local_head, False

    remote_head = remote_head.strip()
    is_stale = remote_head != local_head

    if remote_first:
        if is_stale:
            print(f"[{project_name}] remote_first: 원격 HEAD {remote_head[:8]} 기준 리뷰 (로컬은 {local_head[:8]})")
        return remote_head, is_stale
    else:
        if is_stale:
            print(
                f"[{project_name}] Warning: 로컬이 origin/{branch}보다 뒤처져 있습니다. "
                f"remote_first: true 설정 시 원격 변경사항을 리뷰할 수 있습니다."
            )
        return local_head, False


def build_json_directive(level_profile, lang, max_issues=None):
    n = max_issues if max_issues is not None else level_profile["max_issues"]
    directive = (
        "\n\nOutput ONLY valid JSON mapped to this exact struct:\n"
        '{"one_line_summary":"","top_issues":[{'
        '"title":"",'
        '"severity":"high|medium|low",'
        '"target_files":[""],'
        '"what_is_wrong":"",'
        '"suggested_action":"",'
        '"anchor":{"file":"","function":""}'
        '}]}'
        "\nReturn raw JSON only on a single line."
        "\nDo not use markdown fences."
        f"\nReturn at most {n} top_issues."
        f"\nKeep one_line_summary under {level_profile['summary_chars']} characters."
        "\nKeep each string field concise: 1 short sentence."
        "\nOmit fields that are not listed in the schema."
        "\nIf there is no meaningful issue, return an empty top_issues array."
    )
    if level_profile.get("strict_mode"):
        directive += (
            "\nDo not report an issue when nearby comments or current code explicitly justify the behavior."
            "\nTreat design-rationale comments as strong evidence unless the diff clearly breaks that contract."
            "\nDo not invent concurrency bugs from heuristic suspicion alone."
        )
    else:
        directive += (
            "\nPrefer concrete bugs first, but you may include medium-confidence risks if they are technically grounded."
        )
    if lang == "ko":
        directive += "\nALL text inside the JSON values MUST be written in Korean."
    else:
        directive += "\nALL text inside the JSON values MUST be written in English."
    return directive


def list_changed_files(base_ref, target_commit, cwd):
    out, _, code = run_git(["diff", "--name-only", base_ref, target_commit], cwd=cwd)
    if code != 0:
        return []
    return [f.strip() for f in out.splitlines() if f.strip()]


def select_review_files(changed_files, config, max_files=30):
    ldconf = config.get("large_diff_review", {})
    limit = ldconf.get("max_files", max_files)

    # test-only diff: _test.go를 skip-last로 보내지 않고 normal로 처리
    all_test_only = bool(changed_files) and all(_TEST_GO_RE.search(f) for f in changed_files)

    contract, high, normal, low = [], [], [], []
    for f in changed_files:
        if _TEST_GO_RE.search(f) and not all_test_only:
            low.append(f)
        elif any(p.search(f) for p in SKIP_PATTERNS):
            low.append(f)
        elif any(p.search(f) for p in CONTRACT_SENSITIVE_PATTERNS):
            contract.append(f)
        elif any(p.search(f) for p in HIGH_PRIORITY_PATTERNS):
            high.append(f)
        else:
            normal.append(f)

    # 우선순위: contract > high > normal > low(skip-last)
    ordered = contract + high + normal + low
    return ordered[:limit], ordered[limit:]


def get_file_diff(base_ref, target_commit, path, cwd, max_lines=500):
    out, _, code = run_git(["diff", base_ref, target_commit, "--", path], cwd=cwd)
    if code != 0:
        return "", False
    lines = out.splitlines()
    if len(lines) <= max_lines:
        return out, False
    return "\n".join(lines[:max_lines]), True


def run_cross_file_summary_pass(agent, config, env, lang, level_profile, file_summaries, omitted_files):
    """파일별 요약+이슈를 합산해 파일 간 상호작용 버그를 LLM으로 감지한다."""
    cross_template = env.get_template("prompts/review/crossfile.md.j2")
    rendered = cross_template.render(
        file_summaries=file_summaries,
        omitted_files=omitted_files,
        heuristics=agent.heuristics,
    )
    directive = build_json_directive(level_profile, lang, max_issues=2)
    ai_res = ask_llm(
        rendered + directive,
        config,
        max_tokens=config.get("llm", {}).get("review_max_tokens", 1400),
        response_format={"type": "json_object"},
    )
    try:
        data = json.loads(ai_res.replace("```json", "").replace("```", "").strip())
        cwd = agent.project_context.get("path", ".")
        issues = [
            iss for iss in data.get("top_issues", [])
            if not issue_looks_false_positive(iss, cwd)
        ]
        return {"issues": issues, "summary": data.get("one_line_summary", ""), "parse_ok": True}
    except Exception:
        return {"issues": [], "summary": "", "parse_ok": False}


def run_large_diff_review(agent, state, config, cwd, env, lang, level_profile, review_level, base_ref, target_commit):
    """큰 diff를 파일별로 분할 리뷰한다. None 반환 시 state가 이미 'skipped'로 설정됨."""
    ldconf = config.get("large_diff_review", {})
    max_lines_per_file = ldconf.get("max_lines_per_file", 500)
    cross_file_pass_enabled = ldconf.get("cross_file_pass", True)

    changed_files = list_changed_files(base_ref, target_commit, cwd)
    if not changed_files:
        state["status_p1_review"] = "skipped"
        state["error_message"] = "Large diff but no changed files detected."
        return None

    selected_files, omitted_files = select_review_files(changed_files, config)

    prompt_path = "prompts/review/generic.md.j2"
    for pt in getattr(agent, 'project_types', []):
        candidate = f"prompts/review/{pt}.md.j2"
        if os.path.exists(candidate):
            prompt_path = candidate
            break
    prompt_template = env.get_template(prompt_path)

    state["analysis_started_at"] = now_iso()
    agent.save_state(state)

    review_truncated = False
    partially_reviewed_files = []
    all_issues = []
    file_summaries = []

    for fpath in selected_files:
        file_diff, truncated = get_file_diff(base_ref, target_commit, fpath, cwd, max_lines_per_file)
        if not file_diff.strip():
            continue
        if truncated:
            review_truncated = True
            partially_reviewed_files.append(fpath)

        rendered = prompt_template.render(
            heuristics=agent.heuristics,
            diff_text=file_diff,
            review_context="",
            review_level=review_level,
        )
        directive = build_json_directive(level_profile, lang, max_issues=2)
        ai_res = ask_llm(
            rendered + directive,
            config,
            max_tokens=config.get("llm", {}).get("review_max_tokens", 1400),
            response_format={"type": "json_object"},
        )
        try:
            data = json.loads(ai_res.replace("```json", "").replace("```", "").strip())
            file_issues = [
                iss for iss in data.get("top_issues", [])
                if not issue_looks_false_positive(iss, cwd)
            ]
            file_summary = data.get("one_line_summary", "")
        except Exception:
            file_issues = []
            file_summary = ""

        all_issues.extend(file_issues)
        file_summaries.append({
            "file": fpath,
            "summary": file_summary,
            "issues": file_issues,
            "truncated": truncated,
        })

    state["analysis_finished_at"] = now_iso()
    state["analysis_duration_sec"] = elapsed_seconds(
        state.get("analysis_started_at"), state.get("analysis_finished_at")
    )

    # cross-file pass: truncated/omitted/multi-file/contract-sensitive 조건 중 하나라도 해당되면 실행
    has_contract_sensitive = any(
        any(p.search(fs["file"]) for p in CONTRACT_SENSITIVE_PATTERNS)
        for fs in file_summaries
    )
    should_cross_file = (
        review_truncated
        or bool(omitted_files)
        or len(file_summaries) >= 2
        or has_contract_sensitive
    )
    cross_issues = []
    if should_cross_file and cross_file_pass_enabled and file_summaries:
        cross_result = run_cross_file_summary_pass(
            agent, config, env, lang, level_profile, file_summaries, omitted_files
        )
        cross_issues = cross_result["issues"]
        all_issues.extend(cross_issues)

    # dedup: (정규화된 title[:60], 첫 번째 target_file) 복합 키 — title만으론 너무 공격적
    seen_keys = set()
    deduped = []
    for iss in all_issues:
        title_norm = iss.get("title", "").lower().strip()[:60]
        file0 = (iss.get("target_files") or [""])[0]
        key = (title_norm, file0)
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(iss)
    deduped.sort(key=lambda x: SEVERITY_ORDER.get(x.get("severity", "low"), 2))

    one_line_summary = next(
        (fs["summary"] for fs in file_summaries if fs.get("summary")), ""
    )

    # can_reconcile: truncated 또는 omitted 파일이 있으면 stale reconcile 금지
    can_reconcile = not review_truncated and not bool(omitted_files)
    skip_reasons = []
    if review_truncated:
        skip_reasons.append(f"{len(partially_reviewed_files)}개 파일 잘림")
    if omitted_files:
        skip_reasons.append(f"{len(omitted_files)}개 파일 미검토")
    last_reviewed_skip_reason = ", ".join(skip_reasons) if skip_reasons else None

    state["large_diff_files_reviewed"] = len(file_summaries)
    state["large_diff_files_omitted"] = len(omitted_files)
    state["large_diff_review_truncated"] = review_truncated
    state["large_diff_partially_reviewed"] = partially_reviewed_files
    state["large_diff_cross_file_pass"] = bool(cross_issues)
    state["one_line_summary"] = one_line_summary
    state["can_reconcile"] = can_reconcile
    if last_reviewed_skip_reason:
        state["last_reviewed_skip_reason"] = last_reviewed_skip_reason
    agent.save_state(state)

    return {
        "issues": deduped,
        "file_summaries": file_summaries,
        "review_truncated": review_truncated,
        "partially_reviewed_files": partially_reviewed_files,
        "omitted_files": omitted_files,
        "cross_issues": cross_issues,
        "one_line_summary": one_line_summary,
        "can_reconcile": can_reconcile,
        "last_reviewed_skip_reason": last_reviewed_skip_reason,
    }


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

        # 원격 동기화: 프로젝트 설정 > 글로벌 설정 > 기본값(False) 순으로 적용
        remote_first = agent.project_context.get(
            "remote_first", config.get("remote_first", False)
        )
        target_commit, is_stale = sync_remote(cwd, agent.project_name, remote_first=remote_first)

        state = agent.load_state()
        if state.get("status_p1_review") in ["success", "skipped", "failed"]:
            return

        # remote_first=True이고 원격이 앞서 있으면 target_commit을 원격 HEAD로 교체
        if is_stale:
            state["local_commit"] = state["target_commit"]
            state["target_commit"] = target_commit
            state["local_stale"] = True

        state["status_p1_review"] = "running"
        state["review_started_at"] = now_iso()
        agent.save_state(state)

        # 공통 설정 — diff/full 양쪽 경로에서 모두 필요
        lang = config.get("language", "ko")
        env = Environment(loader=FileSystemLoader('.'))
        review_level = resolve_review_level(agent, config)
        level_profile = review_level_profile(review_level)
        state["review_level"] = review_level

        # 1. Preflight Commands (Build, Test, Lint)
        detailed_test_results = collect_command_results(agent, cwd)

        # 2. Diff extraction
        diff_text, merge_base, err_msg = get_filtered_diff(agent, state, cwd)
        if err_msg or not diff_text.strip():
            can_full_review = (
                not err_msg and
                state.get("diff_base_mode") == "last_reviewed" and
                config.get("full_review_on_clean", True)
            )
            if can_full_review:
                full_result = run_full_code_review(agent, state, config, cwd, env, lang, level_profile)
                if full_result["triggered"]:
                    from agent_core import make_issue_record
                    run_issues = []
                    for iss in full_result["issues"]:
                        rec = make_issue_record(iss, agent.run_id, state["target_commit"])
                        rec["full_code_review"] = True
                        run_issues.append(rec)
                    issues_path = os.path.join(agent.get_run_dir(), "issues.json")
                    with open(issues_path, "w") as f:
                        json.dump(run_issues, f, indent=4, ensure_ascii=False)
                    state["issues_file"] = issues_path
                    report_template = env.get_template(f"templates/{lang}/report.md.j2")
                    report_path = os.path.join(agent.get_run_dir(), "review_report.md")
                    with open(report_path, "w") as f:
                        f.write(report_template.render(
                            project_name=agent.project_name,
                            current_date=state["created_at"],
                            target_branch=state["target_branch"],
                            target_commit=state["target_commit"],
                            merge_base=merge_base,
                            changed_files_count=0,
                            diff_lines_count=0,
                            status_review="Done" if full_result["parse_ok"] else "Degraded",
                            status_fix="Pending",
                            review_level=review_level,
                            review_started_at=state.get("review_started_at"),
                            review_finished_at=state.get("full_review_finished_at"),
                            review_duration_sec=state.get("full_review_duration_sec"),
                            analysis_started_at=state.get("full_review_started_at"),
                            analysis_finished_at=state.get("full_review_finished_at"),
                            analysis_duration_sec=state.get("full_review_duration_sec"),
                            report_started_at=state.get("full_review_finished_at"),
                            report_finished_at=state.get("full_review_finished_at"),
                            report_duration_sec=0,
                            one_line_summary="",
                            top_issues=[],
                            categorize={},
                            test_results=detailed_test_results,
                            llm_review={},
                            full_code_review_triggered=True,
                            full_review_issues=full_result["issues"],
                            full_review_summary=full_result["summary"],
                            full_review_started_at=state.get("full_review_started_at"),
                            full_review_finished_at=state.get("full_review_finished_at"),
                            full_review_duration_sec=state.get("full_review_duration_sec"),
                        ))
                    state["review_report_path"] = report_path
                    state["status_p1_review"] = "full_review_only"
                    state["review_finished_at"] = state.get("full_review_finished_at") or now_iso()
                    state["review_duration_sec"] = elapsed_seconds(
                        state.get("review_started_at"), state.get("review_finished_at")
                    )
                    agent.save_state(state)
                    if full_result["parse_ok"]:
                        save_last_reviewed(agent.project_name, state["target_commit"], state.get("review_finished_at"))
                else:
                    state["status_p1_review"] = "skipped"
                    state["error_message"] = "No diff and no source files for full review."
                    state["review_finished_at"] = now_iso()
                    state["review_duration_sec"] = elapsed_seconds(
                        state.get("review_started_at"), state.get("review_finished_at")
                    )
                    agent.save_state(state)
            else:
                state["status_p1_review"] = "skipped"
                state["error_message"] = err_msg or "No diff found."
                state["review_finished_at"] = now_iso()
                state["review_duration_sec"] = elapsed_seconds(
                    state.get("review_started_at"), state.get("review_finished_at")
                )
                agent.save_state(state)
            return

        diff_lines = len(diff_text.splitlines())
        if diff_lines > config.get("max_diff_lines", 2000):
            ldconf = config.get("large_diff_review", {})
            if not ldconf.get("enabled", True):
                state["review_finished_at"] = now_iso()
                state["review_duration_sec"] = elapsed_seconds(
                    state.get("review_started_at"), state.get("review_finished_at")
                )
                state["status_p1_review"] = "skipped"
                state["error_message"] = "Diff too large limit exceeded."
                agent.save_state(state)
                return

            large_result = run_large_diff_review(
                agent, state, config, cwd, env, lang, level_profile, review_level,
                merge_base, state["target_commit"]
            )
            if large_result is None:
                state["review_finished_at"] = now_iso()
                state["review_duration_sec"] = elapsed_seconds(
                    state.get("review_started_at"), state.get("review_finished_at")
                )
                agent.save_state(state)
                return

            from agent_core import make_issue_record
            run_issues = []
            for iss in large_result["issues"]:
                rec = make_issue_record(iss, agent.run_id, state["target_commit"])
                rec["large_diff_review"] = True
                run_issues.append(rec)
            issues_path = os.path.join(agent.get_run_dir(), "issues.json")
            with open(issues_path, "w") as f:
                json.dump(run_issues, f, indent=4, ensure_ascii=False)
            state["issues_file"] = issues_path

            state["report_started_at"] = now_iso()
            agent.save_state(state)

            report_template_ld = env.get_template(f"templates/{lang}/report.md.j2")
            report_path = os.path.join(agent.get_run_dir(), "review_report.md")
            with open(report_path, "w") as f:
                f.write(report_template_ld.render(
                    project_name=agent.project_name,
                    current_date=state["created_at"],
                    target_branch=state["target_branch"],
                    target_commit=state["target_commit"],
                    merge_base=merge_base,
                    changed_files_count=len(large_result["file_summaries"]),
                    diff_lines_count=diff_lines,
                    status_review="PartialReview",
                    status_fix="Pending",
                    review_level=review_level,
                    review_started_at=state.get("review_started_at"),
                    review_finished_at=state.get("review_finished_at"),
                    review_duration_sec=state.get("review_duration_sec"),
                    analysis_started_at=state.get("analysis_started_at"),
                    analysis_finished_at=state.get("analysis_finished_at"),
                    analysis_duration_sec=state.get("analysis_duration_sec"),
                    report_started_at=state.get("report_started_at"),
                    report_finished_at=state.get("report_finished_at"),
                    report_duration_sec=state.get("report_duration_sec"),
                    one_line_summary=large_result["one_line_summary"],
                    top_issues=large_result["issues"],
                    categorize={},
                    test_results=detailed_test_results,
                    llm_review={},
                    full_code_review_triggered=False,
                    full_review_issues=[],
                    full_review_summary="",
                    full_review_parse_ok=False,
                    full_review_started_at=None,
                    full_review_finished_at=None,
                    full_review_duration_sec=None,
                    large_diff_review_triggered=True,
                    large_diff_file_summaries=large_result["file_summaries"],
                    large_diff_omitted_files=large_result["omitted_files"],
                    large_diff_partially_reviewed=large_result["partially_reviewed_files"],
                    large_diff_cross_file_pass=bool(large_result["cross_issues"]),
                    large_diff_review_truncated=large_result["review_truncated"],
                ))

            state["review_report_path"] = report_path
            state["test_results"] = detailed_test_results
            state["report_finished_at"] = now_iso()
            state["report_duration_sec"] = elapsed_seconds(
                state.get("report_started_at"), state.get("report_finished_at")
            )
            state["review_finished_at"] = state["report_finished_at"]
            state["review_duration_sec"] = elapsed_seconds(
                state.get("review_started_at"), state.get("review_finished_at")
            )
            state["status_p1_review"] = "partial_large_diff_review"
            agent.save_state(state)

            if large_result["can_reconcile"]:
                save_last_reviewed(
                    agent.project_name, state["target_commit"], state.get("review_finished_at")
                )
            else:
                reason = large_result.get("last_reviewed_skip_reason", "부분 리뷰")
                print(
                    f"[{agent.project_name}] last_reviewed_commit 미갱신 ({reason}) "
                    f"— 다음 실행에서 reconcile 생략"
                )
            return

        state["merge_base"] = merge_base
        state["base_branch"] = agent.project_context.get('base_branch')

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
        
        json_directive = build_json_directive(level_profile, lang)

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

        # 전체 코드 리뷰: 증분 이슈가 없을 때 자동 실행
        full_code_review_triggered = False
        full_review_issues = []
        full_review_summary = ""
        full_review_parse_ok = False

        if (llm_parse_ok and
                not ai_data.get("top_issues") and
                state.get("diff_base_mode") == "last_reviewed" and
                config.get("full_review_on_clean", True)):
            full_result = run_full_code_review(agent, state, config, cwd, env, lang, level_profile)
            full_code_review_triggered = full_result["triggered"]
            full_review_issues = full_result["issues"]
            full_review_summary = full_result["summary"]
            full_review_parse_ok = full_result["parse_ok"]

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
                llm_review=ai_data.get("llm_review", {}),
                full_code_review_triggered=full_code_review_triggered,
                full_review_issues=full_review_issues,
                full_review_summary=full_review_summary,
                full_review_parse_ok=full_review_parse_ok,
                full_review_started_at=state.get("full_review_started_at"),
                full_review_finished_at=state.get("full_review_finished_at"),
                full_review_duration_sec=state.get("full_review_duration_sec"),
                large_diff_review_triggered=False,
                large_diff_file_summaries=[],
                large_diff_omitted_files=[],
                large_diff_partially_reviewed=[],
                large_diff_cross_file_pass=False,
                large_diff_review_truncated=False,
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
        for iss in full_review_issues:
            rec = make_issue_record(iss, agent.run_id, state["target_commit"])
            rec["full_code_review"] = True
            run_issues.append(rec)

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
        sys.exit(1)
    finally:
        agent.release_lock()

if __name__ == "__main__":
    main()
