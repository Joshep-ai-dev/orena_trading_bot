# Orenya → Claude OCR bot

This Windows bot reads the selected Orenya question/answer area and pastes the recognized text into Claude.

## Install

Open **PowerShell** in this folder and run:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
```

Run it with **Windows Python**, not Python inside WSL, because it must capture and click the Windows desktop.

If PowerShell blocks activation, skip it and replace `py` below with `.venv\Scripts\python.exe`.

## First-time setup

Arrange Orenya on the left and Claude on the right, as in the screenshot, then run:

```powershell
py bot.py --setup
```

1. Drag around the Orenya question and all answer choices (the red rectangle in image 2).
2. Click inside Claude's **Write a message** box.
3. Click the `#603B2F` part of Claude's running/submit button.
4. Drag around Claude's response text area.
5. Choose whether the bot should automatically press Enter.

The coordinates are saved in `config.json`. Run setup again after moving/resizing the windows or changing display scaling.

## Run

```powershell
py bot.py
```

- **F7** — run one capture/OCR/paste cycle
- **F8** — run one capture/OCR/paste cycle
- **F9** — select the area and Claude box again
- **F10** — quit

For a single capture/paste without the hotkey loop:

```powershell
py bot.py --once
```

Keep the Orenya content unobscured when pressing F8. The first OCR run can take several seconds while its model initializes.


Rate limit exceeded — try again later
