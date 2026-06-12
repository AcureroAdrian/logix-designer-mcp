"""Heuristic cross-reference extraction for Logix routine text."""

from __future__ import annotations

import re
from typing import Iterable, Sequence


IDENT_PATTERN = (
    r"[A-Za-z_][A-Za-z0-9_$]*(?::[A-Za-z0-9_$]+)?"
    r"(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_$]*(?:\[[^\]]+\])?)*"
)
IDENT_RE = re.compile(IDENT_PATTERN)
CALL_RE = re.compile(r"\b([A-Z_][A-Z0-9_]*)\s*\(", re.IGNORECASE)

READ = "read"
WRITE = "write"
READ_WRITE = "read_write"
CALL = "call"
# Rung label operands (JMP/LBL) are jump targets, not tags; they are dropped
# from the cross-reference output entirely.
LABEL = "label"

# Map an AOI parameter Usage to the access mode of the argument supplied to it.
USAGE_ACCESS = {"Input": READ, "Output": WRITE, "InOut": READ_WRITE}


def _sig(*roles: str, rest: str | None = None) -> tuple[tuple[str, ...], str | None]:
    """An instruction signature: per-operand roles plus a role for extras."""

    return (roles, rest)


# Per-operand access roles for the common ControlLogix instruction set. Each
# operand of a Ladder instruction is classified by its position; ``rest`` covers
# any trailing operands (e.g. PID configuration tags). Anything not listed here
# falls back to a heuristic so confidence can be reported honestly.
INSTRUCTION_SIGNATURES: dict[str, tuple[tuple[str, ...], str | None]] = {
    # --- Bit / relay ---
    "XIC": _sig(READ),
    "XIO": _sig(READ),
    "OTE": _sig(WRITE),
    "OTL": _sig(WRITE),
    "OTU": _sig(WRITE),
    "ONS": _sig(READ_WRITE),
    "OSR": _sig(READ_WRITE, WRITE),
    "OSF": _sig(READ_WRITE, WRITE),
    # --- Timers / counters ---
    "TON": _sig(READ_WRITE, READ, READ),
    "TOF": _sig(READ_WRITE, READ, READ),
    "RTO": _sig(READ_WRITE, READ, READ),
    "TONR": _sig(READ_WRITE, READ, READ),
    "TOFR": _sig(READ_WRITE, READ, READ),
    "RTOR": _sig(READ_WRITE, READ, READ),
    "CTU": _sig(READ_WRITE, READ, READ),
    "CTD": _sig(READ_WRITE, READ, READ),
    "CTUD": _sig(READ_WRITE, READ, READ),
    "RES": _sig(WRITE),
    # --- Compare ---
    "CMP": _sig(READ),
    "EQU": _sig(READ, READ),
    "NEQ": _sig(READ, READ),
    "LES": _sig(READ, READ),
    "LEQ": _sig(READ, READ),
    "GRT": _sig(READ, READ),
    "GEQ": _sig(READ, READ),
    "LIM": _sig(READ, READ, READ),
    "MEQ": _sig(READ, READ, READ),
    # --- Math / compute (destination varies by instruction) ---
    "ADD": _sig(READ, READ, WRITE),
    "SUB": _sig(READ, READ, WRITE),
    "MUL": _sig(READ, READ, WRITE),
    "DIV": _sig(READ, READ, WRITE),
    "MOD": _sig(READ, READ, WRITE),
    "XPY": _sig(READ, READ, WRITE),
    "SQR": _sig(READ, WRITE),
    "SQRT": _sig(READ, WRITE),
    "NEG": _sig(READ, WRITE),
    "ABS": _sig(READ, WRITE),
    "LN": _sig(READ, WRITE),
    "LOG": _sig(READ, WRITE),
    "SIN": _sig(READ, WRITE),
    "COS": _sig(READ, WRITE),
    "TAN": _sig(READ, WRITE),
    "ASN": _sig(READ, WRITE),
    "ACS": _sig(READ, WRITE),
    "ATN": _sig(READ, WRITE),
    "DEG": _sig(READ, WRITE),
    "RAD": _sig(READ, WRITE),
    "TRN": _sig(READ, WRITE),
    "CPT": _sig(WRITE, READ),  # CPT(Dest, Expression): destination is first
    # --- Logical / bitwise ---
    "AND": _sig(READ, READ, WRITE),
    "OR": _sig(READ, READ, WRITE),
    "XOR": _sig(READ, READ, WRITE),
    "NOT": _sig(READ, WRITE),
    "CLR": _sig(WRITE),
    "SWPB": _sig(READ, READ, WRITE),
    # --- Move / copy ---
    "MOV": _sig(READ, WRITE),
    "MVM": _sig(READ, READ, WRITE),  # MVM(Source, Mask, Dest)
    "BTD": _sig(READ, READ, WRITE, READ, READ),  # BTD(Src, SrcBit, Dest, DestBit, Len)
    "COP": _sig(READ, WRITE, READ),
    "CPS": _sig(READ, WRITE, READ),
    "FLL": _sig(READ, WRITE, READ),
    "TOD": _sig(READ, WRITE),
    "FRD": _sig(READ, WRITE),
    "DTR": _sig(READ, READ, READ_WRITE),
    # --- File / array ---
    "FAL": _sig(READ_WRITE, READ, READ_WRITE, READ, WRITE, READ),
    "FSC": _sig(READ_WRITE, READ, READ_WRITE, READ, READ),
    "FFL": _sig(READ, WRITE, READ_WRITE, READ, READ_WRITE),
    "FFU": _sig(READ, WRITE, READ_WRITE, READ, READ_WRITE),
    "LFL": _sig(READ, WRITE, READ_WRITE, READ, READ_WRITE),
    "LFU": _sig(READ, WRITE, READ_WRITE, READ, READ_WRITE),
    "BSL": _sig(READ_WRITE, READ_WRITE, READ, READ),
    "BSR": _sig(READ_WRITE, READ_WRITE, READ, READ),
    "AVE": _sig(READ, WRITE, READ_WRITE, READ, READ_WRITE),
    "SRT": _sig(READ_WRITE, READ_WRITE, READ, READ_WRITE),
    "STD": _sig(READ, WRITE, WRITE, READ_WRITE, READ, READ_WRITE),
    # --- System / messaging ---
    "PID": _sig(READ_WRITE, READ, READ, READ_WRITE, rest=READ),
    "PIDE": _sig(READ_WRITE, rest=READ),
    "MSG": _sig(READ_WRITE),
    "GSV": _sig(READ, READ, READ, WRITE),
    "SSV": _sig(READ, READ, READ, READ),
    "IOT": _sig(READ_WRITE),
    "EOT": _sig(READ),
    # --- Alarms (1756-RM003: the instruction drives the alarm tag's status) ---
    "ALMD": _sig(READ_WRITE, READ, READ, READ, READ),  # ALMD(Tag, ProgAck, ProgReset, ProgDisable, ProgEnable)
    "ALMA": _sig(READ_WRITE, READ, READ, READ, READ, READ),  # ALMA(Tag, In, ProgAck, ProgReset, ProgDisable, ProgEnable)
    # --- Array / misc ---
    "SIZE": _sig(READ, READ, WRITE),  # SIZE(Source, Dim, Size)
    # --- Program control ---
    "JSR": _sig(CALL, rest=READ_WRITE),
    "FOR": _sig(CALL, READ_WRITE, READ, READ, READ),
    "SBR": _sig(rest=WRITE),
    "RET": _sig(rest=READ),
    "JMP": _sig(LABEL),
    "LBL": _sig(LABEL),
    "SFR": _sig(CALL, READ),  # SFR(SFCRoutine, Step)
    "SFP": _sig(CALL, READ),  # SFP(SFCRoutine, TargetState)
}

