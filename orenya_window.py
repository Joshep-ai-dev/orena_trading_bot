"""Windows-handle and UI Automation access for Orenya Commerce Agent."""

from __future__ import annotations

from dataclasses import dataclass
import re

from pywinauto import Desktop
from pywinauto import uia_defines
from comtypes import COMError
import win32con
import win32gui


WINDOW_TITLE = "Orenya Commerce Agent"
_cached_window_handle: int | None = None


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
    question: str
    question_source: str
    question_rectangle: tuple[int, int, int, int] | None
    text: str
    answers: list[tuple[str, str]]
    rate_limited: bool
    error_message: str
    submit_present: bool
    submit_enabled: bool
    signature: tuple[object, ...]
    answer_controls: dict[str, object]
    submit_control: object | None = None


def find_orenya_window():
    """Return Orenya's top-level UIA wrapper, including background/hidden windows."""
    global _cached_window_handle
    if _cached_window_handle and win32gui.IsWindow(_cached_window_handle):
        title = win32gui.GetWindowText(_cached_window_handle).strip()
        if WINDOW_TITLE.casefold() in title.casefold():
            return Desktop(backend="uia").window(handle=_cached_window_handle)
        _cached_window_handle = None
    for window in Desktop(backend="uia").windows(visible_only=False, enabled_only=False):
        title = window.window_text().strip()
        if WINDOW_TITLE.casefold() in title.casefold():
            _cached_window_handle = int(window.handle)
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
        _cached_window_handle = handles[0]
        return Desktop(backend="uia").window(handle=handles[0])
    return None


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


def _inactive_coordinate_offset(window) -> tuple[int, int]:
    """Translate minimized off-screen coordinates to the saved normal placement."""
    handle = int(window.handle)
    if not win32gui.IsIconic(handle):
        return 0, 0
    try:
        normal_left, normal_top, _right, _bottom = win32gui.GetWindowPlacement(handle)[4]
        current = window.element_info.rectangle
        return normal_left - current.left, normal_top - current.top
    except Exception:
        return 0, 0


def _translated_bounds(rectangle, offset: tuple[int, int]) -> tuple[int, int, int, int]:
    dx, dy = offset
    return (
        rectangle.left + dx,
        rectangle.top + dy,
        rectangle.right + dx,
        rectangle.bottom + dy,
    )


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


def _named_controls(window, descendants: list[object] | None = None) -> list[object]:
    """Return all named descendants in visual order without activating the window."""
    controls = []
    for control in descendants if descendants is not None else window.descendants():
        try:
            name = (control.element_info.name or "").strip()
            rectangle = control.element_info.rectangle
        except Exception:
            continue
        if name:
            controls.append((rectangle.top, rectangle.left, control))
    controls.sort(key=lambda value: (value[0], value[1]))
    return [control for _top, _left, control in controls]


def _text_pattern_documents(window, descendants: list[object] | None = None) -> list[str]:
    """Read rendered Electron text even when it has no standalone UIA Name."""
    documents: list[str] = []
    seen: set[str] = set()
    controls = [window, *(descendants if descendants is not None else window.descendants())]
    for control in controls:
        try:
            value = control.iface_text.DocumentRange.GetText(-1)
        except (uia_defines.NoPatternInterfaceError, COMError, AttributeError):
            continue
        value = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if value and value not in seen:
            seen.add(value)
            documents.append(value)
    return documents


def _question_from_document(text: str) -> str:
    """Extract the query line immediately before Orenya's fixed instruction."""
    lines = [" ".join(line.split()) for line in text.split("\n") if line.strip()]
    for index, line in enumerate(lines):
        if "pick the best product" not in line.casefold():
            continue
        for candidate in reversed(lines[:index]):
            lowered = candidate.casefold()
            if "product match" not in lowered and candidate:
                return candidate
    return ""


