from pathlib import Path


DOCKERIGNORE = Path(__file__).parents[2] / ".dockerignore"


def _patterns() -> set[str]:
    return {
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_generated_repository_bulk_is_outside_docker_context():
    patterns = _patterns()
    assert ".cache/" in patterns
    assert ".zvec-grep/" in patterns


def test_runtime_source_boundaries_remain_in_docker_context():
    patterns = _patterns()
    assert "src/" not in patterns
    assert ".github/" not in patterns
    assert ".gitignore" not in patterns
