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
import random
import re
import sys
import threading
import time
from pathlib import Path
import tkinter as tk

from PIL import ImageGrab, ImageTk
from pynput import keyboard


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
events: queue.Queue[str] = queue.Queue()
stop_requested = threading.Event()
RATE_LIMIT_RETRY = "__RATE_LIMIT_RETRY__"
SELECT_DELAY_RANGE = (3.0, 4.0)
SUBMIT_DELAY_RANGE = (0.4, 0.7)


def ux(status: str, message: str) -> None:
    """Write one clear, user-facing status line."""
    print(f"[{status}] {message}", flush=True)
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
    ux("PAUSED", "Rate limit reached. Retrying in 10 minutes.")
    if stop_requested.wait(600.0):
        return False
    ux("RESUMED", "The 10-minute wait is complete. Trying again.")
    return True


def wait_for_daily_schedule() -> bool:
    """Pause daily from 07:59:58 until 09:00:00 in GMT+9."""
    now = datetime.now(GMT_PLUS_9)
    seconds_after_midnight = now.hour * 3600 + now.minute * 60 + now.second
    if not 7 * 3600 + 59 * 60 + 58 <= seconds_after_midnight < 9 * 3600:
        return not stop_requested.is_set()
    resume_at = now.replace(hour=9, minute=0, second=0, microsecond=0)
    seconds = max(0.0, (resume_at - now).total_seconds())
    ux("PAUSED", f"Daily pause is active. Resuming at 09:00:00 GMT+9.")
    if stop_requested.wait(seconds):
        return False
    ux("RESUMED", "Daily pause finished.")
    return True


def run_once(config: dict | None = None, prefetched_task=None):
    """Read, choose, select, and submit using UI Automation only."""
    cycle_started = time.perf_counter()
    if not wait_for_daily_schedule():
        return None
    from orenya_window import read_task, select_answer, submit_answer
    read_started = time.perf_counter()
    task = prefetched_task if prefetched_task is not None else read_task()
    read_elapsed = 0.0 if prefetched_task is not None else time.perf_counter() - read_started
    if not task.answers:
        ux("WAITING", "The current question is not ready yet.")
        return RATE_LIMIT_RETRY
    query = task.question
    ux("QUESTION", query or "Question text is unavailable.")
    for label, value in task.answers:
        ux("OPTION", f"{label}. {value}")
    model_text = "\n".join([query] + [f"{label}. {value}" for label, value in task.answers])
    selected, _scores = choose_local_answer(model_text)
    selected_text = dict(task.answers).get(selected, "")
    ux("SELECTED", f"{selected}. {selected_text}")
    if not wait_for_daily_schedule():
        return None
    select_delay = random.uniform(*SELECT_DELAY_RANGE)
    ux("WAITING", f"Selecting answer in {select_delay:.2f}s.")
    if stop_requested.wait(select_delay) or not wait_for_daily_schedule():
        return None
    select_started = time.perf_counter()
    _action, _answer_object = select_answer(selected, task)
    select_elapsed = time.perf_counter() - select_started
    ux("ACTION", f"Answer {selected} selected.")
    submit_delay = random.uniform(*SUBMIT_DELAY_RANGE)
    ux("WAITING", f"Submitting answer in {submit_delay:.2f}s.")
    if stop_requested.wait(submit_delay) or not wait_for_daily_schedule():
        return None
    deadline = time.monotonic() + 20.0
    submit_started = time.perf_counter()
    while not stop_requested.is_set():
        try:
            _submit_action, _submit_object = submit_answer(task)
            submit_elapsed = time.perf_counter() - submit_started
            total_elapsed = time.perf_counter() - cycle_started
            ux("SUBMITTED", "Answer submitted successfully.")
            ux(
                "TIMING",
                "read={:.2f}s select={:.2f}s submit={:.2f}s total={:.2f}s".format(
                    read_elapsed, select_elapsed, submit_elapsed, total_elapsed
                ),
            )
            return task.signature
        except RuntimeError as exc:
            if time.monotonic() >= deadline:
                submit_elapsed = time.perf_counter() - submit_started
                ux("ERROR", f"Could not submit the answer: {exc}")
                ux(
                    "TIMING",
                    "read={:.2f}s select={:.2f}s submit_failed_after={:.2f}s".format(
                        read_elapsed, select_elapsed, submit_elapsed
                    ),
                )
                return None
            stop_requested.wait(0.1)
    return None


def _wait_for_next_uia(old_signature):
    """Classify post-submit UIA state and wait for a complete new task."""
    from orenya_window import read_task
    ux("WAITING", "Waiting for the next question.")
    last_error = ""
    wait_started = time.perf_counter()
    read_count = 0
    slowest_read = 0.0
    while not stop_requested.is_set():
        try:
            read_started = time.perf_counter()
            task = read_task()
            read_elapsed = time.perf_counter() - read_started
            read_count += 1
            slowest_read = max(slowest_read, read_elapsed)
            if task.rate_limited:
                ux(
                    "TIMING",
                    "wait_next={:.2f}s read_calls={} slowest_read={:.2f}s".format(
                        time.perf_counter() - wait_started, read_count, slowest_read
                    ),
                )
                return "rate_limit", task
            if task.error_message:
                ux(
                    "TIMING",
                    "wait_next={:.2f}s read_calls={} slowest_read={:.2f}s".format(
                        time.perf_counter() - wait_started, read_count, slowest_read
                    ),
                )
                return "error", task
            if task.answers and task.signature != old_signature and task.submit_present:
                ux(
                    "TIMING",
                    "wait_next={:.2f}s read_calls={} slowest_read={:.2f}s".format(
                        time.perf_counter() - wait_started, read_count, slowest_read
                    ),
                )
                return "ready", task
        except RuntimeError as exc:
            message = str(exc)
            if message != last_error:
                ux("WAITING", message)
                last_error = message
        stop_requested.wait(0.2)
    return "stopped", None


def run_repeating(config: dict | None = None) -> None:
    prefetched_task = None
    while not stop_requested.is_set():
        if not wait_for_daily_schedule():
            return
        old_signature = run_once(config, prefetched_task)
        prefetched_task = None
        if old_signature == RATE_LIMIT_RETRY:
            stop_requested.wait(0.2)
            continue
        if not old_signature:
            return
        state, next_task = _wait_for_next_uia(old_signature)
        if state == "stopped" or next_task is None:
            return
        if state == "rate_limit":
            ux("NOTICE", "Orenya reported a rate limit.")
            if not pause_for_rate_limit():
                return
        elif state == "error":
            ux("ERROR", next_task.error_message)
            return
        else:
            prefetched_task = next_task


def main() -> int:
    enable_dpi_awareness()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="select the Orenya task region")
    parser.add_argument("--once", action="store_true", help="choose and submit once, then quit")
    parser.add_argument("--inspect-window", action="store_true", help="list Orenya UI Automation objects")
    args = parser.parse_args()
    if args.inspect_window:
        from orenya_window import print_objects
        try:
            print_objects()
            return 0
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 3
    if args.setup:
        setup()
        return 0
    # UI Automation discovers Orenya and its controls dynamically.
    config = {}
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
    ux("READY", "F7: start repeating | F8: answer once | F10: stop")
    while True:
        action = events.get()
        if action == "quit":
            listener.stop()
            return 0
        if action == "setup":
            listener.stop()
            setup()
            ux("NOTICE", "Restart the program to apply the legacy setup change.")
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
                ux("ERROR", str(exc))
            finally:
                busy.release()


if __name__ == "__main__":
    raise SystemExit(main())
