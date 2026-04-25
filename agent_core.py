import os
import json
import yaml
import fcntl
import uuid
import datetime
import subprocess
import argparse
import requests
from urllib.parse import urlparse

AGENT_DIR = ".nightly_agent"
CONFIG_FILE = "config.json"
PROJECTS_FILE = "configs/projects.yaml"

def run_cmd(cmd, cwd="."):
    """빌드/테스트/린트 등 셸 기능(glob, pipe)이 필요한 명령용."""
    result = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return result.stdout.strip(), result.stderr.strip(), result.returncode

def run_git(args, cwd="."):
    """git 명령 전용. list argv 방식으로 shell injection 없음.
    worktree_path, patch_path 등 공백 포함 경로도 안전하게 처리된다."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode

def parse_hour(value):
    """시간 값을 0-23 정수로 변환한다.

    지원 형식:
      - 정수 0-23   : 24시간제 그대로 사용  (예: 2, 14)
      - "2am"/"2AM" : 오전 2시 → 2
      - "8pm"/"8PM" : 오후 8시 → 20
      - "12am"      : 자정     → 0
      - "12pm"      : 정오     → 12
    None 입력 시 None 반환 (마감 없음 처리용).
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    import re
    m = re.match(r'^(\d{1,2})(am|pm)$', str(value).lower().strip())
    if m:
        h, ap = int(m.group(1)), m.group(2)
        if ap == 'pm' and h != 12:
            h += 12
        elif ap == 'am' and h == 12:
            h = 0
        return h
    return int(value)

def ask_llm(prompt, config):
    """OpenAI 호환 API로 LLM에 프롬프트를 전송하고 응답 문자열을 반환한다.
    Ollama, vLLM, TGI, SGLang 등 OpenAI 호환 엔진 모두 지원."""
    llm_conf = config.get("llm", {})
    base_url = llm_conf.get("api_base_url", "http://localhost:11434/v1").rstrip("/")
    model = llm_conf.get("model_name", "qwen3.6:27b")
    api_key = llm_conf.get("api_key", "") or "ollama"
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": 0.2,
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=600)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"LLM API 호출 실패: {e}")


