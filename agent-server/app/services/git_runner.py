import asyncio
import logging
import time
from pathlib import Path
import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def _run_cmd(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="ignore"),
        stderr.decode("utf-8", errors="ignore"),
    )


async def ensure_repo_cloned() -> Path:
    """타겟 저장소가 없으면 clone하고, 있으면 최신 base 브랜치로 동기화합니다."""
    settings = get_settings()
    repo_path = settings.target_repo_path
    repo_path.parent.mkdir(parents=True, exist_ok=True)

    if not (repo_path / ".git").exists():
        logger.info("Cloning target repository into %s", repo_path)
        code, _, err = await _run_cmd(
            ["git", "clone", settings.target_repo_clone_url, str(repo_path)],
            cwd=repo_path.parent,
        )
        if code != 0:
            raise RuntimeError(f"Git clone failed: {err}")
        await _run_cmd(["git", "config", "user.name", "AI Incident Bot"], cwd=repo_path)
        await _run_cmd(["git", "config", "user.email", "ai-incident-bot@users.noreply.github.com"], cwd=repo_path)

    await _run_cmd(["git", "fetch", "origin", settings.TARGET_BASE_BRANCH], cwd=repo_path)
    return repo_path


async def apply_patch_and_run_tests(
    incident_id: str,
    patch_diff: str,
    affected_tests: list[str],
) -> tuple[bool, str, str, int]:
    """
    임시 브랜치에 패치를 적용하고 선별된 JUnit 테스트만 실행합니다.
    Returns: (passed, branch_name, error_log, duration_ms)
    """
    settings = get_settings()
    repo_path = await ensure_repo_cloned()
    branch_name = f"fix/incident-{incident_id[:8]}"

    # 1. 깨끗한 base 브랜치 상태에서 임시 패치 브랜치 생성
    await _run_cmd(["git", "reset", "--hard"], cwd=repo_path)
    await _run_cmd(["git", "clean", "-fd"], cwd=repo_path)
    await _run_cmd(["git", "checkout", "-B", branch_name, f"origin/{settings.TARGET_BASE_BRANCH}"], cwd=repo_path)

    if not patch_diff.strip():
        return False, branch_name, "Generated unified diff is empty.", 0

    patch_file = repo_path / ".ai_incident.patch"
    patch_file.write_text(patch_diff if patch_diff.endswith("\n") else patch_diff + "\n", encoding="utf-8")

    # 2. git apply 실행 (--recount로 LLM이 계산한 줄 번호 오차 보정)
    code, _, err = await _run_cmd(
        ["git", "apply", "--recount", "--ignore-whitespace", str(patch_file)],
        cwd=repo_path,
    )
    patch_file.unlink(missing_ok=True)

    if code != 0:
        return False, branch_name, f"git apply failed:\n{err}", 0

    # 3. Gradle 실행 권한 보장 및 선별 회귀 테스트 실행
    gradlew = repo_path / "gradlew"
    if gradlew.exists():
        gradlew.chmod(0o755)
        gradle_bin = "./gradlew"
    else:
        gradle_bin = "gradle"

    if affected_tests:
        cmd = [gradle_bin, "test"]
        for test_fqcn in affected_tests:
            cmd.extend(["--tests", test_fqcn])
        cmd.append("--no-daemon")
    else:
        # 연관 테스트가 매핑되지 않은 경우 최소한 전체 컴파일 및 테스트 클래스 빌드 검증
        cmd = [gradle_bin, "testClasses", "--no-daemon"]

    logger.info("Running selective regression command: %s", " ".join(cmd))
    start_ts = time.perf_counter()
    test_code, out, test_err = await _run_cmd(cmd, cwd=repo_path)
    duration_ms = int((time.perf_counter() - start_ts) * 1000)

    if test_code == 0:
        return True, branch_name, "", duration_ms

    combined_log = (out[-2500:] + "\n" + test_err[-2500:]).strip()
    return False, branch_name, combined_log, duration_ms


async def create_github_pr(
    incident_id: str,
    branch_name: str,
    error_category: str,
    patch_explanation: str,
    affected_tests: list[str],
    executed_tests_count: int,
    total_tests_count: int,
) -> str:
    """Discord 승인 버튼 클릭 시 패치 브랜치를 푸시하고 GitHub PR을 생성합니다."""
    settings = get_settings()
    repo_path = await ensure_repo_cloned()

    await _run_cmd(["git", "checkout", branch_name], cwd=repo_path)
    await _run_cmd(["git", "add", "-A"], cwd=repo_path)
    commit_msg = f"fix({error_category.lower()}): auto-resolve incident #{incident_id[:8]}"
    code, _, err = await _run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_path)
    if code != 0 and "nothing to commit" not in err:
        raise RuntimeError(f"Git commit failed: {err}")

    push_code, _, push_err = await _run_cmd(
        ["git", "push", "-u", "origin", branch_name, "--force"],
        cwd=repo_path,
    )
    if push_code != 0:
        raise RuntimeError(f"Git push failed: {push_err}")

    tests_md = "\n".join(f"- `{t}`" for t in affected_tests) or "- `testClasses` 컴파일 검증 완료"
    pr_body = (
        f"## 🤖 AI 장애 자동 분석 및 회귀 검증 리포트 (`#{incident_id[:8]}`)\n\n"
        f"### 1. 원인 분석 및 수정 요약\n{patch_explanation}\n\n"
        f"### 2. 영향도 분석 기반 선별 회귀 테스트 ({executed_tests_count} / {total_tests_count}개 실행)\n"
        f"{tests_md}\n\n"
        "---\n*Approved via Discord Incident Responder*"
    )

    api_url = f"https://api.github.com/repos/{settings.TARGET_REPO_OWNER}/{settings.TARGET_REPO_NAME}/pulls"
    headers = {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": f"[AI Auto-Fix] {error_category} 장애 복구 (#{incident_id[:8]})",
        "head": branch_name,
        "base": settings.TARGET_BASE_BRANCH,
        "body": pr_body,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(api_url, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"GitHub PR API error ({resp.status_code}): {resp.text}")
        pr_url = str(resp.json()["html_url"])

    # 작업 완료 후 base 브랜치로 복귀
    await _run_cmd(["git", "checkout", settings.TARGET_BASE_BRANCH], cwd=repo_path)
    return pr_url


async def cleanup_branch(branch_name: str) -> None:
    """수정 반려 시 로컬 임시 브랜치를 정리합니다."""
    settings = get_settings()
    repo_path = await ensure_repo_cloned()
    await _run_cmd(["git", "reset", "--hard"], cwd=repo_path)
    await _run_cmd(["git", "checkout", settings.TARGET_BASE_BRANCH], cwd=repo_path)
    await _run_cmd(["git", "branch", "-D", branch_name], cwd=repo_path)
