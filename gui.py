"""Windows desktop interface for the local Orenya answer bot."""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import bot
import orenya_window
from PIL import Image, ImageDraw
from pynput import keyboard
import pystray


class QueueWriter:
    def __init__(self, messages: queue.Queue[str]) -> None:
        self.messages = messages

    def write(self, text: str) -> int:
        if text:
            self.messages.put(text)
        return len(text)

    def flush(self) -> None:
        pass


class BotApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.messages: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.original_stdout = sys.stdout
        self.in_setup = False

        root.title("Orenya Answer Bot")
        root.geometry("760x500")
        root.minsize(620, 380)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Unmap>", self.on_unmap)

        controls = ttk.Frame(root, padding=12)
        controls.pack(fill="x")
        ttk.Button(controls, text="Setup", command=self.setup).pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="Start Repeat (F7)", command=self.start_repeat).pack(side="left", padx=8)
        ttk.Button(controls, text="Run Once (F8)", command=self.run_once).pack(side="left", padx=8)
        ttk.Button(controls, text="Stop (F10)", command=self.stop).pack(side="left", padx=8)
        ttk.Button(controls, text="Inspect Orenya", command=self.inspect_orenya).pack(side="left", padx=8)

        self.status = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status, padding=(12, 0, 12, 8)).pack(fill="x")

        log_frame = ttk.Frame(root, padding=(12, 0, 12, 12))
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, wrap="word", state="disabled", bg="#101410", fg="#e8eee9")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        sys.stdout = QueueWriter(self.messages)
        self.tray = self.create_tray_icon()
        self.tray.run_detached()
        self.hotkeys = keyboard.Listener(on_press=self.on_hotkey)
        self.hotkeys.start()
        self.root.after(100, self.drain_messages)

    def create_tray_icon(self) -> pystray.Icon:
        image = Image.new("RGB", (64, 64), "#080C09")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((7, 7, 57, 57), radius=12, fill="#F79346")
        draw.text((20, 14), "O", fill="black")

        def action(callback):
            return lambda _icon, _item: self.root.after(0, callback)

        menu = pystray.Menu(
            pystray.MenuItem("Show", action(self.show_window), default=True),
            pystray.MenuItem("Start Repeat", action(self.start_repeat)),
            pystray.MenuItem("Run Once", action(self.run_once)),
            pystray.MenuItem("Stop", action(self.stop)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", action(self.close)),
        )
        icon = pystray.Icon("orenya_bot", image, "Orenya Answer Bot", menu)
        icon.visible = False
        return icon

    def on_unmap(self, _event=None) -> None:
        if self.in_setup:
            return
        self.root.after(100, self.hide_if_minimized)

    def hide_if_minimized(self) -> None:
        if self.root.state() == "iconic":
            self.root.withdraw()
            self.tray.visible = True

    def show_window(self) -> None:
        self.tray.visible = False
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def on_hotkey(self, key) -> None:
        if key == keyboard.Key.f7:
            self.root.after(0, self.start_repeat)
        elif key == keyboard.Key.f8:
            self.root.after(0, self.run_once)
        elif key == keyboard.Key.f9:
            self.root.after(0, self.setup)
        elif key == keyboard.Key.f10:
            self.root.after(0, self.stop)

    def config(self) -> dict | None:
        try:
            return bot.load_config()
        except Exception as exc:
            messagebox.showerror("Setup required", str(exc))
            return None

    def start_worker(self, target, label: str) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Bot running", "Stop the current task before starting another.")
            return
        # F7/F8 use UI Automation and require no screen-region configuration.
        config = {}
        bot.stop_requested.clear()
        self.status.set(label)

        def work() -> None:
            try:
                target(config)
            except Exception as exc:
                bot.ux("ERROR", str(exc))
            finally:
                self.root.after(0, lambda: self.status.set("Ready"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def setup(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Bot running", "Stop the bot before running setup.")
            return
        self.in_setup = True
        self.root.withdraw()
        try:
            bot.setup(self.root)
        finally:
            self.in_setup = False
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
            self.root.update_idletasks()

    def start_repeat(self) -> None:
        self.start_worker(bot.run_repeating, "Repeating — press Stop to finish")

    def run_once(self) -> None:
        def one_with_retries(config: dict) -> None:
            while not bot.stop_requested.is_set():
                if bot.run_once(config) != bot.RATE_LIMIT_RETRY:
                    break

        self.start_worker(one_with_retries, "Running one cycle")

    def stop(self) -> None:
        bot.stop_requested.set()
        self.status.set("Stopping...")
        bot.ux("STOPPED", "Automation stopped by the user.")

    def inspect_orenya(self) -> None:
        try:
            orenya_window.print_objects()
        except Exception as exc:
            print(f"Orenya inspection failed: {exc}", flush=True)

    def drain_messages(self) -> None:
        chunks: list[str] = []
        while True:
            try:
                chunks.append(self.messages.get_nowait())
            except queue.Empty:
                break
        if chunks:
            self.log.configure(state="normal")
            self.log.insert("end", "".join(chunks))
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(100, self.drain_messages)

    def close(self) -> None:
        bot.stop_requested.set()
        self.hotkeys.stop()
        self.tray.stop()
        sys.stdout = self.original_stdout
        self.root.destroy()


def main() -> None:
    bot.enable_dpi_awareness()
    root = tk.Tk()
    BotApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
