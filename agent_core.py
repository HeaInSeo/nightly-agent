import os
import json
import yaml
import fcntl
import datetime
import subprocess
import argparse

AGENT_DIR = ".nightly_agent"
CONFIG_FILE = "config.json"
PROJECTS_FILE = "configs/projects.yaml"

def run_cmd(cmd, cwd="."):
    result = subprocess.run(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return result.stdout.strip(), result.stderr.strip(), result.returncode

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
                
            p_type = self.project_context.get('type')
            profile_path = f"configs/profiles/{p_type}.yaml"
            profile = {}
            if os.path.exists(profile_path):
                with open(profile_path, "r") as f:
                    profile = yaml.safe_load(f) or {}
                    
            self.merged_commands = profile.get("default_commands", {}).copy()
            self.merged_commands.update(self.project_context.get("commands", {}))
            self.heuristics = profile.get("heuristics", [])

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

    def generate_run_id(self):
        return datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

    def load_global_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        return {"max_diff_lines": 2000, "max_retries": 3, "ollama_url": "http://localhost:11434/api/generate", "model_name": "gemma4:26b"}

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
        head, _, _ = run_cmd("git rev-parse HEAD", cwd=cwd)
        branch, _, _ = run_cmd("git branch --show-current", cwd=cwd)
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
        
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="Target project name in registry", required=False)
    parser.add_argument("--run-id", help="Override standard run_id", required=False)
    return parser.parse_args()
