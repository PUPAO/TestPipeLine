import logging
import re
from pathlib import Path
import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

logger = logging.getLogger(__name__)

JAVA_LANGUAGE = Language(tsjava.language())


def extract_modified_files_from_diff(patch_diff: str) -> list[str]:
    """Unified Diff 헤더(+++ b/...)에서 변경된 상대 파일 경로 목록을 추출합니다."""
    files: list[str] = []
    for line in patch_diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null" and path not in files:
                files.append(path)
    return files


def _extract_referenced_types(source_bytes: bytes) -> set[str]:
    """tree-sitter-java AST를 순회해 파일 내부에서 참조하는 모든 타입/클래스 식별자를 추출합니다."""
    parser = Parser(JAVA_LANGUAGE)
    tree = parser.parse(source_bytes)
    types: set[str] = set()

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "type_identifier":
            types.add(source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore"))
        elif node.type == "import_declaration":
            imp_text = source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")
            match = re.search(r"\.([A-Z][A-Za-z0-9_]*)\s*;", imp_text)
            if match:
                types.add(match.group(1))
        stack.extend(node.children)

    return types


def _path_to_fqcn(rel_path: str) -> str:
    """src/test/java/com/example/FooTest.java -> com.example.FooTest"""
    normalized = rel_path.replace("\\", "/")
    for prefix in ("src/test/java/", "src/main/java/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    if normalized.endswith(".java"):
        normalized = normalized[:-5]
    return normalized.replace("/", ".")


def analyze_test_impact(repo_path: Path, patch_diff: str) -> tuple[list[str], int]:
    """
    변경된 클래스를 참조하는 상위 계층(Caller)까지 AST로 역추적해
    실행해야 할 JUnit 테스트 FQCN 목록과 전체 테스트 클래스 수를 반환합니다.
    """
    test_root = repo_path / "src" / "test" / "java"
    main_root = repo_path / "src" / "main" / "java"

    if not test_root.exists():
        return [], 0

    all_test_files = list(test_root.rglob("*Test.java")) + list(test_root.rglob("*Tests.java"))
    # 중복 제거
    unique_test_files = {f.resolve(): f for f in all_test_files}
    total_tests_count = len(unique_test_files)

    modified_paths = extract_modified_files_from_diff(patch_diff)
    impacted_classes: set[str] = set()
    direct_test_fqcn: set[str] = set()

    for m_path in modified_paths:
        stem = Path(m_path).stem
        if m_path.startswith("src/test/java/"):
            direct_test_fqcn.add(_path_to_fqcn(m_path))
        elif stem:
            impacted_classes.add(stem)

    # 1단계: main 코드 내에서 변경된 클래스를 참조하는 상위 호출자(Service -> Controller/Facade) 탐색 (2-hop)
    if main_root.exists() and impacted_classes:
        main_refs: dict[str, set[str]] = {}
        for java_file in main_root.rglob("*.java"):
            refs = _extract_referenced_types(java_file.read_bytes())
            main_refs[java_file.stem] = refs

        for _ in range(2):
            newly_impacted = set()
            for cls_name, refs in main_refs.items():
                if cls_name not in impacted_classes and (refs & impacted_classes):
                    newly_impacted.add(cls_name)
            impacted_classes.update(newly_impacted)

    # 2단계: 영향받는 클래스군을 직접 테스트하거나 참조하는 JUnit 테스트 클래스 선별
    selected_tests: set[str] = set(direct_test_fqcn)
    for test_file in unique_test_files.values():
        rel_test_path = test_file.relative_to(repo_path).as_posix()
        fqcn = _path_to_fqcn(rel_test_path)
        test_stem = test_file.stem

        # 이름 기반 직접 매핑 (예: ReviewService -> ReviewServiceTest)
        if any(test_stem.startswith(cls) for cls in impacted_classes):
            selected_tests.add(fqcn)
            continue

        # AST 타입 참조 기반 매핑
        test_refs = _extract_referenced_types(test_file.read_bytes())
        if test_refs & impacted_classes:
            selected_tests.add(fqcn)

    sorted_tests = sorted(selected_tests)
    logger.info(
        "Impact analysis complete: impacted_classes=%s, selected_tests=%d/%d",
        impacted_classes,
        len(sorted_tests),
        total_tests_count,
    )
    return sorted_tests, total_tests_count