ST_KEYWORDS = {
    "AND",
    "BY",
    "CASE",
    "DO",
    "ELSE",
    "ELSIF",
    "END_CASE",
    "END_FOR",
    "END_IF",
    "END_REPEAT",
    "END_WHILE",
    "EXIT",
    "FALSE",
    "FOR",
    "IF",
    "LIMIT",
    "MAX",
    "MIN",
    "MOD",
    "NOT",
    "OF",
    "OR",
    "REPEAT",
    "RETURN",
    "THEN",
    "TO",
    "TRUE",
    "UNTIL",
    "WHILE",
    "XOR",
}


def aoi_signature_from_parameters(parameters: Iterable[dict]) -> list[str]:
    """Operand access roles for an AOI call, from its parameter definitions.

    The Ladder/ST call form is ``AOI(BackingTag, arg1, arg2, ...)`` where the
    arguments map positionally to the AOI parameters in declaration order,
    excluding the implicit ``EnableIn``/``EnableOut`` bits. Input parameters read
    their argument, Output parameters write it, and InOut parameters do both.
    """

    roles: list[str] = []
    for param in parameters:
        name = str(param.get("name") or "")
        if name in {"EnableIn", "EnableOut"}:
            continue
        roles.append(USAGE_ACCESS.get(str(param.get("usage") or ""), READ_WRITE))
    return roles


