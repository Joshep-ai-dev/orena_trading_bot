# Orenya Local Answer Bot

This Windows bot copies selectable text from an Orenya task, ranks its A–D choices locally with
a lightweight TF-IDF/cosine text model, clicks the best match, and submits it. It does not use
OCR, a network API, or an external AI service.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Setup

Display the complete Orenya task and run:

```powershell
python bot.py --setup
```

Drag one rectangle around the task, including the query, all answer choices, and Submit button.
The region is saved in `config.json`.

## Run

```powershell
python bot.py
```

- **F7** — repeat choosing and submitting until F10.
- **F8** — choose and submit once.
- **F9** — select the Orenya region again.
- **F10** — stop.

For one cycle without the hotkey loop:

```powershell
python bot.py --once
```

The bot selects browser text by clicking near the region's top-left, Shift-clicking near its
bottom-left, and copying. Runtime logs are limited to selections, clicks, waits, retries, and
errors. After clicking Submit, the mouse moves 300 pixels left while keeping the same Y position.

<<<<<<< HEAD
Answer cards are primarily found from their wide `#050806` interiors, with `#0F1511` boundaries
as fallback. Text is parsed first so the detected card count must match the A-D answer count before
anything is clicked. Each item is clicked at its detected rectangle center. Submit is searched below the last item's center and near the
=======
Answer sections are found using item color `#080C09` and `#0F1511` boundaries; each item is clicked
at its detected rectangle center. Submit is searched below the last item's center and near the
>>>>>>> 87673944444356e10849f6bc98c5a485f1b8c5a4
area's right edge. Only its enabled bright `#F79346` state is clickable; the brown `#774E29`
disabled state is never submitted. After clicking, repeat mode waits for the bright state to
disappear, pauses five seconds for the next task, and continues.

Before an answer position is returned, its center pixel is checked for exact color `#050806`.
If necessary, the detector moves it to the nearest matching pixel within a 30-pixel radius.

Repeat mode reuses text already selected for the next-screen rate-limit check, avoiding a second
Shift-click selection of the same task.

F7 completes answer selection and submission first, then checks the next result. If that next
result contains `Rate limit exceeded`, it waits 10 minutes before starting the next complete F7
cycle. Empty selected text retries.

Automation pauses every day from 07:59:58 through 08:59:59 in GMT+9 and resumes at 09:00:00.
F10 can stop the bot during this scheduled pause.

## Desktop interface

```powershell
python gui.py
```

The interface provides Setup, Start Repeat, Run Once, Stop, and a live log.
Minimizing the window hides it to a system-tray icon. The bot and global F7/F8/F9/F10 hotkeys
continue working while the window is hidden or inactive. Use the tray menu to show or exit it.

## Build a Windows executable

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed --name OrenyaBot gui.py
```

The executable is created at `dist\OrenyaBot.exe`. Its `config.json` is stored beside the EXE.
