from pathlib import Path

import pytest

from logix_mcp.parser import parse_l5x


REAL_L5X = Path(__file__).resolve().parents[1] / "Arnold_0058_020_060926.L5X"


@pytest.mark.skipif(not REAL_L5X.exists(), reason="real L5X fixture is not present")
def test_real_arnold_l5x_smoke_counts():
    project = parse_l5x(REAL_L5X)

    assert project["controller"]["name"] == "Arnold"
    assert project["controller"]["processor_type"] == "1756-L85E"
    assert project["counts"]["data_types"] == 324
    assert project["counts"]["aois"] == 109
    assert project["counts"]["controller_tags"] == 4456
    assert project["counts"]["programs"] == 33
    assert project["counts"]["program_tags"] == 2752
    assert project["counts"]["routines"] == 391
    assert project["counts"]["modules"] == 355
    assert project["counts"]["tasks"] == 9
    assert project["counts"]["xrefs"] > 20000
