# Codex Command History

Saved: 2026-08-27

## PowerShell commands

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python bot.py --setup
python bot.py
python bot.py --once
```

## Current controls

- `F7`: Run one capture, OCR, and paste cycle.
- `F8`: Run one capture, OCR, and paste cycle.
- `F9`: Run setup again.
- `F10`: Quit.

## Codex changes

1. Added `F7` as a one-cycle trigger.
2. Added setup selection for Claude's status-button pixel.
3. Added setup selection for Claude's response-text region.
4. Set Claude's running-state color to `#603B2F`.
5. After the status color changes, wait 0.2 seconds, OCR Claude's response, copy it to the clipboard, and print it in the console.
6. Added protection against stray Tkinter mouse-release events during setup.

