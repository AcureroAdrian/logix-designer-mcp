# Making FBD and SFC Legible to an LLM

How the visual Logix languages — Function Block Diagram (FBD) and Sequential
Function Chart (SFC) — are stored in L5X and in our IR, why an LLM struggles to
"read" them today, and the concrete IR additions and MCP tools that fix it.

FBD and SFC are **graphs**, not linear text. A model reading the raw IR sees a
flat pile of nodes and wires and has to mentally reconstruct the topology before
it can reason about data flow or sequence. RLL and ST do not have this problem —
they are already lines of text. The goal here is to give FBD/SFC the same
"already linearized" property.

All examples below were pulled from the ingested workspace
`Arnold_0057_022_052226.logix` (4297 fbd_nodes, 3770 fbd_wires, 314 sfc_nodes,
292 sfc_links).

---

## 1. The L5X source model

### 1.1 FBD

```
Routine[Type=FBD]
  FBDContent
    Sheet Number="1"
      IRef  ID X Y Operand                          -- input reference (reads a tag/literal)
      ORef  ID X Y Operand                          -- output reference (writes a tag)
      Block Type ID X Y Operand VisiblePins          -- built-in function block (e.g. MAVE, ADD)
      AddOnInstruction Name ID X Y Operand           -- AOI call block
      ICon  ID X Y Name                              -- input wire connector (off-page / cross-sheet)
      OCon  ID X Y Name                              -- output wire connector
      TextBox ID X Y  (free text, a comment)
      Wire  FromID ToID [FromParam] [ToParam]        -- a directed edge between pins
```

Key facts (confirmed against Rockwell 1756-PM009 FBD Programming Manual and the
1756-RM014 Import/Export reference):

- A **Wire** is a directed edge. `FromID`/`ToID` reference node `ID`s on the
  same sheet. `FromParam`/`ToParam` name the **pin** on the block end; an
  IRef/ORef end has no param (the whole element is the single pin). So
  `Wire FromID=5 FromParam="Out" ToID=4` means "block 5's `Out` pin feeds
  ORef 4."
- A **Block**'s `VisiblePins` lists its pins in order (e.g.
  `"EnableIn In Initialize NumberOfSamples Out"`). Inputs and outputs are
  distinguished only by which side of a wire they appear on (a pin that is ever
  a `ToParam` is an input; a `FromParam` is an output).
- **ICon/OCon are the only cross-sheet / cross-region link mechanism.** They are
  *not* joined by ID — they are joined by **`Name`**. One `OCon Name="LevelInches"`
  is the source; every `ICon Name="LevelInches"` (possibly on another sheet) is a
  sink. This is the FBD equivalent of a named net. In this workspace they are
  rare (2 OCon / 2 ICon) but they exist and break any pure FromID/ToID traversal.
- Coordinates `X`/`Y` are layout only. They are *not* evaluation order. Logix
  evaluates a sheet by data-flow topological sort (with explicit feedback-wire
  handling), not by position.

### 1.2 SFC

```
Routine[Type=SFC]
  SFCContent
    Step ID X Y Operand InitialStep
      Action ID Operand Qualifier (N/P/S/R/L/D...)
        Body/STContent  -- ST statements run while/when the step is active
    Transition ID X Y Operand
      Condition/STContent  -- ST boolean expression; fires when TRUE
    Branch ID BranchType(Selection|Simultaneous) BranchFlow(Diverge|Converge)
    SbrReturn / JSR        -- subroutine entry/return (not present in this dataset)
    DirectedLink FromID ToID  -- directed edge: step->transition->step, etc.
```

- Execution is a state machine: an active **Step** runs its **Action** bodies
  (gated by the action qualifier), then control passes through the outgoing
  **Transition** when its ST condition evaluates TRUE, following the
  **DirectedLink** to the next step.
- **Branch** nodes implement selection (OR — first true transition wins, by
  priority) and simultaneous (AND — all paths active) divergence/convergence.
  The branch carries `BranchType` and `BranchFlow`; the actual paths are the
  DirectedLinks in and out of it.
- Stored state (which step is active, action timers) is runtime, not in the L5X.

---

## 2. What our IR stores today

Source: `src/logix_mcp/routines.py`.

### 2.1 FBD — `ir/fbd_nodes.jsonl`, `ir/fbd_wires.jsonl`

A node row:

```json
{"kind":"fbd_node","id":"AOI:Avg_Draft.Routine:Logic.Sheet:1.Node:5:Block",
 "routine_id":"AOI:Avg_Draft.Routine:Logic","sheet_id":"...Sheet:1","sheet_number":"1",
 "node_type":"Block","node_id":"5","id_on_sheet":"5","instruction":"MAVE","operand":"MAVE_01",
 "visible_pins":"EnableIn In Initialize NumberOfSamples Out","x":"660","y":"160",
 "arrays":[{"name":"StorageArray","operand":"Storage_Array"}, ...]}
```

A wire row:

