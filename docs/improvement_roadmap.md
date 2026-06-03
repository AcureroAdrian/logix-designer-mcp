# Logix MCP — Improvement Roadmap

Synthesis of a 6-agent investigation (3 research + 3 empirical field tasks against the
real Arnold project). The goal: make the tool *smarter for analysis* and *stop burning
the context window*. Every recommendation below is grounded either in an official Rockwell
spec finding or in a measured cost from an agent that actually tried to answer a real
plant question.

Companion docs produced alongside this one:
- `docs/logix_language_reference.md` — syntax/semantics of the 5 languages + destructive-vs-reference instruction table.
- `docs/fbd_sfc_representation.md` — how visual languages are stored + linearization proposal.
- `docs/cross_reference_spec.md` — Logix Cross Reference feature + `cross_reference()` tool design.

---

## 0. The single biggest finding

**The MCP server is not connected to the analyst session, and the IR's cheapest
asset — an FTS5 full-text index already built into `index/logix.sqlite`
(`search_index`, ~78k rows) — is invisible to models.** So every model that has
analyzed Arnold fell back to `rg` over multi-MB `.jsonl` files (`xrefs` 13 MB,
`symbols` 12 MB, `comments` 5.9 MB, `data_values`/`tag_data` 58 MB each). A single
`rg "MANIFOLD" comments.jsonl` returns thousands of lines — that is the context bomb
the user described. Both empirical agents that *did* know to use sqlite stayed cheap;
the cost spikes were always (a) a naive whole-file read of an FBD routine markdown, or
(b) the rg trap they were lucky to avoid.

Two conclusions drive the whole roadmap:
1. **Make the cheap path the obvious path** (connect the MCP, expose `search`/`exists`
   over the existing FTS index, advertise them).
2. **Reshape the expensive outputs** (FBD routine markdown and the per-routine
   References table are the worst offenders).

---

## 1. Measured context offenders (from the empirical runs)

| Source | Observed cost | Why | Fix theme |
| --- | --- | --- | --- |
| Whole-file read of an FBD routine `.md` (`R08_AUX_MOTOR.md` 1004 lines, `R07_SR01_AUX_GEN_MAP.md` ~630) | ~25–40k tokens, 60–70 % of a run | Flat IRef/ORef/Block/Wire dump for *every* sheet, no wiring shown | Output shaping (§4) + FBD trace tool (§3) |
| Per-routine `## References` table | ~⅔ of an FBD routine file | Auto-generated read/write/instr/rung dump that restates the logic | Output shaping (§4) |
| `rg`/naive read over `*.jsonl` | thousands of lines if it happens | Files are 1–58 MB | `search`/`exists` over FTS (§5) |
| Broad `LIKE '%2-2%'` style search | ~30 lines of noise | Array-bit comments (`52-2` breaker) with no resolved parent tag | Search shaping (§5) |
| Reading the wrong routine to find out what it does | 237 wasted lines | Only line-count available to triage | Summary-first view (§4) |

---

## 2. What the field tasks actually needed (convergent asks)

Three independent empirical agents, three different subsystems (gland pump HMI status,
blower Auto permissive, genset alarms), converged on the *same* top request:

> **"Give me the boolean expression that drives this coil/output, resolved through FBD
> wires and into AOI instances — instead of making me reconstruct the graph by hand."**

That is the headline feature. Everything else is supporting cast.

---

## 3. New MCP tools (analysis intelligence)

### 3.1 `trace_signal(symbol, direction="upstream", max_depth=N)` — **TOP PRIORITY**
Walk `fbd_wires` + RLL rung structure + step into AOI instances and return a *flattened
boolean / data-flow expression* feeding (upstream) or fed by (downstream) a symbol.

- Example output for the blower case:
  `DP1.Blower[2] Start_Mtr := Mode∈{2,6} AND System_On AND HMI_Auto AND System_Enable AND (Permissive OR Permissive_bypass) AND NOT(Run_Input_St) AND NOT(Tripped) AND NOT(Trip) AND NOT(Comm_Flt)`
- Resolves IRef/ORef/Block/Pin connectivity so the model never reads the raw wire table.
- **Replaces ~600 lines of node tables with ~15–30 lines.** Both field agents ranked
  this #1; saves an estimated ~20k tokens per investigation.

