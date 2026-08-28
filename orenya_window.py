"""Windows-handle and UI Automation access for Orenya Commerce Agent."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time

from pywinauto import Desktop
import win32con
import win32gui


WINDOW_TITLE = "Orenya Commerce Agent"


@dataclass(frozen=True)
class OrenyaObject:
    kind: str
    name: str
    control_type: str
    automation_id: str
    class_name: str
    rectangle: tuple[int, int, int, int]
    enabled: bool
    visible: bool
    handle: int

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rectangle
        return (left + right) // 2, (top + bottom) // 2


@dataclass
class OrenyaTask:
    text: str
    answers: list[tuple[str, str]]
    rate_limited: bool
    signature: tuple[tuple[str, str], ...]
    answer_controls: dict[str, object]


def find_orenya_window():
    """Return Orenya's top-level UIA wrapper, including background/hidden windows."""
    for window in Desktop(backend="uia").windows(visible_only=False, enabled_only=False):
        title = window.window_text().strip()
        if WINDOW_TITLE.casefold() in title.casefold():
            return window
    # Some Electron windows do not appear through the UIA top-level query while
    # inactive. Enumerate native HWNDs, then wrap the match with UIA.
    handles: list[int] = []

    def enum_window(handle: int, _extra) -> bool:
        title = win32gui.GetWindowText(handle).strip()
        if WINDOW_TITLE.casefold() in title.casefold():
            handles.append(handle)
        return True

    win32gui.EnumWindows(enum_window, None)
    if handles:
        return Desktop(backend="uia").window(handle=handles[0])
    return None


def activate_orenya_window() -> int | None:
    """Restore/show/focus Orenya so screen capture and physical clicks are reliable."""
    window = find_orenya_window()
    if window is None:
        return None
    handle = int(window.handle)
    if win32gui.IsIconic(handle):
        win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    elif not win32gui.IsWindowVisible(handle):
        win32gui.ShowWindow(handle, win32con.SW_SHOW)
    try:
        win32gui.SetForegroundWindow(handle)
    except Exception:
        try:
            window.set_focus()
        except Exception:
            pass
    time.sleep(0.2)
    return handle


def classify(control_type: str, name: str, automation_id: str, class_name: str) -> str:
    searchable = f"{name} {automation_id} {class_name}".casefold()
    if any(word in searchable for word in ("rate limit", "toast", "bubble", "alert", "status")):
        return "bubble"
    if control_type == "Button":
        return "button"
    if control_type in {"List", "ListItem", "DataGrid", "DataItem"}:
        return "list"
    if control_type in {"Text", "Edit", "Document", "Hyperlink"}:
        return "text"
    return "other"


def detect_objects() -> tuple[int, list[OrenyaObject]]:
    """Enumerate accessible objects below the Orenya native window handle."""
    window = find_orenya_window()
    if window is None:
        raise RuntimeError("No top-level 'Orenya Commerce Agent' window handle was found.")
    objects: list[OrenyaObject] = []
    seen: set[tuple] = set()
    for control in window.descendants():
        info = control.element_info
        name = (info.name or "").strip()
        control_type = info.control_type or ""
        automation_id = info.automation_id or ""
        class_name = info.class_name or ""
        rectangle = info.rectangle
        bounds = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
        key = (control_type, name, automation_id, bounds)
        if key in seen:
            continue
        seen.add(key)
        try:
            enabled = bool(control.is_enabled())
            visible = bool(control.is_visible())
        except Exception:
            enabled, visible = False, False
        objects.append(OrenyaObject(
            kind=classify(control_type, name, automation_id, class_name),
            name=name,
            control_type=control_type,
            automation_id=automation_id,
            class_name=class_name,
            rectangle=bounds,
            enabled=enabled,
            visible=visible,
            handle=int(info.handle or 0),
        ))
    objects.sort(key=lambda item: (item.rectangle[1], item.rectangle[0], item.kind, item.name))
    return int(window.handle), objects


