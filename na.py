#!/usr/bin/env python3
"""na — Nightly Agent CLI

사용법:
  na start       systemd 타이머 활성화 (매일 새벽 자동 실행)
  na stop        systemd 타이머 비활성화
  na scan        scan_paths 탐색 → 대화형 프로젝트 등록
  na config      GitHub 저장소/토큰 설정
"""
import os
import sys
import json
import subprocess
import yaml

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


def detect_type(path):
    """파일 목록으로 프로젝트 타입 감지."""
    try:
        entries = os.listdir(path)
    except PermissionError:
        return None
    for ptype, markers in TYPE_MARKERS.items():
        for marker in markers:
            if any(e == marker or e.endswith(marker) for e in entries):
                return ptype
    return None


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
            ptype = detect_type(full_path)
            if not ptype:
                continue
            detected.append({
                "name": name,
                "path": full_path,
                "type": ptype,
                "registered": full_path in registered_paths,
            })

    if not detected:
        print("감지된 프로젝트 없음.")
        return

    print("\n감지된 프로젝트:")
    for i, p in enumerate(detected, 1):
        status = "✅ 이미 등록됨" if p["registered"] else "➕ 미등록"
        print(f"  [{i}] {p['name']:<20} {p['path']:<55} ({p['type']:<12}) {status}")

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
        existing_projects.append({
            "name": p["name"],
            "path": p["path"],
            "type": p["type"],
            "base_branch": "main",
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
    save_config(cfg)
    print("\n✅ config.json 업데이트 완료")


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
    elif cmd == "config":
        cmd_config()
    else:
        print(f"알 수 없는 명령: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
