"""Optional pycomm3 runtime reads for Logix MCP.

This module is intentionally separate from the SDK adapter. Importing it does
not import ``pycomm3``; the optional dependency is loaded only by
``Pycomm3TagReader`` when an online read is requested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol
import math
import os
import subprocess
import sys
import time
import uuid

from . import runtime_store


JsonDict = dict[str, Any]

DEFAULT_INTERVAL_MS = 100
MAX_RUNTIME_TAGS = 32
MAX_DURATION_SECONDS = 300
MAX_BYTES_PER_CYCLE = 4096
DEFAULT_DURATION_SECONDS = 60


class RuntimeReaderError(RuntimeError):
    """Runtime reader validation or communication failed."""


@dataclass(frozen=True)
class RuntimeTagValue:
    tag: str
    value: Any
    type: str | None = None
    error: str | None = None

    def to_record(self) -> JsonDict:
        return {
            "tag": self.tag,
            "value": runtime_store.compact_json_value(self.value),
            "type": self.type,
            "error": self.error,
        }


@dataclass(frozen=True)
class RuntimeReadNowRequest:
    path: str
    tags: tuple[str, ...]
    source: str = "pycomm3"
    mode: str = "ONLINE"


@dataclass(frozen=True)
class RuntimeCaptureRequest:
    workspace: str | Path
    path: str
    tags: tuple[str, ...]
    interval_ms: int = DEFAULT_INTERVAL_MS
    duration_seconds: float | None = DEFAULT_DURATION_SECONDS
    session_id: str | None = None
    stop_file: str | Path | None = None
    source: str = "pycomm3"
    mode: str = "ONLINE"
    permission: dict[str, Any] = field(default_factory=dict)
    retention_policy: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeSessionManifest:
    session_id: str
    source: str
    mode: str
    interval_ms: int
    requested_tags: tuple[str, ...]


class TagReader(Protocol):
    source: str

    def open(self) -> None:
        ...

    def close(self) -> None:
        ...

    def read_tags(self, tags: Iterable[str]) -> list[RuntimeTagValue]:
        ...

    def controller_identity(self) -> dict[str, Any]:
        ...


class Pycomm3TagReader:
    """Read tags from a Logix controller through pycomm3."""

    source = "pycomm3"

    def __init__(self, path: str, *, init_tags: bool = False, init_program_tags: bool = False):
        self.path = str(path)
        self.init_tags = init_tags
        self.init_program_tags = init_program_tags
        self._driver: Any = None

    def open(self) -> None:
        try:
            from pycomm3 import LogixDriver
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeReaderError("pycomm3 is not installed. Install with: pip install -e .[runtime]") from exc
        self._driver = LogixDriver(
            self.path,
            init_tags=self.init_tags,
            init_program_tags=self.init_program_tags,
        )
        opened = self._driver.open()
        if opened is False:
            raise RuntimeReaderError("pycomm3 could not open the controller connection")

    def close(self) -> None:
        driver = self._driver
        self._driver = None
        if driver is not None:
            driver.close()

    def read_tags(self, tags: Iterable[str]) -> list[RuntimeTagValue]:
        if self._driver is None:
            raise RuntimeReaderError("pycomm3 reader is not open")
        tag_list = _normalize_tags(tags)
        response = self._driver.read(*tag_list)
        if not isinstance(response, list):
            response = [response]
        return [_tag_to_value(item) for item in response]

    def controller_identity(self) -> dict[str, Any]:
        driver = self._driver
        if driver is None:
            return {}
        try:
            info = driver.get_plc_info()
            if isinstance(info, dict):
                return runtime_store.compact_json_value(info)
        except Exception:
            pass
        info = getattr(driver, "info", None)
        if isinstance(info, dict):
            return runtime_store.compact_json_value(info)
        return {"path_fingerprint": runtime_store.fingerprint_text(self.path)}


class FakeTagReader:
    """Deterministic moving tag reader used for tests and offline harnesses."""

    source = "fake"

    def __init__(self, *, data_type: str = "DINT", period: int = 20, amplitude: float = 100.0, offset: float = 0.0):
        self.data_type = str(data_type or "DINT").upper()
        self.period = max(int(period or 1), 1)
        self.amplitude = float(amplitude)
        self.offset = float(offset)
        self._cycle = 0
        self.opened = False
        self.events: list[str] = []

    def open(self) -> None:
        self.opened = True
        self.events.append("open")

    def close(self) -> None:
        self.opened = False
        self.events.append("close")

    def read_tags(self, tags: Iterable[str]) -> list[RuntimeTagValue]:
        if not self.opened:
            raise RuntimeReaderError("fake reader is not open")
        self.events.append("read")
        tag_list = _normalize_tags(tags)
        results: list[RuntimeTagValue] = []
        for index, tag in enumerate(tag_list):
            raw = self.offset + self.amplitude * (((self._cycle + index) % self.period) / self.period)
            value: Any
            if self.data_type in {"BOOL"}:
                value = bool((self._cycle + index) % 2)
            elif self.data_type in {"REAL", "LREAL"}:
                value = round(raw, 6)
            else:
                value = int(round(raw))
            results.append(RuntimeTagValue(tag=tag, value=value, type=self.data_type, error=None))
        self._cycle += 1
        return results

    def controller_identity(self) -> dict[str, Any]:
        return {"name": "Fake Logix Runtime", "source": self.source, "simulated": True}


def read_tags_now(
    path: str | RuntimeReadNowRequest,
    tags: Iterable[str] | None = None,
    *,
    source: str = "pycomm3",
    reader: TagReader | None = None,
    observed_at: Any | None = None,
) -> JsonDict:
    if isinstance(path, RuntimeReadNowRequest):
        request = path
        path_text = request.path
        tag_list = _validate_tags(request.tags)
        source = request.source
    else:
        if tags is None:
            raise RuntimeReaderError("tags are required")
        path_text = str(path)
        tag_list = _validate_tags(tags)
    selected_reader = reader or _build_reader(source, path_text)
    observed_text = runtime_store.format_utc(observed_at)
    identity: dict[str, Any] = {}
    try:
        selected_reader.open()
        identity = selected_reader.controller_identity()
        try:
            values = _values_for_tags(tag_list, selected_reader.read_tags(tag_list))
        except Exception as exc:
            values = [RuntimeTagValue(tag=tag, value=None, type=None, error=str(exc)) for tag in tag_list]
    except Exception as exc:
        values = [RuntimeTagValue(tag=tag, value=None, type=None, error=str(exc)) for tag in tag_list]
    finally:
        try:
            selected_reader.close()
        except Exception:
            pass
    resolved_source = str(getattr(selected_reader, "source", source) or source).lower()
    results = [value.to_record() for value in values]
    # Bound the response so a tag the agent freely chose (e.g. a large array/UDT)
    # cannot blow the LLM context. NOTE: this caps the returned/stored payload,
    # not the bytes already pulled off the controller; preventing wire load would
    # require a get_tag_list size pre-check.
    _enforce_cycle_budget(results)
    return {
        "ok": True,
        "operation": "read_tags_now",
        "source": getattr(selected_reader, "source", source),
        "mode": "OFFLINE" if resolved_source in {"fake", "simulated"} else "ONLINE",
        "observed_at": observed_text,
        "comm_path_fingerprint": runtime_store.fingerprint_text(path_text),
        "controller_identity": runtime_store.compact_json_value(identity),
        "requested_tags": tag_list,
        "results": results,
    }


def run_capture(request: RuntimeCaptureRequest, *, reader: TagReader | None = None) -> JsonDict:
    tag_list = _validate_tags(request.tags)
    interval_ms = _validate_interval(request.interval_ms)
    duration_seconds = _validate_duration(request.duration_seconds)
    session_id = runtime_store.validate_session_id(request.session_id or _new_session_id("rt"))
    selected_reader = reader or _build_reader(request.source, request.path)
    limits = {
        "max_tags": MAX_RUNTIME_TAGS,
        "max_bytes_per_cycle": MAX_BYTES_PER_CYCLE,
        "max_duration_seconds": MAX_DURATION_SECONDS,
    }
    started = runtime_store.utc_now()
    selected_reader.open()
    final_status = "completed"
    error_message: str | None = None
    try:
        created = runtime_store.create_session(
            request.workspace,
            session_id=session_id,
            controller_identity=selected_reader.controller_identity(),
            comm_path_fingerprint=runtime_store.fingerprint_text(request.path),
            mode=request.mode,
            source=getattr(selected_reader, "source", request.source),
            interval_ms=interval_ms,
            requested_tags=tag_list,
            limits=limits,
            permission=request.permission,
            retention_policy=request.retention_policy,
            created_at=started,
            status="running",
            pid=os.getpid(),
        )
        monotonic_start = time.monotonic()
        next_cycle_at = monotonic_start
        cycle = 0
        while True:
            if _stop_file_requested(request.stop_file) or _store_stop_requested(request.workspace, session_id):
                final_status = "stopped"
                break
            if time.monotonic() - monotonic_start >= duration_seconds:
                final_status = "completed"
                break
            cycle_started = time.monotonic()
            samples = _read_cycle_samples(selected_reader, tag_list, cycle=cycle, ts_mono=cycle_started)
            runtime_store.append_samples(request.workspace, session_id, samples)
            cycle += 1
            next_cycle_at += interval_ms / 1000.0
            sleep_for = next_cycle_at - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
    except Exception as exc:
        final_status = "failed"
        error_message = str(exc)
        try:
            _update_store_state(request.workspace, session_id, status=final_status, error=error_message)
        except Exception:
            pass
        raise
    finally:
        selected_reader.close()
        try:
            updates: JsonDict = {"status": final_status, "ended_at": runtime_store.format_utc()}
            if error_message:
                updates["error"] = error_message
            _update_store_state(request.workspace, session_id, **updates)
        except Exception:
            pass
    manifest = created.get("manifest") if isinstance(created.get("manifest"), dict) else created
    return {
        "ok": True,
        "operation": "runtime_capture",
        "session_id": session_id,
        "status": final_status,
        "stop_reason": "stop_file" if final_status == "stopped" else "duration",
        "state": runtime_store.read_state(request.workspace, session_id),
        "manifest": manifest,
        "paths": created.get("paths") or _session_paths_record(request.workspace, session_id),
    }


def start_capture_subprocess(
    workspace: str | Path,
    *,
    path: str,
    tags: Iterable[str],
    interval_ms: int = DEFAULT_INTERVAL_MS,
    duration_seconds: float | None = DEFAULT_DURATION_SECONDS,
    source: str = "pycomm3",
    session_id: str | None = None,
) -> JsonDict:
    tag_list = _validate_tags(tags)
    safe_id = runtime_store.validate_session_id(session_id or _new_session_id("rt"))
    command = [
        sys.executable,
        "-m",
        "logix_mcp",
        "runtime-capture-run",
        str(workspace),
        "--path",
        str(path),
        "--interval-ms",
        str(_validate_interval(interval_ms)),
        "--duration-seconds",
        str(_validate_duration(duration_seconds)),
        "--source",
        str(source),
        "--session-id",
        safe_id,
    ]
    for tag in tag_list:
        command.extend(["--tag", tag])
    process = subprocess.Popen(
        command,
        cwd=str(Path.cwd()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return {
        "ok": True,
        "operation": "runtime_capture_start",
        "subprocess_mode": True,
        "session_id": safe_id,
        "pid": process.pid,
        "source": source,
        "requested_tags": tag_list,
        "interval_ms": _validate_interval(interval_ms),
        "duration_seconds": _validate_duration(duration_seconds),
    }


def stop_capture(workspace: str | Path, session_id: str) -> JsonDict:
    state = _request_store_stop(workspace, session_id)
    return {
        "ok": True,
        "operation": "runtime_capture_stop",
        "session_id": runtime_store.validate_session_id(session_id),
        "stop_requested": True,
        "state": state,
    }


def _read_cycle_samples(reader: TagReader, tags: list[str], *, cycle: int, ts_mono: float) -> list[JsonDict]:
    ts_utc = runtime_store.format_utc()
    try:
        values = reader.read_tags(tags)
    except Exception as exc:
        values = [RuntimeTagValue(tag=tag, value=None, type=None, error=str(exc)) for tag in tags]
    samples: list[JsonDict] = []
    for value in values:
        record = value.to_record()
        record.update({"ts_utc": ts_utc, "ts_mono": round(ts_mono, 6), "cycle": cycle})
        samples.append(record)
    _enforce_cycle_budget(samples)
    return samples


def _tag_to_value(tag_obj: Any) -> RuntimeTagValue:
    return RuntimeTagValue(
        tag=str(getattr(tag_obj, "tag", "")),
        value=getattr(tag_obj, "value", None),
        type=None if getattr(tag_obj, "type", None) is None else str(getattr(tag_obj, "type")),
        error=None if getattr(tag_obj, "error", None) is None else str(getattr(tag_obj, "error")),
    )


def _values_for_tags(tags: list[str], values: list[RuntimeTagValue]) -> list[RuntimeTagValue]:
    by_tag = {value.tag: value for value in values}
    ordered: list[RuntimeTagValue] = []
    for index, tag in enumerate(tags):
        if tag in by_tag:
            ordered.append(by_tag[tag])
        elif index < len(values):
            ordered.append(values[index])
        else:
            ordered.append(RuntimeTagValue(tag=tag, value=None, type=None, error="missing tag result"))
    return ordered


def _build_reader(source: str, path: str) -> TagReader:
    normalized = str(source or "pycomm3").lower()
    if normalized in {"fake", "simulated"}:
        return FakeTagReader()
    if normalized != "pycomm3":
        raise RuntimeReaderError(f"Unsupported runtime source: {source}")
    return Pycomm3TagReader(path)


def _validate_tags(tags: Iterable[str]) -> list[str]:
    tag_list = _normalize_tags(tags)
    if len(tag_list) > MAX_RUNTIME_TAGS:
        raise RuntimeReaderError(f"Runtime read requested {len(tag_list)} tags, limit is {MAX_RUNTIME_TAGS}")
    return tag_list


def _normalize_tags(tags: Iterable[str]) -> list[str]:
    tag_list: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        text = str(tag or "").strip()
        if not text or text in seen:
            continue
        tag_list.append(text)
        seen.add(text)
    if not tag_list:
        raise RuntimeReaderError("At least one runtime tag is required")
    return tag_list


def _validate_interval(interval_ms: int) -> int:
    value = int(interval_ms)
    if value < DEFAULT_INTERVAL_MS:
        raise RuntimeReaderError(f"interval_ms must be >= {DEFAULT_INTERVAL_MS}")
    return value


def _validate_duration(duration_seconds: float | None) -> float:
    value = DEFAULT_DURATION_SECONDS if duration_seconds is None else float(duration_seconds)
    if not math.isfinite(value) or value <= 0:
        raise RuntimeReaderError("duration_seconds must be > 0")
    if value > MAX_DURATION_SECONDS:
        raise RuntimeReaderError(f"duration_seconds must be <= {MAX_DURATION_SECONDS}")
    return value


def _enforce_cycle_budget(samples: list[JsonDict]) -> None:
    import json

    total = 0
    for sample in samples:
        size = len(json.dumps(sample, ensure_ascii=True, default=str))
        if total and total + size > MAX_BYTES_PER_CYCLE:
            sample["value"] = None
            sample["error"] = sample.get("error") or f"cycle payload exceeded {MAX_BYTES_PER_CYCLE} bytes"
            size = len(json.dumps(sample, ensure_ascii=True, default=str))
        total += size


def _stop_file_requested(stop_file: str | Path | None) -> bool:
    return bool(stop_file and Path(stop_file).exists())


def _new_session_id(prefix: str) -> str:
    safe_prefix = "".join(char if char.isalnum() else "-" for char in str(prefix or "rt")).strip("-") or "rt"
    return f"{safe_prefix}-{uuid.uuid4().hex[:16]}"


def _store_stop_requested(workspace: str | Path, session_id: str) -> bool:
    checker = getattr(runtime_store, "stop_requested", None)
    if checker is not None:
        return bool(checker(workspace, session_id))
    return Path(_session_paths_record(workspace, session_id)["stop"]).exists()


def _request_store_stop(workspace: str | Path, session_id: str) -> JsonDict:
    requester = getattr(runtime_store, "request_stop", None)
    if requester is not None:
        return requester(workspace, session_id)
    stop_path = Path(_session_paths_record(workspace, session_id)["stop"])
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.write_text(runtime_store.format_utc() + "\n", encoding="utf-8")
    return _update_store_state(workspace, session_id, status="stopped", stop_requested=True)


def _update_store_state(workspace: str | Path, session_id: str, **updates: Any) -> JsonDict:
    updater = getattr(runtime_store, "update_state", None)
    if updater is not None:
        return updater(workspace, session_id, **updates)
    state = runtime_store.read_state(workspace, session_id, default_status="running")
    state.update(updates)
    return runtime_store.write_state(workspace, session_id, state)


def _session_paths_record(workspace: str | Path, session_id: str) -> JsonDict:
    paths = getattr(runtime_store, "session_paths", None)
    if paths is not None:
        record = paths(workspace, session_id)
        return {key: str(value) for key, value in getattr(record, "__dict__", {}).items()} or {
            "manifest": str(record.manifest),
            "samples": str(record.samples),
            "state": str(record.state),
            "stop": str(record.stop),
        }
    root_func = getattr(runtime_store, "runtime_sessions_dir", None)
    if root_func is not None:
        root = Path(root_func(workspace, create=True))
    else:
        root = Path(runtime_store.runtime_evidence_dir(workspace)) / "sessions"
        root.mkdir(parents=True, exist_ok=True)
    safe_id = runtime_store.validate_session_id(session_id)
    return {
        "manifest": str(root / f"{safe_id}.manifest.json"),
        "samples": str(root / f"{safe_id}.samples.jsonl"),
        "state": str(root / f"{safe_id}.state.json"),
        "stop": str(root / f"{safe_id}.stop"),
    }
