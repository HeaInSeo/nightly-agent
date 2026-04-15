import os
import json
import fcntl
import datetime
import subprocess

AGENT_DIR = ".nightly_agent"

def run_cmd(cmd, cwd="."):
    result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip(), result.stderr.strip(), result.returncode

class AgentState:
    def __init__(self, run_id=None):
        self.run_id = run_id or datetime.date.today().isoformat()
        self.run_dir = os.path.join(AGENT_DIR, "runs", self.run_id)
        self.state_file = os.path.join(self.run_dir, "state.json")
        self.lock_file = os.path.join(self.run_dir, "run.lock")
        self.lock_fd = None
        os.makedirs(self.run_dir, exist_ok=True)

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
        head, _, _ = run_cmd("git rev-parse HEAD")
        branch, _, _ = run_cmd("git branch --show-current")
        return head, branch

    def load_state(self):
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                return json.load(f)
        head, branch = self.get_git_info()
        return {
            "run_id": self.run_id,
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
        return self.run_dir
