#!/usr/bin/env python3
"""na — Nightly Agent CLI

사용법:
  na start              systemd 타이머 활성화 (매일 새벽 자동 실행)
  na stop               systemd 타이머 비활성화
  na scan               scan_paths 탐색 → 대화형 프로젝트 등록
  na add <GitHub URL>   GitHub 저장소 클론 후 자동 등록
  na config             GitHub 저장소/토큰 및 clone_roots 설정
  na model              현재 모델과 설치된 Ollama 모델 조회
  na model <name>       현재 LLM 모델 변경 (설치된 Ollama 모델만 허용)
  na model tune <alias> [base_model]  배치용 Ollama alias 생성
"""
import os
import re
import sys
import json
import subprocess
import yaml
import requests

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
PROJECTS_FILE = os.path.join(os.path.dirname(__file__), "configs", "projects.yaml")
SERVICE_NAME = "nightly-agent"

TYPE_MARKERS = {
    "go":         ["go.mod"],
    "rust":       ["Cargo.toml"],
    "dotnet":     [".csproj", ".sln"],
    "typescript": ["package.json", "tsconfig.json"],
    "python":     ["pyproject.toml", "setup.py", "requirements.txt"],
    "infra":      [".tf", ".hcl"],
    "shell":      [".sh"],
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_projects():
    if os.path.exists(PROJECTS_FILE):
        with open(PROJECTS_FILE) as f:
            data = yaml.safe_load(f) or {}
            return data.get("projects", [])
    return []


def save_projects(projects):
    with open(PROJECTS_FILE, "w") as f:
        yaml.dump({"projects": projects}, f, allow_unicode=True, default_flow_style=False)


def detect_types(path):
    """파일 목록으로 프로젝트 타입 목록 감지."""
    try:
        entries = os.listdir(path)
    except PermissionError:
        return []
    detected = []
    for ptype, markers in TYPE_MARKERS.items():
        if any(any(e == m or e.endswith(m) for e in entries) for m in markers):
            detected.append(ptype)
    return detected


def cmd_start():
    ret = subprocess.run(["systemctl", "start", f"{SERVICE_NAME}.timer"])
    if ret.returncode == 0:
        print(f"Nightly Agent 시작됨. (매일 새벽 자동 실행)")
    else:
        print(f"시작 실패. sudo 권한이 필요할 수 있습니다: sudo systemctl start {SERVICE_NAME}.timer")


def cmd_stop():
    ret = subprocess.run(["systemctl", "stop", f"{SERVICE_NAME}.timer"])
    if ret.returncode == 0:
        print("Nightly Agent 중지됨.")
    else:
        print(f"중지 실패. sudo 권한이 필요할 수 있습니다: sudo systemctl stop {SERVICE_NAME}.timer")


def _parse_github_url(url):
    """GitHub URL에서 (owner, repo) 추출. 실패 시 None 반환."""
    url = url.strip().rstrip("/")
    # https://github.com/owner/repo  or  git@github.com:owner/repo
    patterns = [
        r"https?://github\.com/([^/]+)/([^/\.]+?)(?:\.git)?$",
        r"git@github\.com:([^/]+)/([^/\.]+?)(?:\.git)?$",
    ]
    for pat in patterns:
        m = re.match(pat, url)
        if m:
            return m.group(1), m.group(2)
    return None


def _infer_clone_root(ptype, cfg):
    """config의 clone_roots에서 타입별 기본 경로 반환."""
    clone_roots = cfg.get("clone_roots", {})
    return clone_roots.get(ptype)


def _register_project(path, name, github_url, projects):
    """projects 리스트에 프로젝트 레코드를 추가하고 저장한다."""
    entry = {
        "name": name,
        "path": path,
        "base_branch": "main",
        "github_url": github_url,
        "review": {"include": [], "exclude": []},
        "commands": {},
    }
    projects.append(entry)
    save_projects(projects)
    return entry


def cmd_add(url):
    parsed = _parse_github_url(url)
    if not parsed:
        print(f"유효하지 않은 GitHub URL: {url}")
        print("형식 예: https://github.com/owner/repo")
        sys.exit(1)

    owner, repo = parsed
    cfg = load_config()
    projects = load_projects()
    registered_paths = {p.get("path") for p in projects}

    # 임시 경로로 클론해서 타입 감지 후 올바른 위치로 이동
    # 1단계: 타입 먼저 감지하기 위해 임시 클론
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = os.path.join(tmp_dir, repo)
        print(f"클론 중: {url} → {tmp_path} (타입 감지용)")
        ret = subprocess.run(["git", "clone", "--depth=1", url, tmp_path])
        if ret.returncode != 0:
            print("클론 실패.")
            sys.exit(1)
        ptypes = detect_types(tmp_path)
        primary_type = ptypes[0] if ptypes else "shell"

    clone_root = _infer_clone_root(primary_type, cfg)
    if not clone_root:
        print(f"clone_roots에 '{primary_type}' 타입 경로가 설정되지 않았습니다.")
        print("config.json의 clone_roots를 확인해주세요.")
        sys.exit(1)

    dest = os.path.join(clone_root, owner, repo)
    if dest in registered_paths:
        print(f"이미 등록된 경로입니다: {dest}")
        sys.exit(0)

    if os.path.exists(dest):
        print(f"디렉토리가 이미 존재합니다: {dest}")
        print("해당 경로를 그대로 사용합니다.")
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        print(f"클론 중: {url} → {dest}")
        ret = subprocess.run(["git", "clone", url, dest])
        if ret.returncode != 0:
            print("클론 실패.")
            sys.exit(1)

    _register_project(dest, repo, url, projects)
    print(f"\n✅ [{repo}] 등록 완료")
    print(f"   감지된 언어: {', '.join(ptypes) or '불명'}")
    print(f"   경로: {dest}")
    print(f"   GitHub: {url}")


def cmd_scan():
    cfg = load_config()
    scan_paths = cfg.get("scan_paths", [])
    if not scan_paths:
        print("config.json에 scan_paths가 설정되지 않았습니다.")
        sys.exit(1)

    existing_projects = load_projects()
    registered_paths = {p.get("path") for p in existing_projects}

    detected = []
    for base in scan_paths:
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            full_path = os.path.join(base, name)
            if not os.path.isdir(full_path):
                continue
            ptypes = detect_types(full_path)
            if not ptypes:
                continue
            detected.append({
                "name": name,
                "path": full_path,
                "types": ptypes,
                "registered": full_path in registered_paths,
            })

    if not detected:
        print("감지된 프로젝트 없음.")
        return

    print("\n감지된 프로젝트:")
    for i, p in enumerate(detected, 1):
        status = "✅ 이미 등록됨" if p["registered"] else "➕ 미등록"
        types_str = ",".join(p["types"])
        print(f"  [{i}] {p['name']:<20} {p['path']:<55} ({types_str:<16}) {status}")

    unregistered = [p for p in detected if not p["registered"]]
    if not unregistered:
        print("\n모든 프로젝트가 이미 등록되어 있습니다.")
        return

    print()
    answer = input("등록할 프로젝트 번호 입력 (예: 3, 1 3 5 또는 all): ").strip()
    if not answer:
        print("취소됨.")
        return

    indices = set()
    if answer.lower() == "all":
        indices = set(range(1, len(detected) + 1))
    else:
        for tok in answer.split():
            try:
                indices.add(int(tok))
            except ValueError:
                pass

    added = 0
    for i, p in enumerate(detected, 1):
        if i not in indices:
            continue
        if p["registered"]:
            print(f"  [{p['name']}] 이미 등록됨 — 스킵")
            continue

        # GitHub URL 입력 (선택)
        github_url = input(
            f"  [{p['name']}] GitHub URL (없으면 Enter 스킵): "
        ).strip()

        # git remote에서 자동 감지 시도
        if not github_url:
            try:
                result = subprocess.run(
                    ["git", "-C", p["path"], "remote", "get-url", "origin"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    candidate = result.stdout.strip()
                    if _parse_github_url(candidate):
                        github_url = candidate
                        print(f"    remote에서 자동 감지: {github_url}")
            except Exception:
                pass

        existing_projects.append({
            "name": p["name"],
            "path": p["path"],
            "base_branch": "main",
            "github_url": github_url or "",
            "review": {"include": [], "exclude": []},
            "commands": {},
        })
        print(f"  ✅ {p['name']} 등록 완료")
        added += 1

    if added:
        save_projects(existing_projects)
        print(f"\nprojects.yaml 업데이트 완료 ({added}개 추가)")


def cmd_config():
    cfg = load_config()
    llm = cfg.get("llm", {})
    gh = cfg.get("github", {})

    print("\nGitHub 설정")
    print("-----------")
    current_repo = gh.get("reports_repo", "")
    current_token = gh.get("token", "")

    repo = input(f"리포트 저장소 [{current_repo or '예: HeaInSeo/nightly-agent-reports'}]: ").strip()
    token = input(f"GitHub 토큰 [{('설정됨' if current_token else '미설정')}]: ").strip()

    if repo:
        gh["reports_repo"] = repo
    if token:
        gh["token"] = token
    cfg["github"] = gh

    print("\nLLM 설정")
    print("--------")
    current_model = llm.get("model_name", "")
    current_timeout = llm.get("timeout_seconds", 900)
    current_reasoning = llm.get("reasoning_effort", "none")
    current_disable_thinking = llm.get("disable_thinking", True)
    current_review_max = llm.get("review_max_tokens", 1400)
    current_continuity_max = llm.get("continuity_max_tokens", 600)
    current_fix_max = llm.get("fix_max_tokens", 2400)

    model_name = input(f"모델명 [{current_model or 'qwen3.6:27b'}]: ").strip()
    timeout_value = input(f"요청 타임아웃 초 [{current_timeout}]: ").strip()
    reasoning = input(f"reasoning_effort [{current_reasoning}]: ").strip()
    disable_thinking = input(
        f"thinking 비활성화(true/false) [{'true' if current_disable_thinking else 'false'}]: "
    ).strip().lower()
    review_max = input(f"리뷰 max_tokens [{current_review_max}]: ").strip()
    continuity_max = input(f"연속성 체크 max_tokens [{current_continuity_max}]: ").strip()
    fix_max = input(f"패치 생성 max_tokens [{current_fix_max}]: ").strip()

    if model_name:
        llm["model_name"] = model_name
    if timeout_value:
        llm["timeout_seconds"] = int(timeout_value)
    if reasoning:
        llm["reasoning_effort"] = reasoning
    if disable_thinking in ("true", "false"):
        llm["disable_thinking"] = (disable_thinking == "true")
    if review_max:
        llm["review_max_tokens"] = int(review_max)
    if continuity_max:
        llm["continuity_max_tokens"] = int(continuity_max)
    if fix_max:
        llm["fix_max_tokens"] = int(fix_max)
    cfg["llm"] = llm

    # clone_roots 설정
    print("\nclone_roots 설정 (타입별 클론 기본 경로, Enter로 현재값 유지)")
    print("------------------------------------------------------------------")
    clone_roots = cfg.get("clone_roots", {})
    for ptype in ["go", "rust", "dotnet", "typescript", "python", "shell", "infra"]:
        current = clone_roots.get(ptype, "")
        val = input(f"  {ptype:<12} [{current or '미설정'}]: ").strip()
        if val:
            clone_roots[ptype] = val
    cfg["clone_roots"] = clone_roots

    save_config(cfg)
    print("\n✅ config.json 업데이트 완료")


def _fetch_installed_ollama_models(cfg):
    llm = cfg.get("llm", {})
    base_url = llm.get("api_base_url", "http://localhost:11434/v1").rstrip("/")
    root_url = base_url[:-3] if base_url.endswith("/v1") else base_url
    resp = requests.get(f"{root_url}/api/tags", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def _resolve_installed_model_name(requested_name, installed_models):
    """Ollama의 :latest suffix를 감안해 실제 설치된 모델명을 찾는다."""
    if requested_name in installed_models:
        return requested_name
    latest_name = f"{requested_name}:latest"
    if latest_name in installed_models:
        return latest_name
    return None


def cmd_model(argv):
    cfg = load_config()
    llm = cfg.setdefault("llm", {})
    current = llm.get("model_name", "")

    try:
        installed = _fetch_installed_ollama_models(cfg)
    except Exception as e:
        print(f"Ollama 모델 조회 실패: {e}")
        sys.exit(1)

    if len(argv) >= 1 and argv[0] == "tune":
        if len(argv) < 2:
            print("사용법: na model tune <alias> [base_model]")
            sys.exit(1)
        alias = argv[1].strip()
        if not alias:
            print("alias가 비어 있습니다.")
            sys.exit(1)
        base_model = argv[2].strip() if len(argv) >= 3 else (current or "qwen3.6:27b")
        resolved_base_model = _resolve_installed_model_name(base_model, installed)
        if not resolved_base_model:
            print(f"설치되지 않은 base_model입니다: {base_model}")
            sys.exit(1)

        modelfile = "\n".join([
            f"FROM {resolved_base_model}",
            "PARAMETER num_ctx 8192",
            "PARAMETER num_predict 2048",
            ""
        ])
        os.makedirs(os.path.join(os.path.dirname(__file__), ".nightly_agent", "modelfiles"), exist_ok=True)
        modelfile_path = os.path.join(
            os.path.dirname(__file__), ".nightly_agent", "modelfiles", f"{alias}.Modelfile"
        )
        with open(modelfile_path, "w") as f:
            f.write(modelfile)

        print(f"Modelfile 작성 완료: {modelfile_path}")
        result = subprocess.run(["ollama", "create", alias, "-f", modelfile_path])
        if result.returncode != 0:
            print("ollama create 실패.")
            sys.exit(1)
        print(f"Ollama alias 생성 완료: {alias}")
        print("필요하면 다음 명령으로 현재 모델을 전환하세요:")
        print(f"  na model {alias}")
        return

    if len(argv) == 0:
        print(f"현재 모델: {current or '(미설정)'}")
        if installed:
            print("설치된 Ollama 모델:")
            for name in installed:
                marker = " (current)" if name == current else ""
                print(f"  - {name}{marker}")
        else:
            print("설치된 Ollama 모델이 없습니다.")
        return

    model_name = argv[0].strip()
    if not model_name:
        print("모델명이 비어 있습니다.")
        sys.exit(1)
    resolved_model_name = _resolve_installed_model_name(model_name, installed)
    if not resolved_model_name:
        print(f"설치되지 않은 모델입니다: {model_name}")
        print("먼저 Ollama에 모델을 설치한 뒤 다시 시도하세요.")
        print("예: ollama pull <model>")
        if installed:
            print("현재 설치된 모델:")
            for name in installed:
                print(f"  - {name}")
        sys.exit(1)

    llm["model_name"] = resolved_model_name
    cfg["llm"] = llm
    save_config(cfg)
    print(f"LLM 모델 변경 완료: {resolved_model_name}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd == "start":
        cmd_start()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "scan":
        cmd_scan()
    elif cmd == "add":
        if len(sys.argv) < 3:
            print("사용법: na add <GitHub URL>")
            sys.exit(1)
        cmd_add(sys.argv[2])
    elif cmd == "config":
        cmd_config()
    elif cmd == "model":
        cmd_model(sys.argv[2:])
    else:
        print(f"알 수 없는 명령: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
