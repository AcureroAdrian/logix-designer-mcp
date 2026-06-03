# Studio 5000 Logix Designer — Language Reference for L5X Analysis

Reference-grade notes for an LLM reading `.L5X` XML exports and the derived IR in this
workspace. Covers the five Logix Designer programming languages plus AOI/UDT structure.
All examples are pulled from the real source file
(`Arnold_0057_022_052226.L5X`, controller 1756-L85E rev 34) or its IR
(`Arnold_0057_022_052226.logix/ir/*.jsonl`).

**Primary sources:** Rockwell Automation **1756-RM003** (General Instructions),
**1756-PM007** (Structured Text), **1756-RM084 / 1756-RM014** (Import/Export, L5X XML schema),
**1756-PM001** (Common Procedures), **1756-RM087** (Function Block / SFC attributes),
and IEC 61131-3 (the underlying standard for ST, FBD, SFC, LD).

---

## 0. Where logic lives in the L5X tree

```
Controller
  DataTypes/DataType .............. UDTs (Class="User")
  AddOnInstructionDefinitions/AddOnInstructionDefinition
      Parameters/Parameter ........ AOI interface (Usage/Required/Visible)
      LocalTags/LocalTag .......... AOI-private working storage
      Routines/Routine ............ the AOI body (usually one routine "Logic")
  Tags/Tag ........................ controller-scope tags
  Programs/Program
      Tags/Tag .................... program-scope tags
      Routines/Routine
          RLLContent | STContent | FBDContent | SFCContent
  Tasks/Task ...................... scan scheduling (which programs run)
```

A `Routine` carries `Type="RLL|ST|FBD|SFC"`. The body element is keyed to that type.
Programs are linked to logic only through `JSR` calls from their `MainRoutineName`
(here e.g. `Program@MainRoutineName="Call_Routines"`); there is **no** implicit fall-through
between routines.

---

## 1. The five languages in XML (with real examples)

### 1a. Ladder / RLL — `RLLContent` → `Rung` → `Text` (CDATA)

```xml
<RLLContent>
<Rung Number="0" Type="N">
<Text><![CDATA[XIC(AFE_In[30].0)OTE(AFE.IN.Status.Ready_On);]]></Text>
</Rung>
</RLLContent>
```

- `Rung@Type`: `N` = normal. (`I`/`D`/`R` appear on rungs under online edit; not present here.)
- Logic is **neutral text** inside `<Text>` CDATA — see §2.
- IR: `routines.jsonl` → `rungs[].text` plus a pre-tokenized `instructions[]` array
  (`{"instruction":"XIC","args":["AFE_In[30].0"],"span":[...]}`).

### 1b. Structured Text — `STContent` → `Line` (numbered CDATA)

```xml
<STContent>
<Line Number="0"><![CDATA[absAngle := relAngle_dg;]]></Line>
<Line Number="1"><![CDATA[IF absAngle < 0.0 THEN]]></Line>
</STContent>
```

- ST is real text. Lines are display rows, **not** statements — a single statement (e.g. an
  `IF…END_IF`) spans many `Line` elements, and a `Line` can hold several `;`-terminated
  statements. Reassemble by concatenating `Line` text in `Number` order before parsing.
- Assignment operator is `:=`. Comments: `//`, `(* *)`, `/* */`.
- IR: `routines.jsonl` → `st_lines[]`.

### 1c. Function Block — `FBDContent` → `Sheet` → `IRef`/`ORef`/`Block`/`Wire`

```xml
<FBDContent SheetSize="B - 11 x 17 in" SheetOrientation="Landscape">
<Sheet Number="1">
<IRef ID="0" X="480" Y="180" Operand="Active"/>          <!-- input reference -->
<IRef ID="2" X="480" Y="220" Operand="Raw"/>
<ORef ID="4" X="960" Y="220" Operand="Out"/>             <!-- output reference -->
<Block Type="MAVE" ID="5" Operand="MAVE_01"
       VisiblePins="EnableIn In Initialize NumberOfSamples Out">
  <Array Name="StorageArray" Operand="Storage_Array"/>
</Block>
<Wire FromID="2" ToID="5" ToParam="In"/>                 <!-- Raw  -> MAVE.In   -->
<Wire FromID="5" FromParam="Out" ToID="4"/>              <!-- MAVE.Out -> Out    -->
</Sheet>
</FBDContent>
```