def check_ollama(ollama_url_or_config, timeout=5):
    """LLM 서비스가 응답하는지 확인한다.
    config dict 또는 URL 문자열 모두 허용한다."""
    if isinstance(ollama_url_or_config, dict):
        base = ollama_url_or_config.get("llm", {}).get("api_base_url", "http://localhost:11434/v1")
    else:
        base = ollama_url_or_config
    try:
        parsed = urlparse(base)
        health_url = f"{parsed.scheme}://{parsed.netloc}/api/tags"
        resp = requests.get(health_url, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False

def notify_discord(webhook_url, content):
    """Discord webhook으로 메시지를 전송한다.
    webhook_url이 비어 있으면 조용히 건너뛴다. 성공 여부를 bool로 반환한다."""
    if not webhook_url:
        return False
    if len(content) > 2000:
        content = content[:1997] + "..."
    try:
        resp = requests.post(webhook_url, json={"content": content}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Discord 전송 실패: {e}")
        return False

class AgentState:
    def __init__(self, project_name=None, run_id=None):
        self.project_name = project_name
        self.config = self.load_global_config()
        self.projects_registry = self.load_projects()
        
        self.project_context = None
        self.merged_commands = {}
        self.heuristics = []
        
        if project_name:
            self.project_context = next((p for p in self.projects_registry.get('projects', []) if p['name'] == project_name), None)
            if not self.project_context:
                raise ValueError(f"Project {project_name} not found in configs/projects.yaml")
                
            # 타입은 프로젝트 경로에서 런타임 자동 감지 (YAML에 type 필드 불필요)
            p_types = self._detect_types(self.project_context.get('path', ''))

            self.merged_commands = {}
            seen_heuristics = []
            for pt in p_types:
                profile_path = f"configs/profiles/{pt}.yaml"
                if not os.path.exists(profile_path):
                    continue
                with open(profile_path, "r") as f:
                    profile = yaml.safe_load(f) or {}
                for k, v in profile.get("default_commands", {}).items():
                    self.merged_commands.setdefault(k, v)
                for h in profile.get("heuristics", []):
                    if h not in seen_heuristics:
                        seen_heuristics.append(h)
            self.merged_commands.update(self.project_context.get("commands", {}))
            self.heuristics = seen_heuristics
            self.project_types = p_types

        if run_id == "latest":
            runs_dir = os.path.join(AGENT_DIR, "runs")
            if os.path.exists(runs_dir):
                runs = sorted(os.listdir(runs_dir), reverse=True)
                self.run_id = runs[0] if runs else self.generate_run_id()
            else:
                self.run_id = self.generate_run_id()
        else:
            self.run_id = run_id or self.generate_run_id()

        self.run_dir = os.path.abspath(os.path.join(AGENT_DIR, "runs", self.run_id))
        
        if self.project_name:
            self.project_run_dir = os.path.join(self.run_dir, self.project_name)
        else:
            self.project_run_dir = self.run_dir
            
        self.state_file = os.path.join(self.project_run_dir, "state.json")
        self.lock_file = os.path.join(self.project_run_dir, "run.lock")
        self.lock_fd = None
        os.makedirs(self.project_run_dir, exist_ok=True)

    _TYPE_MARKERS = {
        "go":         ["go.mod"],
        "rust":       ["Cargo.toml"],
        "dotnet":     [".csproj", ".sln"],
        "typescript": ["package.json", "tsconfig.json"],
        "python":     ["pyproject.toml", "setup.py", "requirements.txt"],
        "infra":      [".tf", ".hcl"],
        "shell":      [".sh"],
    }

    def _detect_types(self, path):
        """프로젝트 경로에서 파일 목록을 보고 해당하는 모든 타입을 반환한다."""
        try:
            entries = os.listdir(path)
        except (PermissionError, FileNotFoundError):
            return []
        detected = []
        for ptype, markers in self._TYPE_MARKERS.items():
            if any(any(e == m or e.endswith(m) for e in entries) for m in markers):
                detected.append(ptype)
        return detected

    def generate_run_id(self):
        return datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

    def load_global_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        return {
            "llm": {"api_base_url": "http://localhost:11434/v1", "model_name": "qwen3.6:27b", "api_key": ""},
            "max_diff_lines": 2000,
            "max_retries": 3,
            "github": {"token": "", "reports_repo": ""},
        }

    def load_projects(self):
        if os.path.exists(PROJECTS_FILE):
            with open(PROJECTS_FILE, "r") as f:
                return yaml.safe_load(f) or {}
        return {"projects": []}

    def acquire_lock(self):
        self.lock_fd = open(self.lock_file, "w")
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.lock_fd.close()
            return False

    def release_lock(self):
        if self.lock_fd:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            self.lock_fd.close()

    def get_git_info(self):
        cwd = self.project_context['path'] if self.project_context else "."
        head, _, _ = run_git(["rev-parse", "HEAD"], cwd=cwd)
        branch, _, _ = run_git(["branch", "--show-current"], cwd=cwd)
        return head, branch

    def load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                return json.load(f)
        head, branch = self.get_git_info()
        return {
            "run_id": self.run_id,
            "project_name": self.project_name,
            "created_at": datetime.datetime.now().isoformat(),
            "target_commit": head,
            "target_branch": branch,
            "status_p1_review": "pending",
            "status_p2_fix": "pending"
        }

    def save_state(self, state):
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=4)
        
    def get_run_dir(self):
        return self.project_run_dir
        
def make_issue_record(llm_issue, run_id, commit):
    """LLM이 반환한 이슈 dict를 UUID + anchor 포함 표준 스키마로 변환한다."""
    return {
        "id": str(uuid.uuid4())[:8],
        "title": llm_issue.get("title", ""),
        "severity": llm_issue.get("severity", "low"),
        "target_files": llm_issue.get("target_files", []),
        "anchor": llm_issue.get("anchor", {}),
        "what_is_wrong": llm_issue.get("what_is_wrong", ""),
        "why_dangerous": llm_issue.get("why_dangerous", ""),
        "suggested_action": llm_issue.get("suggested_action", ""),
        "status": "open",
        "first_seen_run": run_id,
        "first_seen_date": datetime.datetime.now().strftime("%Y-%m-%d"),
        "first_seen_commit": commit,
        "last_seen_date": datetime.datetime.now().strftime("%Y-%m-%d"),
        "days_elapsed": 1,
        "parent_id": None,
    }


ISSUES_DB_DIR = os.path.join(AGENT_DIR, "issues_db")


def load_issues_db(project_name):
    """프로젝트별 누적 이슈 DB를 로드한다. 없으면 빈 리스트 반환."""
    os.makedirs(ISSUES_DB_DIR, exist_ok=True)
    db_path = os.path.join(ISSUES_DB_DIR, f"{project_name}.json")
    if os.path.exists(db_path):
        with open(db_path, "r") as f:
            return json.load(f)
    return []


def save_issues_db(project_name, issues):
    """프로젝트별 누적 이슈 DB를 저장한다."""
    os.makedirs(ISSUES_DB_DIR, exist_ok=True)
    db_path = os.path.join(ISSUES_DB_DIR, f"{project_name}.json")
    with open(db_path, "w") as f:
        json.dump(issues, f, indent=4, ensure_ascii=False)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="Target project name in registry", required=False)
    parser.add_argument("--run-id", help="Override standard run_id", required=False)
    return parser.parse_args()