### 3.2 `cross_reference(symbol, scope=None, destructive_only=False, page=...)`
Logix-style Cross Reference. Already ~90 % present in `xrefs.jsonl`.
- Add the literal **`destructive` boolean** (trivial: `access ∈ {write, read_write}` → true).
- Add a **neutral-text snippet** per row (lazy-fetch by `source`, so the 13 MB file stays small).
- Return a **summary header first** (counts: total / destructive / by_program / by_language /
  by_instruction) then **paginated rows**. See `docs/cross_reference_spec.md`.
- Roll up bit/member references under the base tag; resolve aliases to base.

### 3.3 `resolve_alarm(name_or_class, include=[...])`
One call: alarm → `assoc_tags` source tag → trip expression (or "none") → scaling/decode
rung → human label (comment vs `%AlarmName` message) → HMI message stub.
- The genset task needed 6 manual stitches; this collapses them to one.
- Crucially it surfaces **"this alarm has no trip in this controller"** immediately — the
  exact insight behind the "alarms that aren't real" complaint.

### 3.4 `decode_summary(tag)`
Expand a summary/OR coil (e.g. `DP2_GLAND_PUMP_SUMMARY_ALARM`) into its member bits with
comments and alarm type, in one shot. Scales to the 1190-row alarm table.

### 3.5 `aoi_instance_bindings(instance)`
Per-instance AOI pin table: `param | usage(In/Out/InOut) | wired? | source_expr | default`.
- **Explicitly lists UNWIRED pins** — this is how the blower agent found the `Permissive`
  pin defaulting to 0 across all four blowers. Removes the need to diff node lists by hand.

### 3.6 `get_fbd_sheet(routine_id, sheet, form="pseudo")` and `get_sfc_sequence(routine_id)`
- FBD: topo-sort the sheet and emit pseudo-equations
  `Out := MAVE_01.MAVE(In=Raw, ...)`, inputs substituted recursively. Turns a 4297-node
  graph the LLM cannot traverse into ST-like text it reads natively.
- SFC: walk `sfc_links` and emit the `step --[ST condition]--> step` ladder (the order
  currently only lives implicitly in the links).

---

## 4. Output-shaping rules (kill the bloat) — **HIGH PRIORITY, LOW EFFORT**

These attack the measured #1 and #2 context offenders directly.

1. **Summary-first routine header.** Every routine view (md + `get_routine_context`) leads
   with a ≤5-line block: `purpose`, `written_tags`, `read_tags`, `subsystems/devices touched`,
   `language`, `#rungs/#sheets`. Lets a model triage without reading the body.
   - e.g. `R04_Permit` → "main MV drive permit (motor temp, gearcase, LCS, comms)";
     `R08_Aux_Motor` → "gland pumps + blowers aux-motor control".
2. **Section selector on reads.** `read_routine(name, section="summary"|"logic"|"refs", sheet=N,
   around_symbol=...)`, default = `summary`+`logic`. Reading only Sheet 3 would have turned
   a 25k-token read into ~1.5k.
3. **Relocate the `## References` table** out of the routine body into a separate artifact
   (or behind `section="refs"`). It is ~⅔ of an FBD routine file and restates the logic.
4. **Pagination + summary header is the default contract** for every list/search tool
   (counts and facets first, rows on request). No tool should be able to dump thousands of
   rows in one call.

---

## 5. Anti-`rg` search primitives (leverage the FTS index that already exists)

`index/logix.sqlite` already has an FTS5 `search_index`. Surface it:

1. **`search(term, in=["comment","tag_name","message","alarm","routine"], limit=20)`**
   → `[{kind, target, snippet, scope}]`, server-side capped. The sanctioned replacement for
   `rg *.jsonl`.
2. **`exists(terms[], scopes[])` → `{term: hit_count}`** (counts only, no bodies). Makes
   *negative* findings cheap and trustworthy — the genset task's key result was "MAT/ACWT/
   TT400/fuel-leak exist nowhere in this controller," and a model is otherwise tempted to
   grep bodies "just to be sure," which is where context burns.
3. Search rows must carry the **resolved parent tag** for `.bit` / `[idx]` comments and a
   `kind`/`scope` filter, so `%2-2%`-style queries stop returning breaker `52-2` noise.

---

## 6. IR / ingestion changes (require re-ingest)

1. **Capture `ICon`/`OCon` connectors** — add to `FBD_NODE_ELEMENTS` (`routines.py:19`).
   Confirmed gap: cross-sheet named-net connectors vanish today, so any wire crossing them
   dead-ends. Prerequisite for reliable `trace_signal`/`get_fbd_sheet`.