def extract_references(
    text: str,
    language: str,
    routine_id: str,
    aoi_signatures: dict[str, list[str]] | None = None,
) -> list[dict[str, object]]:
    """Return symbol references from routine text."""

    language = (language or "").upper()
    if language == "RLL":
        return extract_rll_references(text, routine_id, aoi_signatures)
    if language == "ST":
        return extract_st_references(text, routine_id)
    return []


def extract_rll_references(
    text: str,
    routine_id: str,
    aoi_signatures: dict[str, list[str]] | None = None,
) -> list[dict[str, object]]:
    """Return read/write/call references from Ladder neutral text."""

    refs: list[dict[str, object]] = []
    for instruction in parse_ladder_instructions(text):
        op = str(instruction["instruction"])
        args = [str(arg) for arg in instruction["args"]]

        # Classify by ARGUMENT position so the signature stays aligned even when
        # operands are literals (e.g. BTD bit counts) or whole expressions (e.g.
        # a CPT expression). Each argument's role is then applied to every tag it
        # contains.
        for index, classified in enumerate(classify_ladder_instruction(op, args, aoi_signatures)):
            access = str(classified["access"])
            if access == LABEL:
                continue
            confidence = str(classified.get("confidence", "heuristic"))
            for symbol in extract_tag_references(str(classified["operand"]), include_calls=False):
                refs.append(_ref(symbol, routine_id, access, op, confidence=confidence))
                if index == 0 and op in ("ALMD", "ALMA"):
                    # The alarm instruction sets the tag's status members; expose
                    # .InAlarm as a write so traces and producer queries connect
                    # alarm logic to the members the rest of the program reads.
                    refs.append(_ref(f"{symbol}.InAlarm", routine_id, WRITE, op, confidence="typed"))
    return _dedupe_refs(refs)


def extract_st_references(text: str, routine_id: str) -> list[dict[str, object]]:
    """Return read/write references from Structured Text assignments."""

    refs: list[dict[str, object]] = []
    for assignment in detect_st_assignments(text):
        for target in assignment["targets"]:
            refs.append(_ref(str(target), routine_id, "write", "ST_ASSIGN"))
        for read in assignment["reads"]:
            refs.append(_ref(str(read), routine_id, "read", "ST"))
    return _dedupe_refs(refs)


