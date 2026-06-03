# Cross Reference Tool Spec

Design for an MCP tool, `cross_reference`, that reproduces Studio 5000 Logix
Designer's **Cross Reference** feature for an LLM. Cross Reference is the core
navigation tool electricians and engineers use to answer "where is this tag
used, and what writes it?"

---

## 1. What Logix Designer's Cross Reference shows

Source: Rockwell Automation Studio 5000 Logix Designer online help (v37),
"Cross Reference", "Cross reference dialog box parameters", "Reference
Information by Tag Hierarchy", "Next Destructive Reference command"; corroborated
by PLCtalk and field-engineering write-ups (see Sources).

The Cross Reference function **searches the entire project** and finds every
occurrence of the selected element (tag, tag member/bit, instruction, or
routine). Results can be viewed by logic, by tag, by tag hierarchy, or by
connection.

### Columns (logic / tag view)

| Column | Meaning |
|---|---|
| **Element** | The element being referenced — the tag, tag member/bit, or instruction name as written at that location. |
| **Container** | The program or controller scope (and/or Add-On Instruction) that contains the routine. Controller-scoped tags show the controller as container. |
| **Routine** | The routine in which the reference occurs. |
| **Location** | Position inside the routine: `Rung n` for Ladder (RLL), `Line n` for Structured Text (ST), or sheet/element for FBD/SFC. |
| **Reference** | The instruction/usage at that location (e.g. `XIC`, `OTE`, `MOV`, an AOI call, or an ST expression). This is the **Reference type**. |
| **Destructive** | `Y` / `N`. `Y` when the reference can **overwrite/modify** the element's value at that location; `N` for a pure read. |
| **Scope** | (tag view) Controller vs program scope of the tag. |
| **Type / Name** | (component views) the component data type and name. |