```json
{"kind":"fbd_wire","id":"...Sheet:1.Wire:0004","from_id":"5","from_param":"Out","to_id":"4"}
```

**Good news — wire connectivity is fully preserved.** `from_id`, `to_id`,
`from_param`, `to_param` are all carried through verbatim (lines 473–476). The
graph *is* reconstructable from the IR. `visible_pins` preserves pin order.
`node_id`/`id_on_sheet` line up with the wire endpoints. This is the hard part
and it is already correct.

**Gaps:**

1. **`node_type` whitelist drops ICon/OCon.** `FBD_NODE_ELEMENTS`
   (`routines.py:19`) = `{IRef, ORef, Block, AddOnInstruction, TextBox}`. ICon
   and OCon are silently skipped, so any wire that crosses a connector
   dead-ends at an ID that has no node row, and named-net continuity is lost.
   (Confirmed: the L5X has 2 OCon + 2 ICon that never appear in `fbd_nodes`.)
2. **No pin direction / no input-vs-output classification.** A consumer has to
   scan all wires to learn which `visible_pins` entries are inputs vs outputs.
3. **No evaluation/topological order.** Nodes are emitted in document order;
   there is no field saying "evaluate node 2, then 5, then write 4."
4. **No linearized / data-flow rendering.** The `body_lines` text
   (`_fbd_node_text`, line 595) is one independent line per node
   (`"IRef 0 Active"`, `"Block 5 MAVE MAVE_01"`) with **no wiring** — it lists
   parts, not the circuit. An LLM reading `body` cannot see that A feeds the
   MAVE which feeds Out.

### 2.2 SFC — `ir/sfc_nodes.jsonl`, `ir/sfc_links.jsonl`

This side is in better shape for content. Step rows embed their Actions with the
full `st_body`; Transition rows carry `condition_body` (the ST expression
inline); Branch rows carry `branch_type`/`branch_flow`. Links carry
`from_id`/`to_id`.

```json
{"node_type":"Transition","node_id":"120","operand":"JCAL_Tran_015",
 "condition_body":"Cal_Stick.Active_Cal > 0 and Cal_Stick.Cal_Success"}
{"kind":"sfc_link","from_id":"0","to_id":"165"}
```

**Gaps:**

1. **No assembled step→transition→step sequence.** The chart order lives only in
   `sfc_links` (FromID/ToID); nobody walks it. To know what runs after
   `JCAL_STEP_000` you must join Step 0 → link(0→165) → node 165 → its outgoing
   link, by hand.
2. **Branch semantics are not expanded into readable paths.** A Branch row tells
   you "Selection/Diverge" but not "if T1 go to step A, elif T2 go to step B."
3. **No designated entry point surfaced per chart** (the `InitialStep`) as a
   starting node for the walk.

---

## 3. The legibility gap, concretely

To "read an FBD sheet as data flow" an LLM needs to produce something like:

```
Out <= MAVE_01.MAVE( In=Raw, Initialize=Reset, NumberOfSamples=Filter_Points,
                     EnableIn=Active )
```

To do that from today's IR it must, unaided: (a) collect all nodes for the
sheet, (b) build an adjacency map from wires keyed by `to_id`/`to_param`, (c)
infer which pins are inputs vs outputs, (d) recursively substitute each input
pin's driver (an IRef → its operand; a Block → a nested call), (e) topologically
order so each ORef/OCon is expressed in terms of leaves (IRefs/ICons/literals),
and (f) patch ICon/OCon named nets back together across sheets — which is
**impossible** today because those nodes are dropped (gap 1 above).

That is a multi-step graph algorithm we are currently asking the *model* to run
in its head over thousands of nodes. It will guess and get it wrong on anything
non-trivial. The fix is to run that algorithm in Python once, at ingest or
on-demand, and hand the model the finished linearization.

The same argument applies to SFC: the model should be handed
`STEP_000 → [Tran_015: Active_Cal>0 and Cal_Success] → STEP_008 → ...` rather
than a bag of steps and a separate bag of links.

---

## 4. Recommendations

### 4.1 IR additions (cheap, do these first)

1. **Capture connectors.** Add `ICon`, `OCon` to `FBD_NODE_ELEMENTS`
   (`routines.py:19`). Store `connector_name` (the `Name` attr). This alone
   un-breaks cross-sheet traversal.
2. **Classify pins on each Block/AOI node.** From the sheet's wires, derive
   `input_pins` (pins seen as `to_param`) and `output_pins` (pins seen as
   `from_param`), plus any `visible_pins` not wired. Emit as
   `pins:[{name, dir, source_node_id|null}]`. Removes step (b)/(c) above.
3. **Emit `eval_order` per sheet.** Topologically sort nodes by wire dependency
   (IRefs/ICons/literals first; ORefs/OCons last; mark any edge that creates a
   cycle as a `feedback` wire so the sort still terminates). Store an integer
   `eval_index` on each node and/or an ordered `eval_order:[node_id,...]` on the
   `fbd_sheet` unit.
