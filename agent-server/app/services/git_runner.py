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


def _resolve_service_dir(repo_path: Path, service_subdir: str | None = None) -> tuple[Path, str]:
    """모노레포 내 Spring Boot 서비스 하위 디렉토리와 상대 경로 접두사를 반환합니다."""
    settings = get_settings()
    subdir = (
        service_subdir
        or getattr(settings, "TARGET_SERVICE_SUBDIR", None)
        or "exceptionServer"
    ).strip("/")

    candidate = repo_path / subdir
    if subdir and candidate.is_dir():
        return candidate, subdir
    return repo_path, ""


def _apply_hunk_lines_fallback(repo_path: Path, service_dir: Path, diff_text: str) -> bool:
    """git apply 실패 시 hunk(- 제거 라인 -> + 추가 라인)를 대상 파일에서 찾아 직접 치환합니다."""
    current_file: str | None = None
    removed_lines: list[str] = []
    added_lines: list[str] = []
    hunks: list[tuple[str, list[str], list[str]]] = []

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            if current_file and (removed_lines or added_lines):
                hunks.append((current_file, removed_lines[:], added_lines[:]))
                removed_lines.clear()
                added_lines.clear()
            current_file = line[6:].strip()
        elif line.startswith("@@ ") and current_file:
            if removed_lines or added_lines:
                hunks.append((current_file, removed_lines[:], added_lines[:]))
                removed_lines.clear()
                added_lines.clear()
        elif current_file and line.startswith("-") and not line.startswith("---"):
            removed_lines.append(line[1:])
        elif current_file and line.startswith("+") and not line.startswith("+++"):
            added_lines.append(line[1:])

    if current_file and (removed_lines or added_lines):
        hunks.append((current_file, removed_lines[:], added_lines[:]))

    changed_files = 0
    for rel_path, rem, add in hunks:
        if not rem:
            continue

        target = repo_path / rel_path
        if not target.exists():
            target = service_dir / rel_path
        if not target.exists():
            continue

        content = target.read_text(encoding="utf-8")
        rem_block = "\n".join(rem)
        add_block = "\n".join(add)

        if rem_block in content:
            target.write_text(content.replace(rem_block, add_block, 1), encoding="utf-8")
            changed_files += 1
            continue

        lines = content.splitlines()
        n = len(rem)
        stripped_rem = [r.strip() for r in rem]
        for idx in range(len(lines) - n + 1):
            if [lines[idx + j].strip() for j in range(n)] == stripped_rem:
                lines[idx : idx + n] = add
                suffix = "\n" if content.endswith("\n") else ""
                target.write_text("\n".join(lines) + suffix, encoding="utf-8")
                changed_files += 1
                break

    return changed_files > 0


async def ensure_repo_cloned() -> Path:
    """로컬/볼륨에 모노레포 저장소가 이미 있으면 재사용하고, 없으면 GitHub에서 clone합니다."""
    settings = get_settings()
    repo_path = settings.target_repo_path
    repo_path.parent.mkdir(parents=True, exist_ok=True)

    await _run_cmd(["git", "config", "--global", "--add", "safe.directory", "*"], cwd=repo_path.parent)

    if repo_path.exists() and any(repo_path.iterdir()):
        if not (repo_path / ".git").exists():
            await _run_cmd(["git", "init", "-b", settings.TARGET_BASE_BRANCH], cwd=repo_path)
            await _run_cmd(["git", "add", "-A"], cwd=repo_path)
            await _run_cmd(["git", "commit", "-m", "chore: initialize local workspace repo"], cwd=repo_path)
    elif not (repo_path / ".git").exists():
        logger.info("Cloning target repository into %s", repo_path)
        code, _, err = await _run_cmd(
            ["git", "clone", settings.target_repo_clone_url, str(repo_path)],
            cwd=repo_path.parent,
        )
        if code != 0:
            raise RuntimeError(f"Git clone failed: {err}")

    name_code, name_out, _ = await _run_cmd(["git", "config", "user.name"], cwd=repo_path)
    if name_code != 0 or not name_out.strip():
        await _run_cmd(["git", "config", "user.name", "AI Incident Bot"], cwd=repo_path)
        await _run_cmd(["git", "config", "user.email", "ai-incident-bot@users.noreply.github.com"], cwd=repo_path)

    code, _, _ = await _run_cmd(["git", "remote", "get-url", "origin"], cwd=repo_path)
    if code == 0:
        await _run_cmd(["git", "fetch", "origin", settings.TARGET_BASE_BRANCH], cwd=repo_path)
    return repo_path