def _find_exact_text_range(
    window,
    wanted: str,
    descendants: list[object] | None = None,
) -> tuple[str, tuple[int, int, int, int] | None]:
    """Find exact rendered text by UIA Name or TextPattern range."""
    normalized = " ".join(wanted.split()).casefold()
    controls = descendants if descendants is not None else window.descendants()
    for control in controls:
        try:
            name = " ".join((control.element_info.name or "").split())
        except Exception:
            continue
        if name.casefold() == normalized:
            rect = control.element_info.rectangle
            return (
                f"UIA element name; type={control.element_info.control_type!r}",
                _translated_bounds(rect, _inactive_coordinate_offset(window)),
            )
    for control in [window, *controls]:
        try:
            text_range = control.iface_text.DocumentRange.FindText(wanted, False, True)
            if not text_range:
                continue
            exact = " ".join((text_range.GetText(-1) or "").split())
            if exact.casefold() != normalized:
                continue
            rectangles = list(text_range.GetBoundingRectangles() or [])
            bounds = None
            if len(rectangles) >= 4:
                lefts = rectangles[0::4]
                tops = rectangles[1::4]
                rights = [x + width for x, width in zip(lefts, rectangles[2::4])]
                bottoms = [y + height for y, height in zip(tops, rectangles[3::4])]
                raw = type("TextBounds", (), {
                    "left": int(min(lefts)), "top": int(min(tops)),
                    "right": int(max(rights)), "bottom": int(max(bottoms)),
                })()
                bounds = _translated_bounds(raw, _inactive_coordinate_offset(window))
            info = control.element_info
            return f"UIA TextPattern range; parent={info.control_type!r} name={(info.name or '').strip()!r}", bounds
        except (uia_defines.NoPatternInterfaceError, COMError, AttributeError, TypeError, ValueError):
            continue
    return "not exposed as an exact UIA element or TextRange", None


def read_task() -> OrenyaTask:
    """Read the current task entirely through Windows UI Automation."""
    window = find_orenya_window()
    if window is None:
        raise RuntimeError("No top-level 'Orenya Commerce Agent' window handle was found.")
    descendants = list(window.descendants())
    controls = _named_controls(window, descendants)
    document_texts = _text_pattern_documents(window, descendants)
    lines: list[str] = []
    answers: dict[str, tuple[str, object]] = {}
    submit_present = False
    submit_enabled = False
    submit_control = None
    error_messages: list[str] = []
    entries: list[tuple[int, int, int, str]] = []
    for control in controls:
        name = (control.element_info.name or "").strip()
        rectangle = control.element_info.rectangle
        entries.append((rectangle.top, rectangle.left, rectangle.bottom, name))
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
        lowered = name.casefold()
        if lowered == "submit answer":
            submit_present = True
            submit_control = submit_control or control
            try:
                if control.is_enabled():
                    submit_enabled = True
                    submit_control = control
            except Exception:
                pass
        control_type = (control.element_info.control_type or "").casefold()
        if control_type in {"text", "statusbar", "group", "custom"} and any(
            marker in lowered
            for marker in ("error:", "error occurred", "failed to", "unable to", "invalid request")
        ):
            error_messages.append(name)
    ordered = [(label, answers[label][0]) for label in "ABCD" if label in answers]
    text = "\n".join(lines)
    question = ""
    answer_entries = [entry for entry in entries if re.match(r"^\s*[A-D][.):]\s*", entry[3])]
    first_answer_top = min((entry[0] for entry in answer_entries), default=10**9)
    answer_left = min((entry[1] for entry in answer_entries), default=0)
    instruction_top = min(
        (top for top, left, bottom, line in entries
         if top < first_answer_top and "pick the best product" in line.casefold()),
        default=first_answer_top,
    )
    question_candidates = []
    for top, left, bottom, line in entries:
        lowered = line.casefold()
        if (
            not line
            or top >= instruction_top
            or instruction_top - bottom > 160
            or abs(left - answer_left) > 100
            or "product match" in lowered
            or "pick the best" in lowered
            or "rewards" in lowered
            or lowered in {"shop", "train & earn", "submit answer", "skip"}
        ):
            continue
        question_candidates.append((bottom, line))
    if question_candidates:
        # The shopping query is the nearest same-column text immediately above
        # Orenya's fixed "Pick the best product..." instruction.
        question = max(question_candidates)[1]
    # TextPattern is authoritative when Chromium renders the query without a
    # separate accessible element/name.
    document_questions = [
        candidate for candidate in (_question_from_document(value) for value in document_texts)
        if candidate
    ]
    if document_questions:
        question = min(document_questions, key=len)
    if document_texts:
        text = max(document_texts, key=len)
    try:
        from orenya_cache import cached_question_and_answers
        cached = cached_question_and_answers()
    except Exception:
        cached = None
    if cached:
        cached_question, cached_answers = cached
        cached_labels = {label for label, _value in cached_answers}
        exposed_labels = set(answers)
        # Only combine cache text with UIA controls when both describe the same
        # A-D layout. This prevents a stale cached response driving a new page.
        if cached_labels == exposed_labels:
            question = cached_question
            ordered = cached_answers
    question_source, question_rectangle = (
        _find_exact_text_range(window, question, descendants) if question else ("empty", None)
    )
    if cached and question == cached[0]:
        question_source = "Orenya Chromium cache task.query (exact API JSON)"
    return OrenyaTask(
        question=question,
        question_source=question_source,
        question_rectangle=question_rectangle,
        text=text,
        answers=ordered,
        rate_limited="rate limit exceeded" in text.casefold(),
        error_message=error_messages[0] if error_messages else "",
        submit_present=submit_present,
        submit_enabled=submit_enabled,
        signature=(question, *ordered),
        answer_controls={label: answers[label][1] for label in answers},
        submit_control=submit_control,
    )


