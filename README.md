# Logix MCP

Offline-first MCP and extractor for Studio 5000 Logix Designer `.L5X` exports.

The first version is read-only. It parses a full-project `.L5X`, builds normalized
IR files, creates Markdown folders optimized for AI review, and exposes the same
data through MCP tools.

## Quick Start

```powershell
python -m pip install -e .
python -m logix_mcp ingest .\Arnold_0057_022_052226.L5X --out .\Arnold_0057_022_052226.logix
python -m logix_mcp inspect .\Arnold_0057_022_052226.logix
python -m logix_mcp serve .\Arnold_0057_022_052226.logix
```

On this Windows profile, Python entry-point scripts may be installed outside
`PATH`, so `python -m logix_mcp ...` is the most reliable command shape.

Generated workspace shape:

```text
<project>.logix/
├─ source/original/
├─ ir/
│  ├─ manifest.json
│  ├─ project.json
│  ├─ coverage.json
│  ├─ diagnostics.json
│  ├─ symbols.jsonl
│  ├─ tags.jsonl
│  ├─ data_values.jsonl
│  ├─ comments.jsonl
│  ├─ routines.jsonl
│  ├─ routine_units.jsonl
│  ├─ fbd_nodes.jsonl
│  ├─ fbd_wires.jsonl
│  ├─ sfc_nodes.jsonl
│  ├─ sfc_links.jsonl
│  ├─ modules.jsonl
│  ├─ module_io_tags.jsonl
│  ├─ module_io_points.jsonl
│  ├─ alarms.jsonl
│  ├─ xrefs.jsonl
│  └─ edges.jsonl
├─ ai/
│  ├─ overview.md
│  ├─ coverage.md
│  ├─ diagnostics.md
│  ├─ tags/
│  ├─ udts/
│  ├─ aois/<AOI>/routines/
│  ├─ programs/
│  └─ modules/
└─ index/logix.sqlite
```

`ir/` is the canonical machine-readable source for agents. `ai/` is a derived
reading layer with Markdown organized for quick review by humans and LLMs. The
Markdown intentionally preserves routine comments, rung comments, tag comments,
AOI routine bodies, FBD nodes/wires, SFC actions/transitions, module I/O
comments, alarms, and data/default data references.

`ir/coverage.json` records quality-gate counts taken directly from the XML and
compares them against extracted surfaces. For the Arnold fixture, P0 coverage is
expected to be complete for comments, data/default data blocks, FBD nodes, SFC
nodes, module I/O tag surfaces, routine Markdown comments, and AOI routine pages.

`ir/diagnostics.json` (and the matching `ai/diagnostics.md`) hold static-analysis
findings: multiple-output writers, dead/uninitialized tags, broken aliases,
unscheduled programs, inhibited/faulted modules, and unused AOIs/UDTs.

`index/logix.sqlite` is the query backend, not just a mirror: the MCP query and
graph tools read from it through indexes (with a JSONL fallback for older
workspaces), so reference, routine-context, and impact lookups stay fast on large
projects.

## Compact Analysis CLI

When the MCP server is not connected, use the CLI before reading large `ir/` or
`ai/` files:

```powershell
python -m logix_mcp search .\Arnold_0057_022_052226.logix "gland pump" --limit 10
python -m logix_mcp exists .\Arnold_0057_022_052226.logix "DP1.Blower[2]"
python -m logix_mcp operand .\Arnold_0057_022_052226.logix "UWP.Permit.COOLING"
python -m logix_mcp routine-slice .\Arnold_0057_022_052226.logix --program DP1 --routine R08_AUX_MOTOR --sheet 3
python -m logix_mcp fbd-sheet .\Arnold_0057_022_052226.logix --program DP1 --routine R08_AUX_MOTOR --sheet 3
python -m logix_mcp xref .\Arnold_0057_022_052226.logix "Motor_Run" --mode members --destructive
python -m logix_mcp trace .\Arnold_0057_022_052226.logix "Motor_Run"
python -m logix_mcp triage .\Arnold_0057_022_052226.logix "Blower 3 will not run in auto DP1"
python -m logix_mcp scope .\Arnold_0057_022_052226.logix "HMI shows red with breaker off"
python -m logix_mcp resolve-alarm .\Arnold_0057_022_052226.logix "DP2_GLAND_PUMP_SUMMARY_ALARM"
python -m logix_mcp decode-summary .\Arnold_0057_022_052226.logix "DP2_GLAND_PUMP_SUMMARY_ALARM"
python -m logix_mcp aoi-bindings .\Arnold_0057_022_052226.logix "MCC_Type1_Starter_03"
```

These commands return bounded JSON with snippets and evidence references. Use
`rg` for repository source code, extractor bugs, or proving missing evidence;
do not use it as the first pass over generated industrial artifacts.

## MCP Tools

- `project_summary()`
- `coverage_report()`
- `load_project(path, out=None)`
- `list_tags(scope=None, data_type=None, limit=200)`
- `get_tag(name, scope=None)`
- `get_tag_context(name, scope=None)`
- `list_udts(limit=200)`
- `get_udt(name)`
- `list_programs()`
- `get_program(name)`
- `list_routines(program=None, limit=200)`
- `get_routine(program, routine)`
- `get_routine_context(program=None, routine=None, routine_id=None)`
- `list_aois(limit=200)`
- `get_aoi(name)`
- `get_aoi_context(name)`
- `list_modules(limit=200)`
- `get_module_context(module)`
- `list_entities(kind=None, limit=200)`
- `get_entity(entity_id)`
- `search_entities(pattern, limit=50)`
- `search_logic(pattern, limit=50)`
- `search_project(query, kinds=None, scope=None, limit=20, offset=0)`
- `exists(query, kinds=None, scope=None)`
- `get_operand_context(operand, scope=None, detail="summary")`
- `get_routine_slice(program=None, routine=None, routine_id=None, sheet=None, unit_id=None, query=None, before=1, after=1)`
- `get_fbd_sheet(program=None, routine=None, routine_id=None, sheet=None, form="pseudo", limit=100)`
- `cross_reference(symbol, mode="exact", access=None, destructive=None, scope=None, limit=50, offset=0)`
- `find_references(symbol, limit=200)`
- `trace_signal(symbol, direction="upstream", max_depth=4, limit=100)`
- `triage_issue(issue_text, limit=5)`
- `scope_metadata(issue_text=None)`
- `resolve_alarm(name_or_class, limit=10)`
- `decode_summary(tag, limit=50)`
- `aoi_instance_bindings(instance, limit=10)`

### Analysis

- `tag_producers_consumers(name)` - routines that write a tag vs read it.
- `impact_of(name, max_depth=3, limit=300)` - transitive change propagation
  from a tag through the logic (affected routines, tags, and alarms).
- `io_trace(name)` - resolve a tag's alias chain to physical I/O, logic, alarms.
- `call_graph(routine=None, program=None)` - callers/callees of a routine, or
  the task/program scheduling tree (including unscheduled programs).
- `run_diagnostics()` - prioritized static-analysis findings.

## Notes

- `.L5X` is the only supported source format in v1.
- `.ACD`, `.L5K`, `.AML`, and `.RDF` are intentionally not parsed yet.
- The extractor never modifies the input `.L5X`.
- Cross-references are classified from a per-instruction signature table and from
  AOI parameter usage, and each row carries a `confidence` (`typed` vs
  `heuristic`). Operands of instructions outside the table remain `heuristic`
  with access `unknown`; ST/SFC AOI-call argument directions are not yet typed.
  The raw routine units and source comments/data are preserved in IR for
  follow-up analysis.
