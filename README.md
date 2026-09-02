# Orenya Local Answer Bot

This Windows bot reads an Orenya task through Windows UI Automation, ranks its A-D choices locally
with a lightweight TF-IDF/cosine text model, selects the best match, and invokes Submit. It does
not use OCR, screenshots, colors, mouse coordinates, a network API, or an external AI service.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Run

```powershell
python bot.py
```

- **F7** - repeat choosing and submitting until F10.
- **F8** - choose and submit once.
- **F9** - legacy screen-region setup; F7/F8 do not use it.
- **F10** - stop.

For one cycle without the hotkey loop:

```powershell
python bot.py --once
```

The runtime finds the inactive Orenya HWND, reads named accessibility elements, identifies A-D
answer controls, performs their Selection/Invoke action, and invokes the enabled `Submit answer`
control. It waits for the accessibility-tree answer signature to change before processing the next
task. Orenya is not activated and the physical mouse is not moved.

When Electron does not publish the shopping query as a UIA element, the bot reads the exact
`task.query` and option text from Orenya's live Chromium HTTP cache through a shared Win32 file
handle. Cached text is accepted only when its A-D labels match the answer controls currently
exposed by UI Automation.

F7 completes answer selection and submission, then examines the next UIA result. If it contains
`Rate limit exceeded`, the bot waits 10 minutes and starts the complete F7 cycle again.

Automation pauses every day from 07:59:58 through 08:59:59 in GMT+9 and resumes at 09:00:00.
F10 can stop the bot during this scheduled pause.

## Desktop interface

```powershell
python gui.py
```

The interface provides Start Repeat, Run Once, Stop, and a live log. Minimizing it hides
it to a system-tray icon. The bot and global hotkeys continue working while its GUI is inactive.

The command-line inspector attaches to the top-level `Orenya Commerce Agent` window and prints every object
Electron publishes to Windows UI Automation, including invisible, off-screen, and zero-rectangle
nodes. The same inspection is available from:

```powershell
python bot.py --inspect-window
```

Inactive, minimized, and hidden Orenya windows can be read and automated without foreground
activation. If Orenya closes its UI and leaves only background processes, there is no HWND or accessibility tree until
it opens a window again. Controls not published by Electron's accessibility tree cannot be read or
invoked by Windows UI Automation.

## Build a Windows executable

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed --name OrenyaBot gui.py
```

The executable is created at `dist\OrenyaBot.exe`.