def _perform_accessible_action(control, window=None) -> tuple[str, OrenyaObject]:
    """Invoke a UIA action without focus, screen coordinates, or mouse input."""
    if window is None:
        window = find_orenya_window()
    if window is None:
        raise RuntimeError("Orenya window handle disappeared before the UIA action.")
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
            ("LegacyIAccessible", "DoDefaultAction"),
        ):
            try:
                interface_name = {
                    "SelectionItem": "iface_selection_item",
                    "Invoke": "iface_invoke",
                    "Toggle": "iface_toggle",
                    "LegacyIAccessible": "iface_legacy_iaccessible",
                }[pattern]
                if pattern == "LegacyIAccessible":
                    interface = uia_defines.get_elem_interface(
                        candidate.element_info.element, "LegacyIAccessible"
                    )
                else:
                    interface = getattr(candidate, interface_name)
                matched = _control_object(candidate, window)
                previous_foreground = win32gui.GetForegroundWindow()
                try:
                    getattr(interface, method)()
                finally:
                    current_foreground = win32gui.GetForegroundWindow()
                    try:
                        current_root = win32gui.GetAncestor(current_foreground, win32con.GA_ROOT)
                    except Exception:
                        current_root = current_foreground
                    # Electron can foreground Orenya as a side effect of a UIA
                    # action. Restore the user's window only when Orenya stole
                    # focus; never override a window the user switched to.
                    if (
                        previous_foreground
                        and previous_foreground != int(window.handle)
                        and current_root == int(window.handle)
                        and win32gui.IsWindow(previous_foreground)
                    ):
                        try:
                            win32gui.SetForegroundWindow(previous_foreground)
                        except Exception:
                            pass
                return pattern, matched
            except (uia_defines.NoPatternInterfaceError, COMError, AttributeError) as exc:
                errors.append(type(exc).__name__)
    raise RuntimeError(
        "Electron did not expose SelectionItem, Invoke, Toggle, or LegacyIAccessible action "
        f"(attempts: {', '.join(sorted(set(errors))) or 'none'})."
    )


def _control_object(control, window) -> OrenyaObject:
    info = control.element_info
    rectangle = info.rectangle
    name = (info.name or "").strip()
    return OrenyaObject(
        kind=classify(info.control_type or "", name, info.automation_id or "", info.class_name or ""),
        name=name,
        control_type=info.control_type or "",
        automation_id=info.automation_id or "",
        class_name=info.class_name or "",
        rectangle=_translated_bounds(rectangle, _inactive_coordinate_offset(window)),
        enabled=bool(control.is_enabled()),
        visible=bool(control.is_visible()),
        handle=int(info.handle or 0),
    )


def select_answer(label: str, task: OrenyaTask | None = None) -> tuple[str, OrenyaObject]:
    task = task or read_task()
    control = task.answer_controls.get(label.upper())
    if control is None:
        raise RuntimeError(f"Answer {label.upper()} is not present in the UI Automation tree.")
    return _perform_accessible_action(control)


def submit_answer(task: OrenyaTask | None = None) -> tuple[str, OrenyaObject]:
    """Find and invoke the enabled Submit answer control through UIA."""
    window = find_orenya_window()
    if window is None:
        raise RuntimeError("Orenya window handle disappeared.")
    if task is not None and task.submit_control is not None:
        try:
            if task.submit_control.is_enabled():
                return _perform_accessible_action(task.submit_control, window)
        except Exception:
            pass
    for control in _named_controls(window):
        name = (control.element_info.name or "").strip().casefold()
        if name == "submit answer" and control.is_enabled():
            return _perform_accessible_action(control, window)
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