4. **Replace the per-node `body_lines` with a linearized sheet** (see 4.2) so
   the routine's `body` text — which is what generic search/LLM reads — actually
   shows the circuit.
5. **SFC: emit an assembled sequence.** On the `sfc_chart` unit, store a walk
   starting from the `InitialStep`, following `sfc_links`, expanding Branch
   nodes into labelled paths, e.g. a `sequence` list of
   `{step, actions:[...], transitions:[{to_step, condition}]}`.

### 4.2 Linearized FBD rendering (the headline change)

A pseudo-equation form, one equation per sink (ORef / OCon), inputs substituted
recursively, blocks rendered as `Operand.INSTR(pin=arg, ...)`. Shared
sub-expressions (a block feeding two sinks) are emitted once as a `let` and
referenced, to avoid duplication:

```
SHEET 1  (eval order: IRef Active, IRef Raw, IRef Reset, IRef Filter_Points, MAVE_01, ORef Out)

Out := MAVE_01.MAVE(
         EnableIn        = Active,
         In              = Raw,
         Initialize      = Reset,
         NumberOfSamples = Filter_Points)        ; arrays: StorageArray=Storage_Array, WeightArray=Weights
```

Cross-sheet example:

```
SHEET 1:  net "LevelInches" := <expr driving OCon 19>
SHEET 2:  VolumeLiters := SomeBlock( In = net "LevelInches" )   ; via ICon 55
```

Rules: IRef → its operand or literal; ORef/OCon → left-hand side; Block/AOI →
`Operand.Type(pin=arg,...)` with unwired pins omitted or shown as `=<default>`;
TextBox → `; comment`; a wire flagged `feedback` renders as
`<prev-scan value of X>` so the equation stays acyclic and readable.

### 4.3 Proposed MCP tools

```python
get_fbd_sheet(routine_id: str, sheet: int | str = 1,
              form: Literal["equations", "graph", "both"] = "equations")
    -> {
        "routine_id": str, "sheet": str,
        "equations": [str, ...],          # the 4.2 linearization, one per sink
        "eval_order": [node_id, ...],
        "nodes":  [{node_id, type, instruction, operand, pins:[{name,dir,source}]}],
        "wires":  [{from_id, from_param, to_id, to_param, feedback: bool}],
        "connectors": [{name, kind: "ICon"|"OCon", node_id, sheet}],
        "text_boxes": [str, ...],
        "unresolved": [ ... ]             # wires whose endpoints are missing, etc.
      }
```

`form="equations"` returns just the readable text (the default an LLM wants);
`graph` returns the typed node/wire lists for programmatic use; `both` returns
everything.

```python
get_sfc_sequence(routine_id: str)
    -> {
        "routine_id": str,
        "initial_step": str,
        "sequence": [                      # walk from initial_step over the links
           {"step": "JCAL_STEP_000",
            "actions": [{"qualifier":"N","operand":"JCAL_Action_000","st":"..."}],
            "transitions": [{"to": "JCAL_STEP_008",
                             "condition": "Cal_Stick.Active_Cal > 0 and Cal_Stick.Cal_Success"}]},
           ...],
        "branches": [{"id","type":"Selection","flow":"Diverge",
                      "paths":[{"transition_condition": "...", "to_step": "..."}]}],
        "text": "..."                      # rendered step -> [cond] -> step ladder
      }
```

Rendered `text` an LLM can read directly:

```
* STEP JCAL_STEP_000  (initial)
    N  JCAL_Action_000:  Cal_Stick.Center_Pt := Cal_Stick.Axis; ... ; Cal_Stick.Capture.Dir2_Min := 17000;
    --[ Active_Cal > 0 and Cal_Success ]--> JCAL_STEP_008
* STEP JCAL_STEP_008
    N  JCAL_Action_007:  if (ABS(...) > 200) and ... then NEU_Success := 1; ...
    ...
```

Both tools are pure functions over the existing IR plus the 4.1 additions — no
re-ingest of the L5X required, and they can be backed by the same topo-sort code
that produces the new `eval_order`/linearized `body`.

---

## 5. Priority

1. **(4.2 + 4.3 `get_fbd_sheet`)** linearized data-flow equations — highest
   impact; this is the thing that makes an FBD sheet readable at all.
2. **(4.1.1)** capture ICon/OCon — small fix, prevents silently wrong traversal.
3. **(4.1.2 / 4.1.3)** pin direction + eval_order fields — enable #1 and any
   downstream analysis.
4. **(4.3 `get_sfc_sequence`)** assembled SFC walk.

---

## Sources

- [Logix 5000 Controllers Function Block Diagram Programming Manual (1756-PM009)](https://literature.rockwellautomation.com/idc/groups/literature/documents/pm/1756-pm009_-en-p.pdf)
- [Logix 5000 Controllers Import/Export (1756-RM014)](https://literature.rockwellautomation.com/idc/groups/literature/documents/rm/1756-rm014_-en-p.pdf)
- Observed L5X and IR in `Arnold_0057_022_052226.logix`; normalizer `src/logix_mcp/routines.py`.
