"""Capture an Orenya task, OCR it, and paste the text into Claude.

Windows usage:
    py -m pip install -r requirements.txt
    py bot.py --setup
    py bot.py
"""

from __future__ import annotations

import argparse
import ctypes
import json
import queue
import re
import sys
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

import mss
import numpy as np
from PIL import Image, ImageEnhance, ImageGrab, ImageTk
import pyautogui
import pyperclip
from pynput import keyboard
from rapidocr_onnxruntime import RapidOCR


CONFIG_PATH = Path(__file__).with_name("config.json")
OCR = RapidOCR()
events: queue.Queue[str] = queue.Queue()
stop_requested = threading.Event()


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


def save_config(
    region: tuple[int, int, int, int],
    target: tuple[int, int],
    submit: bool,
    claude_status: tuple[int, int],
    claude_text_region: tuple[int, int, int, int],
) -> None:
    CONFIG_PATH.write_text(
        json.dumps({
            "region": region,
            "target": target,
            "submit": submit,
            "claude_status": claude_status,
            "claude_text_region": claude_text_region,
        }, indent=2),
        encoding="utf-8",
    )


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("No config.json. Run: py bot.py --setup")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if (len(data.get("region", [])) != 4
            or len(data.get("target", [])) != 2
            or len(data.get("claude_status", [])) != 2
            or len(data.get("claude_text_region", [])) != 4):
        raise ValueError("config.json is invalid. Run: py bot.py --setup")
    return data


class ScreenPicker:
    def __init__(self, root: tk.Tk, title: str, mode: str) -> None:
        self.root = root
        self.mode = mode
        self.result = None
        self.start = None
        self.rect = None
        shot = ImageGrab.grab(all_screens=False)
        self.photo = ImageTk.PhotoImage(shot)

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


def pick(title: str, mode: str):
    root = tk.Tk()
    picker = ScreenPicker(root, title, mode)
    root.mainloop()
    return picker.result


def setup() -> None:
    region = pick("Drag a box around the Orenya question and choices. Esc cancels.", "region")
    if not region:
        print("Setup cancelled.")
        return
    target = pick("Click inside Claude's 'Write a message' box. Esc cancels.", "point")
    if not target:
        print("Setup cancelled.")
        return
    claude_status = pick("Click the #603B2F part of Claude's running/submit button.", "point")
    if not claude_status:
        print("Setup cancelled.")
        return
    claude_text_region = pick("Drag a box around Claude's response text area.", "region")
    if not claude_text_region:
        print("Setup cancelled.")
        return
    root = tk.Tk()
    root.withdraw()
    submit = messagebox.askyesno("Auto-submit", "Press Enter automatically after pasting into Claude?")
    root.destroy()
    save_config(region, target, submit, claude_status, claude_text_region)
    print(f"Saved {CONFIG_PATH}")


