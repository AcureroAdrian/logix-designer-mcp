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
│  ├─ tag_data.jsonl
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
Two honesty surfaces guard the gate: `unknown_elements` (P0: XML elements the
pipeline does not recognize at all) and `unextracted_elements` (P1: known
elements not modeled yet, each with a documented reason).

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

All tools are read-only (`readOnlyHint`); ingestion is CLI-only
(`python -m logix_mcp ingest`). `list_*` tools return a uniform envelope
`{items, total, offset, limit, has_more, truncated}` with summary rows (never
raw bodies/members/nodes); failed lookups return `{found: false, did_you_mean}`.
Deep context tools default to `detail="summary"` and support
`detail="detail"` for bounded row samples or `detail="full"` for the raw legacy
bundle. Bounded deep responses include `result_size` and `truncated`; spill files
are never created unless `spill=true` is passed explicitly.

- `project_summary()`
- `coverage_report()`
- `list_tags(scope=None, data_type=None, limit=100, offset=0)`
- `get_tag(name, scope=None)`
- `get_tag_context(name, scope=None, detail="summary", spill=false)`
- `list_udts(limit=200, offset=0)`
- `get_udt(name, detail="summary")`
- `list_programs(limit=200, offset=0)`
- `get_program(name)`
- `list_routines(program=None, limit=100, offset=0)`
- `get_routine(program, routine)`
- `get_routine_context(program=None, routine=None, routine_id=None, detail="summary", unit_limit=100)`
- `list_aois(limit=200, offset=0)`
- `get_aoi(name, detail="summary")`
- `get_aoi_context(name, detail="summary", spill=false)`
- `list_modules(limit=100, offset=0)`
- `get_module_context(module=None, name=None, detail="summary", spill=false)`
- `list_entities(kind=None, limit=100, offset=0)`
- `get_entity(entity_id)`
- `search_entities(pattern=None, query=None, limit=50, offset=0)`
- `search_logic(pattern=None, query=None, limit=50, offset=0)`
- `search_project(query, kinds=None, scope=None, limit=20, offset=0)`
- `exists(query, kinds=None, scope=None)`
- `get_operand_context(operand, scope=None, detail="summary")`
- `get_routine_slice(program=None, routine=None, routine_id=None, sheet=None, unit_id=None, query=None, before=1, after=1)`
- `get_fbd_sheet(program=None, routine=None, routine_id=None, sheet=None, form="pseudo", limit=100)`
- `cross_reference(symbol=None, name=None, operand=None, mode="exact", access=None, destructive=None, scope=None, limit=50, offset=0)`
- `find_references(symbol, limit=200, offset=0)`
- `trace_signal(symbol, direction="upstream", max_depth=4, limit=100)`
- `triage_issue(issue_text, limit=5)`
- `scope_metadata(issue_text=None)`
- `resolve_alarm(name_or_class, limit=10)`
- `decode_summary(tag, limit=50)`
- `aoi_instance_bindings(instance=None, name=None, detail="summary", limit=10, spill=false)`
- `sdk_status()` - optional SDK availability plus the fail-closed allowlist.
- `runtime_evidence_summary(session_id=None)` - compact summary for runtime capture sessions.
- `read_tags_now(path, tags, source="pycomm3")` - one-shot runtime snapshot.
- `start_runtime_capture(path, tags, interval_ms=100, duration_seconds=60, source="pycomm3")` -
  starts the CLI capture subprocess and returns a session id/PID.
- `runtime_capture_status(session_id)` / `stop_runtime_capture(session_id)`.
- `list_runtime_sessions(limit=50, offset=0)`.
- `read_runtime_stream_slice(session_id, tag=None, max_points=200, offset=0)`.
- `runtime_change_points(session_id, tag=None, limit=200, offset=0)`.

### Analysis

- `tag_producers_consumers(name)` - routines that write a tag vs read it.
- `impact_of(name, max_depth=3, limit=300)` - transitive change propagation
  from a tag through the logic (affected routines, tags, and alarms).
- `io_trace(name=None, symbol=None)` - resolve a tag's alias chain to physical I/O, logic, alarms.
- `call_graph(routine=None, program=None)` - callers/callees of a routine, or
  the task/program scheduling tree (including unscheduled programs).
- `run_diagnostics(rules=None, severity=None, limit=50)` - prioritized
  static-analysis findings, filterable by rule and severity.

## Notes

- `.L5X` is the only supported source format in v1.
- `.ACD`, `.L5K`, `.AML`, and `.RDF` are intentionally not parsed yet.
- The extractor never modifies the input `.L5X`.
- Optional pycomm3 runtime reads are isolated from the offline parser. The MCP
  can do one-shot snapshots (`runtime-read-now`) or start a background capture
  subprocess (`runtime-capture-start`) that writes
  `runtime_evidence/sessions/<session_id>.manifest.json`,
  `<session_id>.samples.jsonl`, and `<session_id>.state.json`.
- Optional Logix Designer SDK support is fail-closed scaffolding only unless a
  named read-only capability is explicitly wired later. Upload/download,
  controller mode changes, tag writes, imports, safety/protect/lock, and SD-card
  operations are denied from the normal MCP surface. Runtime evidence belongs in
  `runtime_evidence/`, not in canonical `ir/`.
- Runtime smoke tests can be exercised without a PLC by using `--source fake`:
  ```powershell
  python -m logix_mcp runtime-read-now .\Arnold_0058_029_062226.logix --path FAKE --tag Timer.ACC --source fake
  python -m logix_mcp runtime-capture-start .\Arnold_0058_029_062226.logix --path FAKE --tag Timer.ACC --interval-ms 100 --duration-seconds 5 --source fake
  python -m logix_mcp runtime-sessions .\Arnold_0058_029_062226.logix
  python -m logix_mcp runtime-summary .\Arnold_0058_029_062226.logix --session-id <id>
  python -m logix_mcp runtime-slice .\Arnold_0058_029_062226.logix --session-id <id> --tag Timer.ACC --max-points 200
  ```
- Cross-references are classified from a per-instruction signature table
  (including ALMD/ALMA, which also emit a derived `<tag>.InAlarm` write) and
  from AOI parameter usage, and each row carries a `confidence` (`typed` vs
  `heuristic`). Operands of instructions outside the table remain `heuristic`
  with access `unknown`; ST/SFC AOI-call argument directions are not yet typed.
  The raw routine units and source comments/data are preserved in IR for
  follow-up analysis.
