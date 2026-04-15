# Nightly Agent

Automated code review & fix pipeline utilizing Gemma 4 and autonomous sandboxed agents.

## Architecture & Concepts

This repository establishes a highly safe, production-grade **Time-Triggered + State-Based** CI-like loop for unsupervised overnight code operations.

Instead of infinitely modifying your primary workspace, it works in 3 highly-isolated phases.

### 1. Phase 1 (`1_nightly_review.py`)
- **Action**: Extracts explicit base diffs via `git diff master...HEAD`.
- **Validation**: Checks limits via `config.json` (e.g. `max_diff_lines` > 2000 degrades gracefully to skip fix).
- **Output**: Generates a human-readable `review_report.md` and machine-readable `issues.json`.

### 2. Phase 2 (`2_nightly_fix_candidate.py`)
- **Action**: Generates a completely isolated `git worktree` named `auto-fix-{run_id}`.
- **Validation**:
  - Employs bounded-retry (default: max 3).
  - Uses `git apply --check` absolute candidate paths before altering files.
- **Output**: Pure patches targeting `best_patch_path` in `state.json` without pushing unapproved code natively.
- **Safety**: Robust trapping (`finally`) ensures the temporary sandbox is immediately destroyed.

### 3. Phase 3 (`3_morning_summary.py`)
- **Action**: Queries the latest run (resolving edge-cases where the developer pushes across a midnight-boundary).
- **Output**: Summarizes state enums, error logs, issues count, and patch locations into `summary.md`.

## Setup
1. Adjust `config.json` to suit your primary base branch or test command.
2. Ensure Ollama is configured on your server (NOTE: `setup-ollama.sh` is an Ubuntu/Linux targeted helper script assuming `systemctl` and `sudo` capabilities).
3. Setup standard crontabs pointing to the 3 phased py scripts (e.g. at 22:00, 23:00, and 07:00).
4. Manually call `./show_morning_summary.sh` to check what happened overnight.