def capture(region: list[int]) -> Image.Image:
    left, top, width, height = region
    with mss.mss() as sct:
        raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
    image = Image.frombytes("RGB", raw.size, raw.rgb)
    # Mild enlargement and contrast improve small browser text without destroying punctuation.
    image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
    return ImageEnhance.Contrast(image).enhance(1.25)


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
    bright = np.max(pixels, axis=2) >= 150
    click_x = region[0] + width - 10

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
        separated_sections.append((click_x, region[1] + (top_edge + bottom_edge) // 2))
    if len(separated_sections) >= 2:
        return separated_sections

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
            candidate_sections.append((click_x, region[1] + (start + end) // 2))
        if len(candidate_sections) > len(best_column_sections):
            best_column_sections = candidate_sections

    if len(best_column_sections) >= 2:
        return best_column_sections

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
        sections.append((region[0] + width - 10, region[1] + (top_edge + bottom_edge) // 2))

    # Remove duplicate centers caused by thick or decorated borders.
    unique: list[tuple[int, int]] = []
    for position in sections:
        if not unique or abs(position[1] - unique[-1][1]) >= 12:
            unique.append(position)
    return unique


def find_color_cluster(
    region: list[int], color: tuple[int, int, int], left: int, right: int, tolerance: int = 45
) -> tuple[int, int] | None:
    """Find the center of the bottom-most matching cluster in the lower half."""
    pixels = np.asarray(capture_raw(region), dtype=np.int16)
    height, width = pixels.shape[:2]
    target_color = np.array(color, dtype=np.int16)
    left = max(0, min(width - 1, left))
    right = max(left + 1, min(width, right))
    top = height // 2
    strip = pixels[top:height, left:right]
    # Treat nearby rendered shades as the same button color. Browser gradients,
    # display scaling, and antialiasing commonly shift RGB channels slightly.
    distance = np.max(np.abs(strip - target_color), axis=2)
    matching = distance <= tolerance

    # Locate the lowest substantial orange band, then use the bounding-box
    # center of its pixels instead of assuming one exact x coordinate.
    matching_rows = np.flatnonzero(np.count_nonzero(matching, axis=1) >= 3)
    runs = grouped_rows(matching_rows, maximum_gap=2)
    if not runs:
        return None
    start, end = runs[-1]
    ys, xs = np.nonzero(matching[start:end + 1])
    if len(xs) < 6:
        return None
    local_x = (int(xs.min()) + int(xs.max())) // 2
    local_y = start + (int(ys.min()) + int(ys.max())) // 2
    return region[0] + left + local_x, region[1] + top + local_y


def find_orenya_submit(region: list[int]) -> tuple[int, int] | None:
    """Find #F79346 near right-120, then retry globally if the button moved."""
    width = region[2]
    expected_x = max(0, width - 120)
    position = find_color_cluster(
        region, (0xF7, 0x93, 0x46), expected_x - 100, expected_x + 101
    )
    return position or find_color_cluster(region, (0xF7, 0x93, 0x46), 0, width)


def find_orenya_ready_submit(region: list[int]) -> tuple[int, int] | None:
    """Refind the moved submit control by its #774E29 ready-state color."""
    return find_color_cluster(region, (0x77, 0x4E, 0x29), 0, region[2])


def show_orenya_sections(region: list[int]) -> list[tuple[int, int]]:
    positions = find_orenya_sections(region)
    if not positions:
        print("Orenya sections: none detected.", flush=True)
    else:
        print(f"Orenya sections ({len(positions)}):", flush=True)
        for index, (x, y) in enumerate(positions):
            label = chr(ord("A") + index) if index < 26 else str(index + 1)
            print(f"  {label}: x={x}, y={y}", flush=True)
    submit = find_orenya_submit(region)
    if submit:
        print(f"Orenya submit: x={submit[0]}, y={submit[1]}", flush=True)
    else:
        print("Orenya submit: #F79346 cluster not found near area right-120.", flush=True)
    return positions


def recognize(image: Image.Image) -> str:
    result, _elapsed = OCR(np.asarray(image))
    if not result:
        return ""
    # RapidOCR returns [box, text, confidence]. Sorting restores reading order.
    rows = sorted(result, key=lambda item: (min(p[1] for p in item[0]), min(p[0] for p in item[0])))
    lines = [item[1].strip() for item in rows if item[1].strip() and float(item[2]) >= 0.35]
    return "\n".join(lines)


def rgb_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def read_claude_result(config: dict) -> str | None:
    running_color = "#603B2F"
    status = tuple(config["claude_status"])

    # First observe Claude entering its black running state. This prevents the
    # idle button seen immediately after pressing Enter from being mistaken for completion.
    print(f"Waiting for Claude running color {running_color}...", flush=True)
    while not stop_requested.is_set() and rgb_hex(pyautogui.pixel(*status)) != running_color:
        time.sleep(0.05)

    if stop_requested.is_set():
        return None

    print("Claude is running...", flush=True)
    while not stop_requested.is_set() and rgb_hex(pyautogui.pixel(*status)) == running_color:
        time.sleep(0.05)

    if stop_requested.is_set():
        return None

    time.sleep(0.2)
    result_text = recognize(capture(config["claude_text_region"]))
    if not result_text:
        print("Claude finished, but no response text was recognized.", flush=True)
        return None
    pyperclip.copy(result_text)
    print("Claude result (copied to clipboard):", flush=True)
    print(result_text, flush=True)
    return result_text


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
        print(
            f"Cannot click Orenya item {index + 1}: only {len(positions)} sections were detected.",
            flush=True,
        )
        return False
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
    print(f"Orenya submit clicked: x={position[0]}, y={position[1]}", flush=True)
    return position


def wait_for_next_orenya(region: list[int], submitted_at: tuple[int, int]) -> bool:
    """Wait for #080C09, then refind the moved #774E29 submit control."""
    inactive = np.array([0x08, 0x0C, 0x09], dtype=np.int16)
    saw_inactive = False
    print("Waiting for Orenya submit button to become inactive...", flush=True)
    while not stop_requested.is_set():
        color = np.array(pyautogui.pixel(*submitted_at), dtype=np.int16)
        if np.max(np.abs(color - inactive)) <= 20:
            if not saw_inactive:
                print("Orenya background is #080C09; waiting...", flush=True)
                saw_inactive = True
        if find_orenya_submit(region) is None:
            break
        time.sleep(0.2)

    if stop_requested.is_set():
        return False

    print("Refinding moved submit button with color similar to #774E29...", flush=True)
    while not stop_requested.is_set():
        ready_position = find_orenya_ready_submit(region)
        if ready_position is not None:
            print(
                f"Orenya ready submit found: x={ready_position[0]}, y={ready_position[1]}; "
                "starting the next F7 cycle.",
                flush=True,
            )
            return True
        time.sleep(0.2)
    return False


def run_once(config: dict) -> tuple[int, int] | None:
    print("Capturing...", flush=True)
    positions = show_orenya_sections(config["region"])
    text = recognize(capture(config["region"]))
    if not text:
        print("No text recognized. Adjust the region with F9.", flush=True)
        return None
    pyperclip.copy(text)
    pyautogui.click(*config["target"])
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "v")
    if config.get("submit", False):
        time.sleep(0.15)
        pyautogui.press("enter")
    print(f"Pasted {len(text)} characters into Claude.", flush=True)
    if config.get("submit", False):
        result_text = read_claude_result(config)
        if result_text:
            if click_orenya_answer(result_text, positions):
                time.sleep(0.2)
                return click_orenya_submit(config["region"])
    return None


def run_repeating(config: dict) -> None:
    while not stop_requested.is_set():
        submitted_at = run_once(config)
        if not submitted_at:
            return
        if not wait_for_next_orenya(config["region"], submitted_at):
            return


def main() -> int:
    enable_dpi_awareness()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="select OCR region and Claude input")
    parser.add_argument("--once", action="store_true", help="capture and paste once, then quit")
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
        run_once(config)
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
                    run_once(config)
            except Exception as exc:  # Keep the hotkey service alive and expose the real error.
                print(f"Capture failed: {exc}", file=sys.stderr, flush=True)
            finally:
                busy.release()


if __name__ == "__main__":
    raise SystemExit(main())
