"""Select Orenya text, choose an answer locally, and submit it.

Windows usage:
    py -m pip install -r requirements.txt
    py bot.py --setup
    py bot.py
"""

from __future__ import annotations

import argparse
from collections import Counter
import ctypes
from datetime import datetime, timedelta, timezone
import json
import math
import queue
import re
import sys
import threading
import time
from pathlib import Path
import tkinter as tk

import mss
import numpy as np
from PIL import Image, ImageGrab, ImageTk
import pyautogui
import pyperclip
from pynput import keyboard


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
events: queue.Queue[str] = queue.Queue()
stop_requested = threading.Event()
last_first_item: tuple[int, int] | None = None
RATE_LIMIT_RETRY = "__RATE_LIMIT_RETRY__"
GMT_PLUS_9 = timezone(timedelta(hours=9))


def enable_dpi_awareness() -> None:
    """Keep Tk, MSS, and mouse coordinates aligned when Windows scaling is above 100%."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def save_config(region: tuple[int, int, int, int]) -> None:
    CONFIG_PATH.write_text(
        json.dumps({"region": region}, indent=2),
        encoding="utf-8",
    )


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("No config.json. Run: py bot.py --setup")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if len(data.get("region", [])) != 4:
        raise ValueError("config.json is invalid. Run: py bot.py --setup")
    return data


class ScreenPicker:
    def __init__(self, root: tk.Misc, title: str, mode: str) -> None:
        self.root = root
        self.mode = mode
        self.result = None
        self.start = None
        self.rect = None
        # A newly created Tk root can briefly map as a blank white window and
        # then be captured by ImageGrab. Keep it hidden until the overlay is ready.
        root.withdraw()
        root.update_idletasks()
        time.sleep(0.1)
        shot = ImageGrab.grab(all_screens=False)
        # Bind the image to this picker's Tcl interpreter. Without an explicit
        # master, GUI mode may create it in the main window's interpreter and
        # the picker canvas then raises: image "pyimageN" doesn't exist.
        self.photo = ImageTk.PhotoImage(shot, master=root)

        root.attributes("-fullscreen", True)
        root.attributes("-topmost", True)
        root.title(title)
        self.canvas = tk.Canvas(root, width=shot.width, height=shot.height, cursor="cross")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.create_rectangle(0, 0, shot.width, 42, fill="#111111", outline="")
        self.canvas.create_text(18, 21, anchor="w", fill="white", font=("Segoe UI", 14), text=title)
        root.bind("<Escape>", lambda _e: root.destroy())
        if mode == "region":
            self.canvas.bind("<ButtonPress-1>", self.press)
            self.canvas.bind("<B1-Motion>", self.drag)
            self.canvas.bind("<ButtonRelease-1>", self.release)
        else:
            self.canvas.bind("<Button-1>", self.click)
        root.deiconify()
        root.lift()
        root.focus_force()
        root.update_idletasks()

    def press(self, event) -> None:
        self.start = (event.x, event.y)
        self.rect = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="red", width=3)

    def drag(self, event) -> None:
        if self.start and self.rect:
            self.canvas.coords(self.rect, self.start[0], self.start[1], event.x, event.y)

    def release(self, event) -> None:
        # Windows/Tk can occasionally deliver a release when this overlay did
        # not receive the matching press (for example while windows switch).
        if self.start is None:
            return
        x1, y1 = self.start
        x2, y2 = event.x, event.y
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        if width >= 50 and height >= 50:
            self.result = (left, top, width, height)
            self.root.destroy()
        else:
            self.start = None
            if self.rect is not None:
                self.canvas.delete(self.rect)
                self.rect = None

    def click(self, event) -> None:
        self.result = (event.x, event.y)
        self.root.destroy()


def pick(title: str, mode: str, parent: tk.Misc | None = None):
    owns_root = parent is None
    root = tk.Tk() if owns_root else tk.Toplevel(parent)
    picker = ScreenPicker(root, title, mode)
    if owns_root:
        root.mainloop()
    else:
        parent.wait_window(root)
    return picker.result


def setup(parent: tk.Misc | None = None) -> None:
    region = pick("Drag a box around the Orenya question and choices. Esc cancels.", "region", parent)
    if not region:
        print("Setup cancelled.")
        return
    save_config(region)
    print(f"Saved {CONFIG_PATH}")


def capture_raw(region: list[int]) -> Image.Image:
    left, top, width, height = region
    with mss.mss() as sct:
        raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
    return Image.frombytes("RGB", raw.size, raw.rgb)


def grouped_rows(rows: np.ndarray, maximum_gap: int = 3) -> list[tuple[int, int]]:
    if len(rows) == 0:
        return []
    groups: list[tuple[int, int]] = []
    start = previous = int(rows[0])
    for value in rows[1:]:
        value = int(value)
        if value - previous > maximum_gap:
            groups.append((start, previous))
            start = value
        previous = value
    groups.append((start, previous))
    return groups


def find_orenya_sections(region: list[int]) -> list[tuple[int, int]]:
    """Locate bordered answer sections and return screen click positions."""
    pixels = np.asarray(capture_raw(region), dtype=np.int16)
    height, width = pixels.shape[:2]
    background = np.array([0x05, 0x08, 0x06], dtype=np.int16)
    color_distance = np.max(np.abs(pixels - background), axis=2)
    item_color = np.array([0x08, 0x0C, 0x09], dtype=np.int16)
    item_mask = np.max(np.abs(pixels - item_color), axis=2) <= 12
    bright = np.max(pixels, axis=2) >= 150

    def snap_to_click_color(position: tuple[int, int]) -> tuple[int, int]:
        """Move a center to the nearest exact #050806 pixel within 5 px."""
        local_x = position[0] - region[0]
        local_y = position[1] - region[1]
        target = np.array([0x05, 0x08, 0x06], dtype=np.int16)
        if 0 <= local_x < width and 0 <= local_y < height:
            if np.array_equal(pixels[local_y, local_x], target):
                return position
        offsets = sorted(
            (
                (dx * dx + dy * dy, dx, dy)
                for dy in range(-5, 6)
                for dx in range(-5, 6)
                if dx * dx + dy * dy <= 25
            ),
            key=lambda value: value[0],
        )
        for _distance, dx, dy in offsets:
            candidate_x, candidate_y = local_x + dx, local_y + dy
            if not (0 <= candidate_x < width and 0 <= candidate_y < height):
                continue
            if np.array_equal(pixels[candidate_y, candidate_x], target):
                return region[0] + candidate_x, region[1] + candidate_y
        return position

    def corrected(positions: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [snap_to_click_color(position) for position in positions]

    def item_center(top_edge: int, bottom_edge: int) -> tuple[int, int]:
        """Return the center of the #080C09 item pixels within two boundaries."""
        ys, xs = np.nonzero(item_mask[top_edge + 1:bottom_edge])
        if len(xs) >= 20:
            left_edge, right_edge = int(xs.min()), int(xs.max())
            center_x = (left_edge + right_edge) // 2
        else:
            center_x = width // 2
        return region[0] + center_x, region[1]  +  (top_edge + bottom_edge) // 2

    # Primary separator method: #0F1511 is the horizontal color between items.
    separator = np.array([0x0F, 0x15, 0x11], dtype=np.int16)
    separator_distance = np.max(np.abs(pixels - separator), axis=2)
    separator_rows = np.flatnonzero(
        np.count_nonzero(separator_distance <= 6, axis=1) >= max(12, int(width * 0.20))
    )
    separator_bands = grouped_rows(separator_rows, maximum_gap=2)
    separator_centers = [(start + end) // 2 for start, end in separator_bands]
    separated_sections: list[tuple[int, int]] = []
    for top_edge, bottom_edge in zip(separator_centers, separator_centers[1:]):
        item_height = bottom_edge - top_edge
        if not 18 <= item_height <= min(220, height):
            continue
        if np.count_nonzero(bright[top_edge + 1:bottom_edge]) < 8:
            continue
        if np.count_nonzero(item_mask[top_edge + 1:bottom_edge]) < 20:
            continue
        separated_sections.append(item_center(top_edge, bottom_edge))
    if len(separated_sections) >= 2:
        return corrected(separated_sections)

    # Primary method: try many columns across the right half. Card widths and
    # rounded corners vary, so right-10 may be outside their visible borders.
    # The best column is the one that separates the most text-containing runs.
    best_column_sections: list[tuple[int, int]] = []
    step = max(2, width // 80)
    for scan_x in range(width - 6, max(0, width // 2), -step):
        background_rows = np.flatnonzero(color_distance[:, scan_x] <= 6)
        background_runs = grouped_rows(background_rows, maximum_gap=1)
        candidate_sections: list[tuple[int, int]] = []
        for start, end in background_runs:
            run_height = end - start + 1
            if not 18 <= run_height <= min(220, height):
                continue
            if np.count_nonzero(bright[start:end + 1]) < 8:
                continue
            candidate_sections.append(item_center(start, end))
        if len(candidate_sections) > len(best_column_sections):
            best_column_sections = candidate_sections

    if len(best_column_sections) >= 2:
        return corrected(best_column_sections)

    # A section's dark horizontal border spans much more of the row than its text.
    border_rows = np.flatnonzero(np.count_nonzero(color_distance >= 4, axis=1) >= max(20, int(width * 0.70)))
    borders = grouped_rows(border_rows)
    border_centers = [(start + end) // 2 for start, end in borders]

    sections: list[tuple[int, int]] = []
    for top_edge, bottom_edge in zip(border_centers, border_centers[1:]):
        section_height = bottom_edge - top_edge
        if not 18 <= section_height <= min(220, height):
            continue
        # Reject spaces between cards: a real card contains visible text.
        if np.count_nonzero(bright[top_edge + 1:bottom_edge]) < 8:
            continue
        sections.append(item_center(top_edge, bottom_edge))

    # Remove duplicate centers caused by thick or decorated borders.
    unique: list[tuple[int, int]] = []
    for position in sections:
        if not unique or abs(position[1] - unique[-1][1]) >= 12:
            unique.append(position)
    return corrected(unique)


def find_color_cluster(
    region: list[int],
    color: tuple[int, int, int],
    left: int,
    right: int,
    tolerance: int = 45,
    screen_y_min: int = 450,
    screen_y_max: int = 900,
) -> tuple[int, int] | None:
    """Find the bottom-most color cluster within the requested screen-Y range."""
    pixels = np.asarray(capture_raw(region), dtype=np.int16)
    height, width = pixels.shape[:2]
    target_color = np.array(color, dtype=np.int16)
    left = max(0, min(width - 1, left))
    right = max(left + 1, min(width, right))
    top = max(0, screen_y_min - region[1])
    bottom = min(height, screen_y_max - region[1] + 1)
    if top >= bottom:
        return None
    strip = pixels[top:bottom, left:right]
    # Treat nearby rendered shades as the same button color. Browser gradients,
    # display scaling, and antialiasing commonly shift RGB channels slightly.
    distance = np.max(np.abs(strip - target_color), axis=2)
    matching = distance <= tolerance

    # Locate the lowest substantial orange band, then use the bounding-box
    # center of its pixels instead of assuming one exact x coordinate.
    # A submit button is a filled color block. Requiring several matching
    # pixels per row rejects thin orange outlines around selected answers.
    minimum_row_pixels = max(12, min(30, (right - left) // 20))
    matching_rows = np.flatnonzero(
        np.count_nonzero(matching, axis=1) >= minimum_row_pixels
    )
    runs = grouped_rows(matching_rows, maximum_gap=2)
    if not runs:
        return None
    start, end = runs[-1]
    ys, xs = np.nonzero(matching[start:end + 1])
    if len(xs) < 100:
        return None
    local_x = (int(xs.min()) + int(xs.max())) // 2
    local_y = start + (int(ys.min()) + int(ys.max())) // 2
    return region[0] + left + local_x, region[1] + top + local_y


def find_orenya_submit(region: list[int]) -> tuple[int, int] | None:
    """Find the moved #F79346 submit control across the configured area width."""
    return find_color_cluster(region, (0xF7, 0x93, 0x46), 0, region[2])


def find_orenya_ready_submit(region: list[int]) -> tuple[int, int] | None:
    """Refind the moved submit control by its #774E29 ready-state color."""
    return find_color_cluster(region, (0x77, 0x4E, 0x29), 0, region[2])


def show_orenya_sections(region: list[int]) -> list[tuple[int, int]]:
    global last_first_item
    positions = find_orenya_sections(region)
    if not positions:
        if last_first_item:
            positions = [last_first_item]
    else:
        last_first_item = positions[0]
    return positions


def copy_orenya_text(region: list[int]) -> str:
    """Select the Orenya region as browser text and return the clipboard value."""
    left, top, _width, height = region
    top_left = (left + 5, top + 5)
    bottom_right = (left +_width - 5, top + height - 5)
    pyperclip.copy("")
    pyautogui.click(*top_left)
    time.sleep(0.1)
    pyautogui.keyDown("shift")
    try:
        pyautogui.click(*bottom_right)
    finally:
        pyautogui.keyUp("shift")
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.2)
    return pyperclip.paste().strip()


def extract_answer_list(text: str) -> list[tuple[str, str]]:
    """Extract multiline A/B/C/D entries from selected Orenya page text."""
    text = re.split(r"(?im)^\s*(?:Submit answer|Skip)\s*$", text, maxsplit=1)[0]
    matches = re.findall(
        r"(?ms)^\s*([A-D])[.):]\s*(.*?)(?=^\s*[A-D][.):]\s|\Z)", text
    )
    return [(label.upper(), " ".join(value.split())) for label, value in matches]


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "best", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "pick", "product", "shopping", "the", "this", "to",
    "with", "your",
}


def tokens(text: str) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9]+", text.casefold()) if word not in STOP_WORDS]


def extract_query(text: str) -> str:
    first_answer = re.search(r"(?m)^\s*[A-D][.):]\s*", text)
    prefix = text[:first_answer.start()] if first_answer else text
    candidates: list[str] = []
    for raw_line in prefix.splitlines():
        line = raw_line.strip()
        lowered = line.casefold()
        if not line or "product match" in lowered or "pick the best" in lowered or "rewards" in lowered:
            continue
        candidates.append(line)
    return candidates[-1] if candidates else ""


def choose_local_answer(text: str) -> tuple[str, list[tuple[str, float]]]:
    """Rank answers using a small local TF-IDF/cosine text model."""
    answers = extract_answer_list(text)
    if not answers:
        return "A", []
    query = extract_query(text)
    query_counts = Counter(tokens(query))
    answer_counts = [Counter(tokens(value)) for _label, value in answers]
    document_count = len(answer_counts)
    document_frequency = Counter(
        word for counts in answer_counts for word in counts
    )

    def weight(word: str) -> float:
        return math.log((document_count + 1) / (document_frequency[word] + 1)) + 1.0

    query_norm = math.sqrt(sum((count * weight(word)) ** 2 for word, count in query_counts.items()))
    scores: list[tuple[str, float]] = []
    for (label, value), counts in zip(answers, answer_counts):
        answer_norm = math.sqrt(sum((count * weight(word)) ** 2 for word, count in counts.items()))
        dot = sum(
            query_count * weight(word) * counts.get(word, 0) * weight(word)
            for word, query_count in query_counts.items()
        )
        score = dot / (query_norm * answer_norm) if query_norm and answer_norm else 0.0
        if query and query.casefold() in value.casefold():
            score += 1.0
        scores.append((label, score))
    return max(scores, key=lambda item: item[1])[0], scores


def pause_for_rate_limit() -> bool:
    print("Orenya rate limit detected; waiting 10 minutes before one retry...", flush=True)
    if stop_requested.wait(600.0):
        return False
    print("10-minute wait finished; running one F7 retry check.", flush=True)
    return True


def wait_for_daily_schedule() -> bool:
    """Pause daily from 07:59:58 until 09:00:00 in GMT+9."""
    now = datetime.now(GMT_PLUS_9)
    seconds_after_midnight = now.hour * 3600 + now.minute * 60 + now.second
    if not 7 * 3600 + 59 * 60 + 58 <= seconds_after_midnight < 9 * 3600:
        return not stop_requested.is_set()
    resume_at = now.replace(hour=9, minute=0, second=0, microsecond=0)
    seconds = max(0.0, (resume_at - now).total_seconds())
    print(
        f"GMT+9 schedule pause at {now:%H:%M:%S}; resuming at 09:00:00 "
        f"({seconds:.1f}s remaining).",
        flush=True,
    )
    if stop_requested.wait(seconds):
        return False
    print("GMT+9 time is 09:00:00; automation resumed.", flush=True)
    return True


def answer_index(result_text: str) -> tuple[str, int]:
    """Map A-D to items 1-4; all other recognized text maps to item 1."""
    match = re.search(r"(?im)^\s*(?:ANSWER\s*[:\-]?\s*)?([ABCD])(?:\s*[.):\-]|\s*$)", result_text)
    if not match:
        return "other", 0
    answer = match.group(1).upper()
    return answer, ord(answer) - ord("A")


def click_orenya_answer(result_text: str, positions: list[tuple[int, int]]) -> bool:
    answer, index = answer_index(result_text)
    if not positions:
        print("Cannot click Orenya: no answer sections were detected.", flush=True)
        return False
    if index >= len(positions):
        print(f"Item {index + 1} was not found; selecting the first item instead.", flush=True)
        index = 0
        answer = "fallback-first"
    x, y = positions[index]
    pyautogui.click(x, y)
    print(f"Orenya click: answer={answer}, item={index + 1}, x={x}, y={y}", flush=True)
    return True


def click_orenya_submit(region: list[int]) -> tuple[int, int] | None:
    position = find_orenya_submit(region)
    if not position:
        print("Cannot click submit: no color similar to #F79346 was found.", flush=True)
        return None
    pyautogui.click(*position)
    moved_x = max(0, position[0] - 300)
    pyautogui.moveTo(moved_x, position[1], duration=0.15)
    print(
        f"Submit clicked: x={position[0]}, y={position[1]}; mouse moved to x={moved_x}",
        flush=True,
    )
    return position


def wait_for_next_orenya(region: list[int], submitted_at: tuple[int, int]) -> bool:
    """Wait for #080C09, then refind the moved #774E29 submit control."""
    inactive = np.array([0x08, 0x0C, 0x09], dtype=np.int16)
    saw_inactive = False
    deadline = time.monotonic() + 20.0
    while not stop_requested.is_set():
        color = np.array(pyautogui.pixel(*submitted_at), dtype=np.int16)
        if np.max(np.abs(color - inactive)) <= 20:
            saw_inactive = True
        if find_orenya_submit(region) is None:
            break
        if time.monotonic() >= deadline:
            print("No Orenya state change for 20 seconds; restarting the F7 workflow.", flush=True)
            return True
        time.sleep(0.2)

    if stop_requested.is_set():
        return False

    if saw_inactive:
        print("Inactive background detected; waiting 5 seconds...", flush=True)
        if stop_requested.wait(5.0):
            return False

    deadline = time.monotonic() + 20.0
    while not stop_requested.is_set():
        ready_position = find_orenya_ready_submit(region)
        if ready_position is not None:
            return True
        if time.monotonic() >= deadline:
            print("No Orenya ready-state change for 20 seconds; restarting F7.", flush=True)
            return True
        time.sleep(0.2)
    return False


def run_once(config: dict) -> tuple[int, int] | str | None:
    if not wait_for_daily_schedule():
        return None
    positions = show_orenya_sections(config["region"])
    text = copy_orenya_text(config["region"])
    if not text:
        print("Selected Orenya text is empty; restarting the F7 workflow.", flush=True)
        return RATE_LIMIT_RETRY
    selected, scores = choose_local_answer(text)
    best_score = dict(scores).get(selected, 0.0)
    print(f"Selected answer: {selected} (score={best_score:.4f})", flush=True)
    if not wait_for_daily_schedule():
        return None
    if click_orenya_answer(selected, positions):
        time.sleep(0.2)
        if not wait_for_daily_schedule():
            return None
        return click_orenya_submit(config["region"])
    return None


def run_repeating(config: dict) -> None:
    while not stop_requested.is_set():
        if not wait_for_daily_schedule():
            return
        submitted_at = run_once(config)
        if submitted_at == RATE_LIMIT_RETRY:
            continue
        if not submitted_at:
            return
        if not wait_for_next_orenya(config["region"], submitted_at):
            return
        next_text = copy_orenya_text(config["region"])
        if "rate limit exceeded" in next_text.casefold():
            print("Next result is rate-limited.", flush=True)
            if not pause_for_rate_limit():
                return


def main() -> int:
    enable_dpi_awareness()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="select the Orenya task region")
    parser.add_argument("--once", action="store_true", help="choose and submit once, then quit")
    args = parser.parse_args()
    if args.setup:
        setup()
        return 0
    try:
        config = load_config()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.once:
        while not stop_requested.is_set():
            if run_once(config) != RATE_LIMIT_RETRY:
                break
        return 0

    busy = threading.Lock()

    def on_press(key) -> None:
        if key == keyboard.Key.f7:
            events.put("repeat")
        elif key == keyboard.Key.f8:
            events.put("capture")
        elif key == keyboard.Key.f9:
            events.put("setup")
        elif key == keyboard.Key.f10:
            stop_requested.set()
            events.put("quit")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    print("Ready: F7 repeat | F8 once | F9 setup again | F10 quit")
    while True:
        action = events.get()
        if action == "quit":
            listener.stop()
            return 0
        if action == "setup":
            listener.stop()
            setup()
            print("Restart the bot to use the new settings.")
            return 0
        if action in ("capture", "repeat") and busy.acquire(blocking=False):
            try:
                if action == "repeat":
                    run_repeating(config)
                else:
                    while not stop_requested.is_set():
                        if run_once(config) != RATE_LIMIT_RETRY:
                            break
            except Exception as exc:  # Keep the hotkey service alive and expose the real error.
                print(f"Capture failed: {exc}", file=sys.stderr, flush=True)
            finally:
                busy.release()


if __name__ == "__main__":
    raise SystemExit(main())
