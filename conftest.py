import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


#: How many tests pytest actually collected this run. tests/test_readme.py
#: compares this against the number the README advertises, because that number
#: was wrong four times in one afternoon when a human kept it by hand.
COLLECTED = {"count": 0, "modules": set()}


def pytest_collection_modifyitems(session, config, items):
    COLLECTED["count"] = len(items)
    COLLECTED["modules"] = {
        Path(str(item.fspath)).name for item in items
    }


def collection_was_complete() -> bool:
    """True only when this run collected from every test module.

    A count taken from `pytest tests/test_readme.py` says nothing about the
    suite, so the README check has to know the difference between a partial run
    and the real thing.
    """
    on_disk = {p.name for p in (Path(__file__).parent / "tests").glob("test_*.py")}
    return bool(on_disk) and on_disk <= COLLECTED["modules"]