2. **Add FBD `eval_order` + pin-direction fields** to `fbd_nodes`/`fbd_wires` so linearization
   doesn't re-run topo-sort at query time.
3. **`destructive` boolean** materialized on `xrefs` rows (don't recompute from `access` each call).
4. **Scope/metadata awareness:** record ingested-vs-referenced-but-absent external modules
   (e.g. the far side of the `MAINPLC_PROSOFT_EGEN25` ProSoft gateway, the HMI/SCADA tag DB).
   Lets tools answer **"out of this project's scope"** instead of a misleading "not found."
5. **(Optional) HMI binding dataset** if a FactoryTalk/PanelView export is ever available —
   map graphic object → expression → tag. Today the HMI animates off published tags
   (`*.Status_Word.RunSt`, `*_SUMMARY_ALARM`) with no PLC graphic logic; at minimum record
   that note so models stop hunting for color logic in the controller.

---

## 7. Discoverability

1. **Add `.mcp.json`** so the analyst session actually has the tools (today it doesn't;
   models fall back to files + `rg`).
2. **`project_summary()` should return a short "how to analyze" preamble** pointing at
   `search`/`exists`, `trace_signal`, `cross_reference`, and "never `rg` the jsonl; use the
   FTS index." A one-paragraph cheat sheet at the top of the most-called tool changes default
   behavior for free.

---

## 8. Suggested sequencing

| Phase | Items | Effort | Payoff |
| --- | --- | --- | --- |
| **P0** | `.mcp.json` (§7.1); `search`/`exists` over existing FTS (§5); summary-first + `section=` routine views, relocate References table (§4) | Low | Kills the two measured context offenders + the rg trap immediately |
| **P1** | `trace_signal` (§3.1); `cross_reference` with destructive+snippet+summary (§3.2); `resolve_alarm` + `decode_summary` (§3.3–3.4) | Medium | The analysis-intelligence the field tasks demanded |
| **P2** | Capture `ICon`/`OCon` + `eval_order` re-ingest (§6.1–6.2); `get_fbd_sheet`/`get_sfc_sequence` (§3.6); `aoi_instance_bindings` (§3.5); scope metadata (§6.4) | Higher | Makes visual languages first-class; needs re-ingest + validation |

---

## Appendix — field answers the agents produced (real deliverables for the issue list)

- **Gland pump 2-2** = `DP2.Gland_Pump_2`. HMI green = `...NXP_Data.In.Status_Word.RunSt`;
  red = `DP2_GLAND_PUMP_SUMMARY_ALARM`. Start permit = `DP2_G2_start_cmd` (BAND chain in
  `DP2/R08_Aux_Motor` sheet 5: service-water interlock + Port_ON + not-Standby + not-E-Stop +
  lead/lag `DP2_ACTIVE_GLND2_HMI`). "Ready in auto but red" = ready ≠ running; most likely
  it is the **lag/standby** pump not selected by lead/lag. Confidence: high on chain, medium
  on which single permit input is open.
- **Blower #3** = `DP1.Blower[2]`. Command in `DP1/R08_AUX_MOTOR` sheet 3 via AOI
  `MCC_Type1_Starter_03`; start gate at AOI `Logic` Rung 3. Most likely field cause, in order:
  HOA not in Remote (Mode≠2/6) → latched trip in `DP1_ALARM.Dig_Alm[34..41]` → `Comm_Flt` →
  `Port_ON`/E-Stop. Observed design note: `Permissive`/`Permissive_bypass` pins look unwired
  on all four blowers (uniform, so not the differential cause).
- **GE engine alarms** = auxiliary genset (`AUX_GEN` class / `AUX_GEN_DATA` UDT) over ProSoft
  `MAINPLC_PROSOFT_EGEN25`, logic in `AUX_EQUIP/R07_SR01_AUX_GEN_MAP`. (a) "General warning,
  no specific indication" is structural: a flat `AUXGEN_ALARM[0..22]` bit array whose labels
  live only in tag comments, with message strings as `%AlarmName` stubs resolved at the HMI.
  (b) Manifold Air Temp, Inter/After Cooler Water Temp, High-Pressure Fuel-Oil Leak, and the
  TT400 lube-oil-inlet differential **do not exist anywhere in this controller** — they are
  large-bore main-engine terminology; the source is a different controller/HMI page, so they
  "sound but aren't real" relative to this project. Confidence: high.
