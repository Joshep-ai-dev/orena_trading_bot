"""Read Orenya task JSON from Electron's local Chromium HTTP cache."""

from __future__ import annotations

import json
from pathlib import Path

import win32con
import win32file


CACHE_DIRECTORY = (
    Path.home() / "AppData" / "Roaming" / "orenya-commerce-agent" / "Cache" / "Cache_Data"
)


def _read_shared(path: Path) -> bytes:
    """Read a live Chromium cache file without asking it to release its lock."""
    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_READ,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_ATTRIBUTE_NORMAL,
        None,
    )
    try:
        size = win32file.GetFileSize(handle)
        chunks = []
        remaining = size
        while remaining:
            _status, chunk = win32file.ReadFile(handle, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        win32file.CloseHandle(handle)


def _responses(path: Path):
    """Yield intact JSON responses embedded in a Chromium cache data file."""
    try:
        raw = _read_shared(path)
    except (OSError, PermissionError):
        return
    marker = b'{"wallet"'
    decoder = json.JSONDecoder()
    start = 0
    while True:
        offset = raw.find(marker, start)
        if offset < 0:
            return
        try:
            # Bytes after the JSON belong to unrelated Chromium cache records
            # and may not be UTF-8. Replacement does not alter the valid JSON
            # prefix consumed by raw_decode.
            text = raw[offset:].decode("utf-8", errors="replace")
            value, _end = decoder.raw_decode(text)
            if isinstance(value, dict):
                yield offset, value
        except json.JSONDecodeError:
            pass
        start = offset + len(marker)


def read_cached_task() -> dict | None:
    """Return the newest cached, incomplete Orenya training task."""
    if not CACHE_DIRECTORY.is_dir():
        return None
    candidates: list[tuple[int, int, str, dict]] = []
    for path in CACHE_DIRECTORY.iterdir():
        if not path.is_file() or not (path.name.startswith("data_") or path.name.startswith("f_")):
            continue
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            continue
        for offset, response in _responses(path):
            task = response.get("task")
            if not isinstance(task, dict) or not task.get("query") or not task.get("options"):
                continue
            if response.get("completed") is True:
                continue
            candidates.append((modified, offset, path.name, response))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3]


def cached_question_and_answers() -> tuple[str, list[tuple[str, str]]] | None:
    response = read_cached_task()
    if response is None:
        return None
    task = response["task"]
    question = " ".join(str(task["query"]).replace("\ufffd", "…").split())
    answers = []
    for option in task.get("options", []):
        label = str(option.get("id", "")).strip().upper()
        title = " ".join(str(option.get("title", "")).replace("\ufffd", "…").split())
        brand = " ".join(str(option.get("brand") or "").replace("\ufffd", "…").split())
        value = " ".join(part for part in (title, brand) if part)
        if label in {"A", "B", "C", "D"} and value:
            answers.append((label, value))
    return (question, answers) if question and answers else None
