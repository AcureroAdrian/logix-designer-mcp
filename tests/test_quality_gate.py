import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest

from logix_mcp.parser import parse_l5x
from logix_mcp.workspace import ingest_l5x, read_jsonl


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_L5X = PROJECT_ROOT / "Arnold_0057_022_052226.L5X"
GENERATED_WORKSPACE = PROJECT_ROOT / "Arnold_0057_022_052226.logix"

EXPECTED_XML_COUNTS = {
    "comments": 9590,
    "data_blocks": 13966,
    "default_data_blocks": 6484,
    "fbd_routines": 37,
    "fbd_sheets": 142,
    "fbd_blocks": 898,
    "fbd_wires": 3770,
    "sfc_routines": 4,
    "sfc_steps": 89,
    "sfc_transitions": 138,
    "sfc_actions": 71,
    "module_input_tags": 231,
    "module_output_tags": 149,
    "module_config_tags": 208,
    "module_io_points": 588,
}

P0_SURFACES = {
    "comments",
    "data_blocks",
    "fbd_nodes",
    "sfc_nodes",
    "module_io_points",
    "routine_markdown_comments",
    "aoi_routine_pages",
}


@pytest.fixture(scope="module")
def arnold_l5x() -> Path:
    if not REAL_L5X.exists():
        pytest.skip("real Arnold L5X fixture is not present")
    return REAL_L5X


@pytest.fixture(scope="module")
def arnold_workspace(tmp_path_factory: pytest.TempPathFactory, arnold_l5x: Path) -> Path:
    if (GENERATED_WORKSPACE / "ir" / "project.json").exists():
        return GENERATED_WORKSPACE

    workspace = tmp_path_factory.mktemp("arnold_quality_gate") / "Arnold_0057_022_052226.logix"
    ingest_l5x(arnold_l5x, workspace)
    return workspace


def test_real_arnold_l5x_contains_quality_gate_source_surfaces(arnold_l5x: Path):
    counts = _source_quality_counts(arnold_l5x)

    assert counts == EXPECTED_XML_COUNTS


def test_parse_l5x_exposes_quality_gate_coverage_counts(arnold_l5x: Path):
    project = parse_l5x(arnold_l5x)
    coverage_counts = _coverage_counts(project.get("coverage") or project.get("quality_gate"))

    assert coverage_counts is not None, "parse_l5x should expose coverage counts under project['coverage']['counts']"
    for key, expected in EXPECTED_XML_COUNTS.items():
        assert coverage_counts.get(key) == expected


@pytest.mark.parametrize("program", ["DP1", "DP2"])
def test_r10_vacon_comm_markdown_preserves_end_mapping(arnold_workspace: Path, program: str):
    routine_md = arnold_workspace / "ai" / "programs" / program / "routines" / "R10_VACON_COMM.md"

    assert routine_md.exists()
    assert "END MAPPING" in routine_md.read_text(encoding="utf-8")


def test_aoi_routine_markdown_pages_exist_for_every_aoi_routine(arnold_workspace: Path):
    aoi_routines = [
        row
        for row in read_jsonl(arnold_workspace, "routines.jsonl")
        if str(row.get("owner") or "").startswith("AOI:")
    ]

    assert aoi_routines, "Arnold fixture should include AOI routines"
    missing = []
    for routine in aoi_routines:
        aoi_name = str(routine["owner"]).split("AOI:", 1)[1]
        expected_path = (
            arnold_workspace
            / "ai"
            / "aois"
            / _safe_name(aoi_name)
            / "routines"
            / f"{_safe_name(routine.get('name'))}.md"
        )
        if not expected_path.exists():
            missing.append(str(expected_path.relative_to(arnold_workspace)))

    assert not missing, "Missing AOI routine Markdown pages: " + ", ".join(missing[:10])


def test_workspace_coverage_json_reports_p0_surfaces_as_complete(arnold_workspace: Path):
    coverage_path = arnold_workspace / "ir" / "coverage.json"

    assert coverage_path.exists(), "expected generated workspace to include ir/coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage_counts = _coverage_counts(coverage)
    surfaces = coverage.get("surfaces")

    assert coverage_counts is not None, "coverage.json should expose top-level coverage counts"
    for key, expected in EXPECTED_XML_COUNTS.items():
        assert coverage_counts.get(key) == expected

    assert isinstance(surfaces, dict), "coverage.json should expose a surfaces object"
    for surface_name in P0_SURFACES:
        assert surface_name in surfaces
        surface = surfaces[surface_name]
        assert surface.get("priority") == "P0"
        assert surface.get("missing_count", 0) == 0
        assert surface.get("missing") in (None, [], {})
        if "source_count" in surface and "covered_count" in surface:
            assert surface["covered_count"] == surface["source_count"]

    missing_by_priority = coverage.get("missing", {})
    if isinstance(missing_by_priority, dict):
        assert missing_by_priority.get("P0", []) == []
        assert missing_by_priority.get("p0", []) == []


def _source_quality_counts(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    element_counts = Counter(_local_name(elem.tag) for elem in root.iter())
    routine_types = Counter(
        elem.attrib.get("Type", "")
        for elem in root.iter()
        if _local_name(elem.tag) == "Routine"
    )
    fbd_counts = _descendant_counts(root, "FBDContent")
    sfc_counts = _descendant_counts(root, "SFCContent")
    module_counts = _module_descendant_counts(root)

    return {
        "comments": element_counts["Comment"],
        "data_blocks": element_counts["Data"],
        "default_data_blocks": element_counts["DefaultData"],
        "fbd_routines": routine_types["FBD"],
        "fbd_sheets": fbd_counts["Sheet"],
        "fbd_blocks": fbd_counts["Block"],
        "fbd_wires": fbd_counts["Wire"],
        "sfc_routines": routine_types["SFC"],
        "sfc_steps": sfc_counts["Step"],
        "sfc_transitions": sfc_counts["Transition"],
        "sfc_actions": sfc_counts["Action"],
        "module_input_tags": module_counts["InputTag"],
        "module_output_tags": module_counts["OutputTag"],
        "module_config_tags": module_counts["ConfigTag"],
        "module_io_points": module_counts["InputTag"] + module_counts["OutputTag"] + module_counts["ConfigTag"],
    }


def _descendant_counts(root: ET.Element, container_name: str) -> Counter:
    counts = Counter()
    for container in root.iter():
        if _local_name(container.tag) != container_name:
            continue
        for descendant in container.iter():
            if descendant is not container:
                counts[_local_name(descendant.tag)] += 1
    return counts


def _module_descendant_counts(root: ET.Element) -> Counter:
    counts = Counter()
    for module in root.iter():
        if _local_name(module.tag) != "Module":
            continue
        for descendant in module.iter():
            if descendant is not module:
                counts[_local_name(descendant.tag)] += 1
    return counts


def _coverage_counts(coverage: object) -> dict | None:
    if not isinstance(coverage, dict):
        return None
    counts = coverage.get("counts")
    return counts if isinstance(counts, dict) else None


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _safe_name(name: object) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "unnamed"))
    safe = safe.strip(" .")
    return safe[:120] or "unnamed"
