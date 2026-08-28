"""The README must not advertise numbers the code contradicts.

Every figure in it was hand-maintained, and the test count alone was wrong four
times in one afternoon. A submission document that misstates its own project is
worse than one that omits the detail.
"""

from __future__ import annotations

import pathlib
import re

import pytest

import conftest

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()


def test_the_advertised_test_count_is_the_real_one():
    if not conftest.collection_was_complete():
        pytest.skip("partial run — a subset's count says nothing about the suite")
    claimed = {int(n) for n in re.findall(r"(\d{3,4}) tests", README)}
    claimed |= {int(n) for n in re.findall(r"\|\s*(\d{3,4}), all offline\s*\|", README)}
    assert claimed, "the README no longer states a test count"
    actual = conftest.COLLECTED["count"]
    assert actual > 0, "collection count was not recorded"
    assert claimed == {actual}, (
        f"README claims {sorted(claimed)} tests, pytest collected {actual}"
    )


def test_the_advertised_brand_count_is_the_real_one():
    from parchi.drugs import BRAND_TABLE

    claimed = {int(n) for n in re.findall(r"(\d{2,3}) brands", README)}
    assert claimed == {len(BRAND_TABLE)}, (
        f"README claims {sorted(claimed)} brands, table has {len(BRAND_TABLE)}"
    )


def test_the_architecture_diagram_exists_and_is_referenced():
    assert "docs/architecture.svg" in README
    svg = ROOT / "docs" / "architecture.svg"
    assert svg.exists()
    # Served by the thing it describes, so it must be bundled into the image.
    assert (ROOT / "parchi" / "static" / "architecture.svg").read_text() == svg.read_text()


def test_the_diagram_carries_no_number_that_goes_stale():
    """It said "345 tests" for three commits after the suite moved on."""
    svg = (ROOT / "docs" / "architecture.svg").read_text()
    assert not re.search(r"\d{3,4} tests", svg)


def test_every_module_in_the_repository_map_exists():
    for match in re.finditer(r"`(parchi/[a-z_]+\.py)`", README):
        assert (ROOT / match.group(1)).exists(), match.group(1)


def test_the_declared_technologies_are_actually_imported():
    """The required-technology table is the first thing checked, so it must not
    claim something the code does not use."""
    source = "\n".join(
        p.read_text() for p in (ROOT / "parchi").rglob("*.py"))
    assert "google.adk" in source, "ADK is claimed but not imported"
    assert "google.genai" in source or "from google import genai" in source
    assert "firestore" in source
    assert "storage" in source