- Node kinds (IR `fbd_nodes.jsonl`): `IRef` (read a tag), `ORef` (write a tag),
  `Block` (built-in FB e.g. MAVE/PID/TON), `AddOnInstruction` (an AOI used as a block),
  `TextBox` (annotation, ignore for logic).
- `Wire` is the dataflow edge: `FromID[/FromParam] → ToID[/ToParam]`. A bare `IRef→Block`
  wire feeds the block pin named in `ToParam`. **Wires, not XML order, define execution.**
- IR: `fbd_nodes.jsonl` + `fbd_wires.jsonl` (FromID/ToID/ToParam/FromParam).

### 1d. Sequential Function Chart — `SFCContent` → `Step`/`Transition`/`Action`

```xml
<SFCContent SheetSize="Letter - 8.5 x 11 in" SheetOrientation="Landscape">
<Step ID="1" Operand="Step_036" InitialStep="true">
  <Action ID="2" Operand="Action_028" Qualifier="NonStored" IsBoolean="false">
    <Body><STContent><Line Number="0"><![CDATA[Tran_038 := 0;]]></Line></STContent></Body>
  </Action>
</Step>
<Transition ID="120" Operand="JCAL_Tran_015">
  <Condition><STContent>
    <Line Number="0"><![CDATA[Cal_Stick.Active_Cal > 0 and Cal_Stick.Cal_Success]]></Line>
  </STContent></Condition>
</Transition>
<DirectedLink FromID="0" ToID="165" Show="true"/>        <!-- step/transition flow -->
</SFCContent>
```

- Node kinds (IR `sfc_nodes.jsonl`): `Step`, `Transition`, `Action`, `Branch`
  (divergence/convergence). `Step@InitialStep="true"` is the entry.
- An **Action** body and a **Transition** condition are themselves **Structured Text**
  (nested `STContent`). Parse those bodies with the ST rules above.
- `Action@Qualifier` (N/NonStored, S/Stored, P/Pulse, L/time-limited, etc.) controls *when*
  the action's ST runs while the step is active.
- Flow follows `DirectedLink` (IR `sfc_links.jsonl` FromID→ToID), **not** XML order.

### 1e. AOI logic (FBD-derived)