A **Show** menu lets the user pick which columns are visible. The
**Next/Previous Destructive Reference** commands jump only between rows where
Destructive = `Y` — the canonical troubleshooting workflow ("what last wrote
this tag?").

### Definition of Destructive vs Reference

> "If an instruction can overwrite a tag value, the instruction is a destructive
> reference for that tag." In Logix5000, *destructive* means **the contents of
> the addressed memory element can be changed by the instruction.**

Key points:

- Destructive is a property of **the operand position within a specific
  instruction**, not of the tag globally. `MOV(A, B)` is destructive for `B`,
  non-destructive for `A`.
- Output/latch/unlatch bit instructions (`OTE`, `OTL`, `OTU`, `RES`) are
  destructive for their bit. Examine/contact instructions (`XIC`, `XIO`) are
  reads.
- Timer/counter control instructions (`TON`, `CTU`, …) are destructive for the
  control structure (they update `.ACC`/`.DN`/etc.), reads of preset/accum
  operands.
- **Bit members** roll up under the base tag: a reference to `Tag.3` or
  `Word.Bit` appears in the cross reference of both the bit and (by hierarchy)
  the parent word. Tag-hierarchy view groups members beneath their base tag.
- **Alias tags** are resolved to their base: cross-referencing a base tag
  surfaces references made through any alias that points at it, and vice versa.
  Logix follows the alias chain to the base before listing references.
- **Add-On Instruction (AOI) parameters**: an AOI call's arguments are
  destructive or not according to the parameter **Usage** — `Input` = read,
  `Output` = destructive, `InOut` = destructive (read+write). The AOI's internal
  logic also cross-references its own local/parameter tags within the AOI
  definition (Container = the AOI).
- **Unknown / "?"**: where Logix cannot determine usage it leaves the
  Destructive cell blank/ambiguous rather than asserting `Y` or `N`.

---

## 2. Our IR schema vs Logix columns

Our `xrefs.jsonl` row (confirmed from `Arnold_0057_022_052226.logix/ir`):

```json
{"symbol":"Dest","routine":"AOI:CombineBytes_8.Routine:Logic","access":"write",
 "instruction":"BTD","confidence":"typed","language":"RLL",
 "source":"...Rung:0","location":"Rung 0","operand":"Dest","base_symbol":"Dest",
 "routine_name":"Logic","owner":"AOI:CombineBytes_8"}
```

Program-scoped rows additionally carry `"program":"AUX_EQUIP"`.

### Field mapping

| Logix column | Our field(s) | Notes |
|---|---|---|
| Element | `operand` / `symbol` | `operand` is the as-written operand; `symbol` is the resolved tag. |
| Container | `program` or `owner` | `owner` = `Program:NAME` or `AOI:NAME`; `program` present for program scope. Controller scope not yet distinguished by an explicit flag. |
| Routine | `routine_name` (also `routine`, `source`) | `routine` is the fully-qualified id; `routine_name` is the short name. |
| Location | `location` (`Rung n` / line) | Good; rung **number** is embedded in the string, not a separate int. |
| Reference (type) | `instruction` | `BTD`, `OTE`, `ST_ASSIGN`, AOI name, etc. |
| **Destructive (Y/N)** | derived from `access` | `access ∈ {read, write, read_write, unknown, call}`. No literal boolean. |
| Scope | partial | inferable from `owner`/`program`; no controller-vs-program flag. |
| Confidence (no Logix analog) | `confidence` (`typed`/`heuristic`) | Our honesty signal; maps to Logix "?" when low. |

### Distribution (first 5k rows, Arnold)

`access`: read 3111, write 1417, read_write 417, unknown 44, call 11 ·
`confidence`: typed 4375, heuristic 625 · `language`: RLL 4419, ST 357, FBD 224.

### Gaps

1. **(a) No literal `destructive` boolean.** We have `access`, but never expose
   the single Y/N column that is the heart of the feature and drives
   Next-Destructive navigation.
2. **(b) No neutral-text snippet.** Rows lack the instruction text at the
   location (e.g. `MOV(Counter,Total)`), so an LLM can't see context without a
   second `get_routine` round-trip.
3. **(c) No bit/member rollup.** `find_references` matches `symbol`,
   `symbol.%`, `symbol[%` by SQL LIKE, so a query for `Word` finds `Word.3`, but
   there is no aggregation/grouping of members under the base, and no reverse
   (query a bit → see the parent word's other references).
4. **(d) No alias resolution.** Aliases are not followed; cross-referencing a
   base tag will miss references written through an alias name (and vice versa).
5. Minor: rung number is a string, not an int (sorting/pagination); no
   controller-vs-program scope flag.

---

## 3. Classifier: how close is access → Destructive

`src/logix_mcp/xrefs.py` drives `access` from a per-operand
**`INSTRUCTION_SIGNATURES`** table: each instruction maps to a tuple of operand
roles (`READ` / `WRITE` / `READ_WRITE` / `CALL`) plus a `rest` role for trailing
operands. Classification is **positional** (`classify_ladder_instruction`), so
literals/expressions stay aligned. AOI calls are classified from parameter
`Usage` via `aoi_signature_from_parameters` (`Input→read`, `Output→write`,
`InOut→read_write`); operand 0 is the backing tag (`read_write`). Hits from the
table or an AOI are `confidence="typed"`; anything else falls back to
`access="unknown"`, `confidence="heuristic"`. ST assignments mark LHS `write`,
RHS reads `read`.

**Mapping to Logix Destructive:** clean and direct.

```
destructive = access in {"write", "read_write"}   # Y
              access == "read"                     # N
              access in {"unknown"}                # ? (omit / null)
              access == "call"                     # N/A (JSR/AOI invocation, not an operand)
```

`write` and `read_write` together exactly capture Logix's "can change the
contents of the memory element" — `read_write` (timers, `ONS`, AOI `InOut`,
backing tags) is correctly destructive, matching Logix marking timer-control and
latch operands `Y`. The signature table covers the common ControlLogix set;
unlisted/ambiguous instructions surface honestly as `unknown` → `?`, which is
*better* than Logix (which silently blanks). Confidence should be surfaced so an
LLM can hedge on heuristic rows.

---

## 4. Proposed tool: `cross_reference(symbol, scope=None, …)`

Returns Logix-style rows plus an aggregated header. **Token-cheap by default:**
the header (counts) is always returned; rows are paginated and start at a small
page. An LLM can answer "is this tag written anywhere, and where?" from the
header alone, then page into rows only if needed.

### Parameters

| Param | Type | Default | Meaning |
|---|---|---|---|
| `symbol` | string | — | Tag, member/bit, instruction, or routine to cross-reference. |
| `scope` | string? | `null` | Restrict to a program/controller/AOI container name. |
| `destructive_only` | bool | `false` | Return only `destructive=true` rows (the Next-Destructive workflow). |
| `language` | string? | `null` | Filter `RLL`/`ST`/`FBD`/`SFC`. |
| `include_aliases` | bool | `true` | Resolve alias↔base so references through either name are included. |
| `rollup_members` | bool | `true` | Include bit/member references of the base and group them. |
| `page` | int | `1` | 1-based page. |
| `page_size` | int | `25` | Rows per page (cap, e.g. 200). |
| `include_snippet` | bool | `true` | Include neutral-text snippet per row. |

### Response schema

```json
{
  "symbol": "Counter",
  "resolved": {
    "base_symbol": "Counter",
    "aliases": ["MachineCount"],
    "members_seen": ["Counter.ACC", "Counter.DN"]
  },
  "summary": {
    "total": 42,
    "destructive": 7,
    "reads": 33,
    "unknown": 2,
    "by_program": { "AUX_EQUIP": 18, "MAIN": 24 },
    "by_language": { "RLL": 40, "ST": 2 },
    "by_instruction": { "OTE": 6, "XIC": 30, "MOV": 4, "...": 2 },
    "destructive_locations": [
      { "container": "MAIN", "routine": "R10_Logic", "location": "Rung 12", "instruction": "MOV" }
    ]
  },
  "page": 1,
  "page_size": 25,
  "total_pages": 2,
  "rows": [
    {
      "element": "Counter.ACC",
      "container": "MAIN",
      "scope": "program",
      "routine": "R10_Logic",
      "location": "Rung 12",
      "rung": 12,
      "instruction": "MOV",
      "reference": "MOV",
      "destructive": true,
      "language": "RLL",
      "snippet": "MOV(Total, Counter.ACC)",
      "confidence": "typed",
      "via_alias": null
    }
  ]
}
```

### Field derivations / behaviors

- `destructive`: `access ∈ {write, read_write}` → `true`; `read` → `false`;
  `unknown` → `null` (renders as `?`).
- `element`: `operand` (as-written); `container`: `program` else `owner`
  stripped of `Program:`/`AOI:` prefix; `scope`: `controller`/`program`/`aoi`
  inferred from `owner`; `routine`: `routine_name`; `rung`: int parsed from
  `location`.
- `snippet`: neutral text of the rung/line. **New IR field needed** (gap b) —
  store the rung's neutral text on the xref row at ingest, or fetch lazily from
  the routine unit keyed by `source`. Lazy fetch keeps `xrefs.jsonl` small.
- `via_alias`: the alias name a reference came through, else `null` (gap d).
- **Default token budget:** `summary` + first 25 rows. For broad tags an LLM
  reads `summary` (esp. `destructive`, `destructive_locations`, `by_program`)
  and stops; it pages only when it needs every site.
- Ordering: destructive rows first, then by container/routine/rung — mirrors the
  troubleshooting flow.

### Implementation notes

- Reuse `db.find_references` (already matches base + `.member` + `[index]`).
  Add a `destructive_only` SQL filter on `access`, and `GROUP BY` for the
  summary so counts don't require loading all rows.
- Alias resolution (gap d): join an alias map (from `symbols.jsonl`/entities) to
  expand `symbol` to its base and any aliases before the lookup.
- Member rollup (gap c): when `rollup_members`, the existing LIKE prefix match
  already gathers members; add `resolved.members_seen` and group counts.
- Snippet (gap b): prefer lazy fetch from the routine unit by `source` to avoid
  growing the 13 MB `xrefs.jsonl`.

---

## Sources

- [Cross Reference (Studio 5000 v37 help)](https://www.rockwellautomation.com/en-us/docs/studio-5000-logix-designer/37-00/contents-ditamap/studio-5000-logix-designer/cross-reference.html)
- [Cross reference dialog box parameters](https://www.rockwellautomation.com/en-us/docs/studio-5000-logix-designer/37-00/contents-ditamap/studio-5000-logix-designer/cross-reference/cross-reference-dialog-box-parameters.html)
- [Next Destructive Reference command](https://www.rockwellautomation.com/en-us/docs/studio-5000-logix-designer/37-00/contents-ditamap/studio-5000-logix-designer/cross-reference/next-destructive-reference-command.html)
- [Reference Information by Tag Hierarchy](https://www.rockwellautomation.com/en-nl/docs/studio-5000-logix-designer/37-00/contents-ditamap/studio-5000-logix-designer/cross-reference/reference-information-by-tag-hierarchy.html)
- [Logix 5000 Controllers Add-On Instructions (1756-PM010)](https://literature.rockwellautomation.com/idc/groups/literature/documents/pm/1756-pm010_-en-p.pdf)
- [Troubleshooting a ControlLogix Output (Bryce Automation)](https://bryceautomation.com/index.php/2018/07/22/troubleshooting-a-controllogix-output/)
- [How to Use Cross-Reference in Studio 5000](https://www.automationreadypanels.com/plc-systems/use-cross-reference-studio-5000-fix-plc-errors/)
- [Definition of "Destructive Bit" in RSL 5000 (PLCtalk)](https://www.plctalk.net/forums/threads/definition-of-destructive-bit-in-rsl-5000.78921/)
