import logging
import re
from pathlib import Path
from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _get_service_root(repo_path: Path) -> Path:
    settings = get_settings()
    sub = repo_path / settings.TARGET_REPO_NAME
    if (sub / "src").exists():
        return sub
    if (repo_path / "exceptionServer" / "src").exists():
        return repo_path / "exceptionServer"
    return repo_path


def analyze_test_impact(repo_path: Path, patch_diff: str) -> tuple[list[str], int]:
    """
    변경된 Java 클래스를 역추적하여 연관된 JUnit 테스트 FQCN 목록과 전체 테스트 수를 반환합니다.
    """
    service_root = _get_service_root(repo_path)
    main_dir = service_root / "src" / "main" / "java"
    test_dir = service_root / "src" / "test" / "java"

    if not test_dir.exists():
        logger.warning("Test directory not found: %s", test_dir)
        return [], 0

    # 1. 전체 테스트 클래스 스캔 (FQCN 매핑)
    all_tests: dict[str, str] = {}
    test_contents: dict[str, str] = {}
    for tf in test_dir.rglob("*.java"):
        if not (tf.name.endswith("Test.java") or tf.name.endswith("Tests.java")):
            continue
        rel = tf.relative_to(test_dir)
        fqcn = ".".join(rel.with_suffix("").parts)
        all_tests[tf.stem] = fqcn
        test_contents[fqcn] = tf.read_text(encoding="utf-8", errors="ignore")

    total_tests_count = len(all_tests)
    if total_tests_count == 0:
        return [], 0

    # 2. patch_diff에서 수정된 Java 클래스명 추출
    changed_classes: set[str] = set()
    for line in patch_diff.splitlines():
        if line.startswith("+++ b/") and line.endswith(".java"):
            changed_classes.add(Path(line[6:].strip()).stem)

    # 3. Caller 역방향 추적 (수정된 클래스를 참조하는 main 클래스 탐색)
    impacted_classes = set(changed_classes)
    if main_dir.exists() and changed_classes:
        added = True
        while added:
            added = False
            for mf in main_dir.rglob("*.java"):
                cls_name = mf.stem
                if cls_name in impacted_classes:
                    continue
                code = mf.read_text(encoding="utf-8", errors="ignore")
                if any(re.search(rf"\b{re.escape(target)}\b", code) for target in impacted_classes):
                    impacted_classes.add(cls_name)
                    added = True

    # 4. 영향받는 클래스를 참조하거나 이름이 대응되는 테스트 선별
    selected_tests: set[str] = set()
    for simple_name, fqcn in all_tests.items():
        content = test_contents[fqcn]
        if any(
            simple_name in (f"{cls}Test", f"{cls}Tests")
            or re.search(rf"\b{re.escape(cls)}\b", content)
            for cls in impacted_classes
        ):
            selected_tests.add(fqcn)

    sorted_tests = sorted(selected_tests)
    logger.info(
        "Impact analysis complete: impacted_classes=%s, selected_tests=%d/%d",
        impacted_classes,
        len(sorted_tests),
        total_tests_count,
    )
    return sorted_tests, total_tests_count


analyze_impact = analyze_test_impact
