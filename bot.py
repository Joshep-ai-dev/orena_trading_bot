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


def read_claude_result(config: dict) -> None:
    running_color = "#603B2F"
    status = tuple(config["claude_status"])

    # First observe Claude entering its black running state. This prevents the
    # idle button seen immediately after pressing Enter from being mistaken for completion.
    print(f"Waiting for Claude running color {running_color}...", flush=True)
    while rgb_hex(pyautogui.pixel(*status)) != running_color:
        time.sleep(0.05)

    print("Claude is running...", flush=True)
    while rgb_hex(pyautogui.pixel(*status)) == running_color:
        time.sleep(0.05)

    time.sleep(0.2)
    result_text = recognize(capture(config["claude_text_region"]))
    if not result_text:
        print("Claude finished, but no response text was recognized.", flush=True)
        return
    pyperclip.copy(result_text)
    print("Claude result (copied to clipboard):", flush=True)
    print(result_text, flush=True)


def run_once(config: dict) -> None:
    print("Capturing...", flush=True)
    text = recognize(capture(config["region"]))
    if not text:
        print("No text recognized. Adjust the region with F9.", flush=True)
        return
    pyperclip.copy(text)
    pyautogui.click(*config["target"])
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "v")
    if config.get("submit", False):
        time.sleep(0.15)
        pyautogui.press("enter")
    print(f"Pasted {len(text)} characters into Claude.", flush=True)
    if config.get("submit", False):
        read_claude_result(config)


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
        if key in (keyboard.Key.f7, keyboard.Key.f8):
            events.put("capture")
        elif key == keyboard.Key.f9:
            events.put("setup")
        elif key == keyboard.Key.f10:
            events.put("quit")

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    print("Ready: F7/F8 capture/paste once | F9 setup again | F10 quit")
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
        if action == "capture" and busy.acquire(blocking=False):
            try:
                run_once(config)
            except Exception as exc:  # Keep the hotkey service alive and expose the real error.
                print(f"Capture failed: {exc}", file=sys.stderr, flush=True)
            finally:
                busy.release()


if __name__ == "__main__":
    raise SystemExit(main())