def parse_ladder_instructions(neutral_text: str) -> list[dict[str, object]]:
    """Parse Ladder neutral text into JSON-serializable instruction records."""

    records: list[dict[str, object]] = []
    pos = 0
    while True:
        match = CALL_RE.search(neutral_text, pos)
        if not match:
            return records

        open_index = match.end() - 1
        close_index = _find_matching_paren(neutral_text, open_index)
        if close_index == -1:
            pos = match.end()
            continue

        records.append(
            {
                "instruction": match.group(1).upper(),
                "args": [_clean_operand(arg) for arg in _split_args(neutral_text[open_index + 1 : close_index])],
                "span": [match.start(), close_index + 1],
            }
        )
        pos = close_index + 1


def extract_operands_from_neutral_text(neutral_text: str) -> list[str]:
    """Extract distinct tag-like operands from Ladder neutral text."""

    operands: list[str] = []
    seen: set[str] = set()
    for instruction in parse_ladder_instructions(neutral_text):
        for arg in instruction["args"]:
            for symbol in extract_tag_references(str(arg), include_calls=False):
                if symbol not in seen:
                    seen.add(symbol)
                    operands.append(symbol)
    return operands


def classify_ladder_instruction(
    instruction: str,
    operands: Sequence[str],
    aoi_signatures: dict[str, list[str]] | None = None,
) -> list[dict[str, str]]:
    """Classify Logix Ladder instruction operands by access mode, per position.

    ``operands`` are the instruction arguments in source order (one entry per
    argument, including literals and expressions). Resolution order: the built-in
    instruction signature table, then an Add-On Instruction call (operand 0 is
    the backing tag, the rest map to the AOI parameters), then a heuristic
    fallback that marks the operand ``unknown``. Each classification carries a
    ``confidence``: ``typed`` for table/AOI hits, ``heuristic`` otherwise.
    """

    op = instruction.upper()
    roles, rest = INSTRUCTION_SIGNATURES.get(op, ((), None))
    aoi_roles = (aoi_signatures or {}).get(op)

    classified: list[dict[str, str]] = []
    for index, operand in enumerate(operands):
        if roles or rest is not None:
            access = roles[index] if index < len(roles) else (rest or "unknown")
            confidence = "typed" if access != "unknown" else "heuristic"
        elif aoi_roles is not None:
            if index == 0:
                access = READ_WRITE  # AOI backing/instance tag
            elif index - 1 < len(aoi_roles):
                access = aoi_roles[index - 1]
            else:
                access = READ_WRITE
            confidence = "typed"
        else:
            access = "unknown"
            confidence = "heuristic"
        classified.append({"operand": operand, "access": access, "confidence": confidence})
    return classified


def xrefs_from_ladder_neutral_text(
    neutral_text: str,
    *,
    routine: str | None = None,
    source: str | None = None,
    location: str | None = None,
    aoi_signatures: dict[str, list[str]] | None = None,
) -> list[dict[str, object]]:
    """Build JSON-serializable cross-reference records from Ladder neutral text."""

    routine_id = routine or location or source or ""
    records: list[dict[str, object]] = []
    for ref in extract_rll_references(neutral_text, routine_id, aoi_signatures):
        record = dict(ref)
        record["language"] = "ladder"
        record["source"] = source
        record["location"] = location
        record["operand"] = record["symbol"]
        record["base_symbol"] = _base_symbol(str(record["symbol"]))
        records.append(record)
    return records


def detect_st_assignments(structured_text: str) -> list[dict[str, object]]:
    """Detect Structured Text assignment statements and RHS read operands."""

    clean = _strip_st_comments(structured_text)
    assignment_re = re.compile(
        rf"(?<![:<>=])\b(?P<target>{IDENT_PATTERN})\s*:=\s*(?P<expression>.*?);",
        re.DOTALL,
    )

    assignments: list[dict[str, object]] = []
    for match in assignment_re.finditer(clean):
        expression = _strip_strings(match.group("expression")).strip()
        target = _clean_operand(match.group("target"))
        assignments.append(
            {
                "target": target,
                "targets": [target],
                "expression": expression,
                "reads": extract_tag_references(expression, include_calls=False),
                "span": [match.start(), match.end()],
            }
        )
    return assignments


