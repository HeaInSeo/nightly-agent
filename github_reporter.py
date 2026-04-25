"""GitHub 리포팅 모듈

issues_db/{project}.json → reports/{project}.md 렌더링 후
GitHub reports_repo에 push한다. 포맷은 Jinja2 템플릿으로 고정.
"""
import os
import json
import datetime
from jinja2 import Environment, FileSystemLoader
from agent_core import load_issues_db, AGENT_DIR


REPORT_TEMPLATE = """\
# {{ project_name }} — 나이틀리 리뷰 리포트
**마지막 갱신**: {{ last_updated }} · **모델**: {{ model_name }}
**브랜치**: {{ branch }} · **분석 커밋**: `{{ commit }}`

---

## 활성 이슈 ({{ active_issues | length }}건)
{% if not active_issues %}
발견된 활성 이슈 없음.
{% endif %}
{% for iss in active_issues %}
---

### {{ severity_emoji(iss.severity) }} [{{ iss.severity | upper }}] `#{{ iss.id }}` {{ iss.title }}{% if iss.status == 'derived' %} (파생){% endif %}

**상태**: {% if iss.status == 'recurring' %}반복 중{% elif iss.status == 'derived' %}파생 이슈{% else %}신규{% endif %} · **경과**: {{ iss.days_elapsed }}일째 (최초 발견: {{ iss.first_seen_date }}){% if iss.parent_id %} · **원본**: `#{{ iss.parent_id }}`{% endif %}

| 항목 | 내용 |
|------|------|
| 파일 | `{{ iss.anchor.file }}` |
| 함수 | `{{ iss.anchor.function }}` |
| 발견 커밋 | `{{ iss.first_seen_commit[:8] if iss.first_seen_commit else '-' }}` |
| 문제 코드 | {{ iss.anchor.snippet | replace('\n', '<br>') | replace('`', '\\`') | trim }} |
| 무엇이 문제인가 | {{ iss.what_is_wrong }} |
| 왜 위험한가 | {{ iss.why_dangerous }} |
| 권고 | {{ iss.suggested_action }} |

{% endfor %}

---

## 해결된 이슈 ({{ resolved_issues | length }}건)
{% if not resolved_issues %}
아직 해결된 이슈 없음.
{% else %}

| 날짜 | ID | 이슈 | 파일 | 함수 | 심각도 | 해결 여부 |
|------|----|------|------|------|--------|----------|
{% for iss in resolved_issues %}
| {{ iss.resolved_date }} | `#{{ iss.id }}` | {{ iss.title }} | `{{ iss.anchor.file }}` | `{{ iss.anchor.function }}` | {{ severity_emoji(iss.severity) }} {{ iss.severity | upper }} | ✅ 해결됨 |
{% endfor %}
{% endif %}
"""


def severity_emoji(severity):
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(severity.lower(), "⚪")


def render_report(project_name, model_name, branch, commit):
    """issues_db를 읽어 고정 포맷 마크다운을 렌더링해 반환한다."""
    issues = load_issues_db(project_name)
    active = [i for i in issues if i.get("status") in ("open", "recurring", "derived")]
    resolved = [i for i in issues if i.get("status") == "resolved"]

    # severity 기준 정렬 (high → medium → low), 같은 severity면 days_elapsed 내림차순
    sev_order = {"high": 0, "medium": 1, "low": 2}
    active.sort(key=lambda x: (sev_order.get(x.get("severity", "low"), 2), -x.get("days_elapsed", 0)))
    resolved.sort(key=lambda x: x.get("resolved_date", ""), reverse=True)

    from jinja2 import Environment
    env = Environment()
    env.globals["severity_emoji"] = severity_emoji
    tmpl = env.from_string(REPORT_TEMPLATE)
    return tmpl.render(
        project_name=project_name,
        last_updated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        model_name=model_name,
        branch=branch,
        commit=commit,
        active_issues=active,
        resolved_issues=resolved,
    )


def push_reports(config, run_states):
    """모든 프로젝트의 리포트를 GitHub에 push한다.

    run_states: {project_name: state_dict} 형태.
    """
    gh_conf = config.get("github", {})
    token = gh_conf.get("token", "").strip()
    repo_name = gh_conf.get("reports_repo", "").strip()
    model_name = config.get("llm", {}).get("model_name", "unknown")

    if not token or not repo_name:
        print("GitHub token 또는 reports_repo 미설정. 리포팅 스킵.")
        return

    try:
        from github import Github, GithubException
    except ImportError:
        print("PyGithub 미설치. pip install PyGithub")
        return

    g = Github(token)
    try:
        repo = g.get_repo(repo_name)
    except Exception as e:
        print(f"GitHub repo 접근 실패: {e}")
        return

    for project_name, state in run_states.items():
        branch = state.get("target_branch", "")
        commit = state.get("target_commit", "")
        content = render_report(project_name, model_name, branch, commit)

        file_path = f"reports/{project_name}.md"
        try:
            existing = repo.get_contents(file_path)
            repo.update_file(
                file_path,
                f"nightly: update {project_name} report",
                content,
                existing.sha,
            )
            print(f"  [{project_name}] GitHub 리포트 업데이트 완료")
        except Exception:
            try:
                repo.create_file(
                    file_path,
                    f"nightly: create {project_name} report",
                    content,
                )
                print(f"  [{project_name}] GitHub 리포트 생성 완료")
            except Exception as e:
                print(f"  [{project_name}] GitHub push 실패: {e}")

    # README 갱신 (전체 현황 요약)
    _update_readme(repo, run_states, model_name)


def _update_readme(repo, run_states, model_name):
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Nightly Agent Reports",
        f"**최종 실행**: {today} · **모델**: `{model_name}`",
        "",
        "| 프로젝트 | 활성 이슈 | 해결됨 | 리포트 |",
        "|---------|---------|--------|--------|",
    ]
    for project_name in sorted(run_states.keys()):
        issues = load_issues_db(project_name)
        active = sum(1 for i in issues if i.get("status") in ("open", "recurring", "derived"))
        resolved = sum(1 for i in issues if i.get("status") == "resolved")
        lines.append(f"| {project_name} | {active} | {resolved} | [리포트](reports/{project_name}.md) |")

    content = "\n".join(lines) + "\n"
    try:
        existing = repo.get_contents("README.md")
        repo.update_file("README.md", "nightly: update summary", content, existing.sha)
    except Exception:
        try:
            repo.create_file("README.md", "nightly: create summary", content)
        except Exception as e:
            print(f"README 업데이트 실패: {e}")