An AOI is `AddOnInstructionDefinition` whose body is one `Routine` (commonly named `Logic`)
in any language. Many here are FBD or ST. When an AOI is *called*, it appears either as a
ladder instruction `AOIName(backing_tag, arg1, arg2, …)` (§3) or as a `Block`/
`AddOnInstruction` node in FBD. The first argument is always the AOI **backing tag**
(an instance of the AOI's data type); remaining positional args map to the AOI's
`Required="true"` parameters in declaration order.

---

## 2. Neutral-text rung syntax (Ladder)

Instructions are written `MNEMONIC(operand,operand,…)` with **no separators** between
adjacent instructions on a wire. The rung ends with `;`.

| Construct | Neutral text | Meaning |
|---|---|---|
| Series (AND) | `XIC(a)XIC(b)OTE(c)` | a AND b → energize c |
| Branch (OR) | `[XIC(a),XIC(b)]OTE(c)` | `[ … , … ]` = parallel legs; comma separates legs |
| Nested branch | `[XIC(a)XIO(f),XIC(b)]OTE(c)` | leg 1 = a AND NOT f |
| Multiple outputs | `XIC(a)[OTE(b),OTE(c)]` | branch on the output side |

Real branched rung from this project:
```
[XIC(In1_Enable) XIO(In1_Fault) OTE(press1_Select) ,
 XIC(In2_Enable) XIO(In2_Fault) OTE(press2_select) , … ]
```
Operands may be tags, bit addresses (`AFE_In[30].0`), immediates (`8000`, `100.0`), or
expressions (in `CPT`/`LIM`). The IR splits this for you in `routine_units.jsonl` and the
per-instruction `instructions[]` arrays, but always cross-check against the raw `text`.

---

## 3. Common instructions — meaning and DESTRUCTIVE vs REFERENCE

**This is the single most important table.** A *destructive* instruction **writes** (modifies)
an operand or is a rung output / coil; a *reference* instruction only **reads** a condition
and never changes a tag. When tracing "what sets tag X," only destructive writes to X matter.

| Mnemonic | Meaning | Class | Notes |
|---|---|---|---|
| **XIC** | Examine If Closed (contact, true when bit = 1) | REFERENCE | reads a bit |
| **XIO** | Examine If Open (contact, true when bit = 0) | REFERENCE | reads a bit |
| **OTE** | Output Energize (coil = rung state each scan) | **DESTRUCTIVE** | writes the bit every scan |
| **OTL** | Output Latch (set bit to 1, retains) | **DESTRUCTIVE** | sets only |
| **OTU** | Output Unlatch (clear bit to 0, retains) | **DESTRUCTIVE** | clears only |
| **ONS/OSR/OSF** | One-shot (rising/falling edge) | **DESTRUCTIVE** | writes storage bit / output bit |
| **TON** | Timer On-Delay | **DESTRUCTIVE** | writes the TIMER tag (.ACC/.DN/.TT/.EN) |
| **TOF** | Timer Off-Delay | **DESTRUCTIVE** | writes the TIMER tag |
| **RTO** | Retentive Timer On | **DESTRUCTIVE** | writes the TIMER tag |
| **CTU / CTD** | Count Up / Down | **DESTRUCTIVE** | writes the COUNTER tag (.ACC/.DN) |
| **RES** | Reset (timer/counter/control) | **DESTRUCTIVE** | clears the referenced structure |
| **MOV** | Move source → dest | **DESTRUCTIVE** | writes **dest** (last operand); source is read |
| **CLR** | Clear dest to 0 | **DESTRUCTIVE** | writes dest |
| **COP / CPS** | Copy / Synchronous copy | **DESTRUCTIVE** | writes dest array |
| **BTD** | Bit Field Distribute (move bit range) | **DESTRUCTIVE** | writes dest (3rd operand) |
| **ADD/SUB/MUL/DIV/MOD** | Arithmetic, result → dest | **DESTRUCTIVE** | writes dest (last operand) |
| **CPT** | Compute (expression → dest) | **DESTRUCTIVE** | writes dest (1st operand) |
| **LIM** | Limit test (low ≤ test ≤ high) | REFERENCE | tests only, no write |
| **GRT/GEQ/LES/LEQ/EQU/NEQ** | Comparisons | REFERENCE | read-only conditions |
| **MEQ** | Masked compare equal | REFERENCE | read-only |
| **JSR** | Jump To Subroutine (run another routine) | control-flow | `JSR(Routine,InCount,…)`; may pass/return params (those are writes) |
| **RET / SBR** | Return / Subroutine entry | control-flow | parameter passing can write |
| **AOI call** | `AOIName(backing, args…)` | **DESTRUCTIVE** (mixed) | writes backing tag + all `Output`/`InOut` args; reads `Input` args |
| **MSG** | Messaging (read/write to remote/module) | **DESTRUCTIVE** | writes MESSAGE tag + (on read) the dest tag |
| **GSV** | Get System Value | **DESTRUCTIVE** | writes the **last** operand (dest) with system data |
| **SSV** | Set System Value | **DESTRUCTIVE** | writes a controller system object |

Rules of thumb (per 1756-RM003 operand tables):
- For data/math instructions, the **destination is the operand that gets written** — usually
  the **last** operand (MOV/ADD/BTD/GSV) but the **first** for `CPT`/`CLR`. Check the manual's
  operand list per instruction; don't assume position.
- Output/coil instructions (`OTE/OTL/OTU`, timers, counters) write their named bit/structure.
- Contacts and compares (`XIC/XIO/GRT/EQU/LIM/MEQ`) never write.

Real examples from this project:
```
GSV(Module,MV_IO_DRIVE_1,EntryStatus,MV1_STATUS_0)      ; writes MV1_STATUS_0
BTD(MV1_STATUS_0,12,MV1_STATUS_0_CONN,0,4)              ; writes MV1_STATUS_0_CONN
CombineBytes_8(Combine1,AFE_In[18],AFE_In[19],Temp1)    ; AOI; writes Combine1 + Temp1(Output)
JSR(R02_Engine_Fan,0)                                   ; runs routine R02_Engine_Fan
[MSG(Gen1_Data_Transfer_Read), MSG(Gen2_Data_Transfer_Read)]  ; each writes its MESSAGE tag
```

---

## 4. AOIs and UDTs

### 4a. AOI parameters (interface)

`AddOnInstructionDefinition/Parameters/Parameter` (IR `aoi_parameters.jsonl`):

| Attribute | Values | What it tells the analyst |
|---|---|---|
| `Usage` | `Input` / `Output` / `InOut` | **Input** = read by AOI (caller→AOI, by value). **Output** = written by AOI (AOI→caller). **InOut** = pass-by-reference pointer (read+write, always Required). |
| `Required` | `true`/`false` | Required params are positional call args (in order). Non-required are optional. |
| `Visible` | `true`/`false` | Whether it shows on the instruction face. Hidden ≠ unused. |
| `DataType` | BOOL/INT/REAL/UDT… | type of the param |
| `ExternalAccess` | Read/Write, Read Only, None | HMI/external visibility |

Every AOI has system params `EnableIn` (Input) / `EnableOut` (Output) auto-generated and
`Required="false" Visible="false"`. The call's **first** positional arg is the backing tag
(type = the AOI name), not a parameter. After it, args bind to `Required="true"` params in
declaration order. For destructive analysis: an AOI **writes** the backing tag and every
`Output` and `InOut` argument.

### 4b. UDTs

`DataTypes/DataType Class="User"` (IR `data_types.jsonl`, `kind:"udt"`). Members carry
`Name/DataType/Dimension/Radix`. Key gotcha: **bit members are packed**. A member with
`DataType="BIT"` has a `Target` (the hidden SINT/INT host member, named `ZZZZZZZZ…`) and a
`BitNumber`. Example: UDT `ADWATEC_COOLER_DP` member `StartCoolingSystem` = `BIT`,
`Target="ZZZZZZZZZZADWATEC_CO14"`, `BitNumber="0"` — i.e. it is bit 0 of that hidden SINT.
`Hidden="true"` members are the host words; treat the named BIT members as the real fields.

---

## 5. Gotchas for an LLM

1. **FBD and SFC are NOT linear text.** Document/IR order of `Block`/`Step` elements is
   layout (X/Y), not execution. Follow `Wire` (FBD) and `DirectedLink`/`sfc_links` (SFC).
   Reading nodes top-to-bottom will produce wrong logic. Ladder *is* ordered (rung 0,1,2…);
   ST *is* ordered by `Line Number`.
2. **Reassemble ST before parsing.** One `Line` ≠ one statement. Concatenate by `Number`;
   a statement (IF/CASE/FOR) spans many lines and a line may hold multiple `;` statements.
   SFC Action bodies and Transition conditions are nested ST — same rule.
3. **Aliases.** `Tag@TagType="Alias"` with `AliasFor="..."` is just another name for a real
   tag/bit (e.g. `B52_2_VOLT` → `MV_SWITCHGEAR_PROSOFT_1:I1.Data[151]`). Resolve aliases to
   the base address before cross-referencing reads/writes.
4. **Bit-level & member addressing.** Operands like `AFE_In[30].0`, `AFE.IN.Status.Ready_On`,
   `Draft[1].Filter_Reset` address a bit/member inside a larger tag. A write to `tag.bit`
   does **not** rewrite the whole word; a write to the word affects all its bits.
5. **Tag scope (3 levels).** Resolve a name in this order: **AOI LocalTag/Parameter** (inside
   an AOI body) → **Program tag** (`Programs/Program/Tags`, `scope_type:"Program"`) →
   **Controller tag** (`scope_type:"Controller"`). The same name can exist at multiple scopes;
   program scope shadows controller scope inside that program. I/O tags
   (`Module:I.Data`, `Local:1:I`) are always controller scope.
6. **Module/I/O tags** use `:` (`MV_IO_DRIVE_1:I1.Data[151]`) and come from module defs, not
   the Tags section directly. Their meaning depends on the module profile.
7. **Routine reachability.** A routine only runs if reached by a `JSR` chain from a program's
   `MainRoutineName`, and that program is in a `Task`. Unreferenced routines may be dead code.
8. **`Type`/`Usage` strings are case- and space-sensitive** in L5X (`"Read/Write"`,
   `"Read Only"`, `"NonStored"`). Match exactly.

---

## Source publications

- **1756-RM003** — Logix 5000 Controllers General Instructions (per-instruction operands,
  destination/source, the destructive/reference behavior in §3).
- **1756-PM007** — Logix 5000 Controllers Structured Text.
- **1756-RM084 / 1756-RM014** — Logix 5000 Import/Export Reference (the L5X XML schema:
  RLLContent/Rung, STContent/Line, FBDContent/Sheet/IRef/ORef/Block/Wire,
  SFCContent/Step/Transition/Action/DirectedLink).
- **1756-PM001** — Logix 5000 Common Procedures (tag scope, AOIs, UDTs, aliases).
- **1756-RM087** — Function Block / SFC element attributes (qualifiers, pins).
- **IEC 61131-3** — base standard for LD/ST/FBD/SFC semantics.