async def apply_patch_and_run_tests(
    incident_id: str,
    patch_diff: str,
    affected_tests: list[str],
    service_subdir: str = "exceptionServer",
) -> tuple[bool, str, str, int]:
    """
    모노레포 루트에서 임시 브랜치를 생성해 하위 서비스 디렉토리에 패치를 적용하고 선별 테스트를 실행합니다.
    Returns: (passed, branch_name, error_log, duration_ms)
    """
    settings = get_settings()
    repo_path = await ensure_repo_cloned()
    service_dir, subdir = _resolve_service_dir(repo_path, service_subdir)
    branch_name = f"fix/incident-{incident_id[:8]}"

    has_origin, _, _ = await _run_cmd(
        ["git", "rev-parse", "--verify", f"origin/{settings.TARGET_BASE_BRANCH}"],
        cwd=repo_path,
    )
    base_ref = f"origin/{settings.TARGET_BASE_BRANCH}" if has_origin == 0 else settings.TARGET_BASE_BRANCH

    await _run_cmd(["git", "reset", "--hard"], cwd=repo_path)
    await _run_cmd(["git", "clean", "-fd"], cwd=repo_path)
    await _run_cmd(["git", "checkout", "-B", branch_name, base_ref], cwd=repo_path)

    if not patch_diff.strip():
        return False, branch_name, "Generated unified diff is empty.", 0

    normalized_diff = patch_diff if patch_diff.endswith("\n") else patch_diff + "\n"
    patch_file = repo_path / ".ai_incident.patch"
    patch_file.write_text(normalized_diff, encoding="utf-8")

    has_subdir_prefix = bool(subdir) and (
        f"--- a/{subdir}/" in normalized_diff or f"+++ b/{subdir}/" in normalized_diff
    )
    dir_args = [f"--directory={subdir}"] if (subdir and not has_subdir_prefix) else []

    code, _, err = await _run_cmd(
        ["git", "apply", *dir_args, "--recount", "--ignore-whitespace", str(patch_file)],
        cwd=repo_path,
    )
    if code != 0:
        code, _, err2 = await _run_cmd(
            ["git", "apply", *dir_args, "--3way", "--recount", str(patch_file)],
            cwd=repo_path,
        )
        if code != 0:
            if _apply_hunk_lines_fallback(repo_path, service_dir, normalized_diff):
                logger.info("Applied patch via line-matching fallback for %s", incident_id[:8])
                code = 0
            else:
                err = f"{err}\n{err2}".strip()

    patch_file.unlink(missing_ok=True)

    if code != 0:
        return False, branch_name, f"git apply failed:\n{err}", 0

    gradlew = service_dir / "gradlew"
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
        cmd = [gradle_bin, "testClasses", "--no-daemon"]

    logger.info("Running selective regression command in %s: %s", service_dir, " ".join(cmd))
    start_ts = time.perf_counter()
    test_code, out, test_err = await _run_cmd(cmd, cwd=service_dir)
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
    """Discord 승인 버튼 클릭 시 모노레포 패치 브랜치를 푸시하고 GitHub PR을 생성합니다."""
    settings = get_settings()
    repo_path = await ensure_repo_cloned()

    await _run_cmd(["git", "checkout", branch_name], cwd=repo_path)
    await _run_cmd(["git", "add", "-A"], cwd=repo_path)
    commit_msg = f"fix({error_category.lower()}): auto-resolve incident #{incident_id[:8]}"
    code, _, err = await _run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_path)
    if code != 0 and "nothing to commit" not in err:
        raise RuntimeError(f"Git commit failed: {err}")

    push_target = (
        f"https://x-access-token:{settings.GITHUB_TOKEN}@github.com/"
        f"{settings.TARGET_REPO_OWNER}/{settings.TARGET_REPO_NAME}.git"
        if settings.GITHUB_TOKEN
        else "origin"
    )
    push_code, _, push_err = await _run_cmd(
        ["git", "push", "-u", push_target, f"{branch_name}:{branch_name}", "--force"],
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

    await _run_cmd(["git", "checkout", settings.TARGET_BASE_BRANCH], cwd=repo_path)
    return pr_url


async def cleanup_branch(branch_name: str) -> None:
    """수정 반려 시 로컬 임시 브랜치를 정리합니다."""
    settings = get_settings()
    repo_path = await ensure_repo_cloned()
    await _run_cmd(["git", "reset", "--hard"], cwd=repo_path)
    await _run_cmd(["git", "checkout", settings.TARGET_BASE_BRANCH], cwd=repo_path)
    await _run_cmd(["git", "branch", "-D", branch_name], cwd=repo_path)