def _named_controls(window) -> list[object]:
    """Return all named descendants in visual order without activating the window."""
    controls = []
    for control in window.descendants():
        try:
            name = (control.element_info.name or "").strip()
            rectangle = control.element_info.rectangle
        except Exception:
            continue
        if name:
            controls.append((rectangle.top, rectangle.left, control))
    controls.sort(key=lambda value: (value[0], value[1]))
    return [control for _top, _left, control in controls]


def read_task() -> OrenyaTask:
    """Read the current task entirely through Windows UI Automation."""
    window = find_orenya_window()
    if window is None:
        raise RuntimeError("No top-level 'Orenya Commerce Agent' window handle was found.")
    controls = _named_controls(window)
    lines: list[str] = []
    answers: dict[str, tuple[str, object]] = {}
    for control in controls:
        name = (control.element_info.name or "").strip()
        if not lines or lines[-1] != name:
            lines.append(name)
        match = re.match(r"^\s*([A-D])[.):]\s*(.+)$", name, re.DOTALL)
        if match:
            label = match.group(1).upper()
            value = " ".join(match.group(2).split())
            # Prefer the richest accessible name if Electron publishes both a
            # card and a nested text node for the same answer.
            if label not in answers or len(value) > len(answers[label][0]):
                answers[label] = (value, control)
    ordered = [(label, answers[label][0]) for label in "ABCD" if label in answers]
    text = "\n".join(lines)
    return OrenyaTask(
        text=text,
        answers=ordered,
        rate_limited="rate limit exceeded" in text.casefold(),
        signature=tuple(ordered),
        answer_controls={label: answers[label][1] for label in answers},
    )


def _perform_accessible_action(control) -> str:
    """Invoke a UIA action without focus, screen coordinates, or mouse input."""
    candidates = []
    current = control
    for _ in range(5):
        if current is None:
            break
        candidates.append(current)
        try:
            current = current.parent()
        except Exception:
            break
    errors: list[str] = []
    for candidate in candidates:
        for pattern, method in (
            ("SelectionItem", "Select"),
            ("Invoke", "Invoke"),
            ("Toggle", "Toggle"),
        ):
            try:
                interface = getattr(candidate, f"iface_{pattern.casefold().replace('item', '_item')}")
                getattr(interface, method)()
                return pattern
            except Exception as exc:
                errors.append(type(exc).__name__)
    raise RuntimeError(
        "Electron did not expose SelectionItem, Invoke, or Toggle for this element "
        f"(attempts: {', '.join(sorted(set(errors))) or 'none'})."
    )


def select_answer(label: str, task: OrenyaTask | None = None) -> str:
    task = task or read_task()
    control = task.answer_controls.get(label.upper())
    if control is None:
        raise RuntimeError(f"Answer {label.upper()} is not present in the UI Automation tree.")
    return _perform_accessible_action(control)


def submit_answer() -> str:
    """Find and invoke the enabled Submit answer control through UIA."""
    window = find_orenya_window()
    if window is None:
        raise RuntimeError("Orenya window handle disappeared.")
    for control in _named_controls(window):
        name = (control.element_info.name or "").strip().casefold()
        if name == "submit answer" and control.is_enabled():
            return _perform_accessible_action(control)
    raise RuntimeError("Enabled 'Submit answer' UI Automation element was not found.")


def print_objects() -> None:
    handle, objects = detect_objects()
    print(f"Orenya HWND: 0x{handle:X}")
    counts: dict[str, int] = {}
    for item in objects:
        counts[item.kind] = counts.get(item.kind, 0) + 1
        print(
            f"[{item.kind}] {item.name!r} type={item.control_type} "
            f"automation_id={item.automation_id!r} class={item.class_name!r} "
            f"rect={item.rectangle} center={item.center} enabled={item.enabled} "
            f"visible={item.visible} hwnd=0x{item.handle:X}"
        )
    print("Object counts: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