def xrefs_from_structured_text(
    structured_text: str,
    *,
    routine: str | None = None,
    source: str | None = None,
    location: str | None = None,
) -> list[dict[str, object]]:
    """Build JSON-serializable read/write records from ST assignments."""

    routine_id = routine or location or source or ""
    records: list[dict[str, object]] = []
    for ref in extract_st_references(structured_text, routine_id):
        record = dict(ref)
        record["language"] = "st"
        record["source"] = source
        record["location"] = location
        record["operand"] = record["symbol"]
        record["base_symbol"] = _base_symbol(str(record["symbol"]))
        records.append(record)
    return records


def extract_tag_references(text: str, *, include_calls: bool = True) -> list[str]:
    """Extract distinct tag-like symbols from an operand or ST expression."""

    clean = _strip_strings(text)
    call_names = set()
    if not include_calls:
        call_names = {match.group(1).upper() for match in CALL_RE.finditer(clean)}

    symbols: list[str] = []
    seen: set[str] = set()
    for match in IDENT_RE.finditer(clean):
        symbol = _clean_operand(match.group(0))
        upper = symbol.upper()
        if upper in call_names or upper in ST_KEYWORDS or not _looks_like_symbol(symbol):
            continue
        if symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def _iter_calls(text: str) -> Iterable[tuple[str, str]]:
    for instruction in parse_ladder_instructions(text):
        yield str(instruction["instruction"]), ",".join(str(arg) for arg in instruction["args"])


def _find_matching_paren(text: str, open_index: int) -> int:
    depth = 0
    quote: str | None = None
    i = open_index
    while i < len(text):
        char = text[i]
        if quote:
            if char == quote:
                quote = None
            elif char == "\\":
                i += 1
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_args(args_text: str) -> list[str]:
    args: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for i, char in enumerate(args_text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            args.append(args_text[start:i].strip())
            start = i + 1
    tail = args_text[start:].strip()
    if tail or args_text.strip():
        args.append(tail)
    return args


def _clean_operand(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^\s*#?", "", value)
    return value.strip("\"'")


def _looks_like_symbol(value: str) -> bool:
    if not value:
        return False
    if value.upper() in ST_KEYWORDS:
        return False
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
        return False
    if re.fullmatch(r"(?:2|8|10|16)#[-+A-Fa-f0-9_.]+", value):
        return False
    if value.startswith("$"):
        return False
    return bool(IDENT_RE.fullmatch(value))


def _strip_st_comments(text: str) -> str:
    text = re.sub(r"\(\*.*?\*\)", " ", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//.*", " ", text)


def _strip_strings(text: str) -> str:
    result: list[str] = []
    quote: str | None = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            result.append(" ")
        elif char in {"'", '"'}:
            quote = char
            result.append(" ")
        else:
            result.append(char)
    return "".join(result)


def _base_symbol(symbol: str) -> str:
    match = re.match(r"[A-Za-z_][A-Za-z0-9_$]*(?::[A-Za-z0-9_$]+)?", symbol)
    return match.group(0) if match else symbol


def _ref(symbol: str, routine_id: str, access: str, instruction: str, confidence: str = "heuristic") -> dict[str, object]:
    return {
        "symbol": symbol,
        "routine": routine_id,
        "access": access,
        "instruction": instruction,
        "confidence": confidence,
    }


def _dedupe_refs(refs: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[object, object, object, object]] = set()
    result: list[dict[str, object]] = []
    for ref in refs:
        key = (ref["symbol"], ref["routine"], ref["access"], ref["instruction"])
        if key in seen:
            continue
        seen.add(key)
        result.append(ref)
    return result
