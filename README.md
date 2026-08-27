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

## Desktop interface

Start the Windows interface with:

```powershell
python gui.py
```

Use **Setup** first, then **Start Repeat**, **Run Once**, or **Stop**. Runtime messages appear in
the interface log panel.

## Build a Windows executable

Build from an activated Windows virtual environment:

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onefile --windowed --name OrenyaBot --collect-all rapidocr_onnxruntime --collect-all onnxruntime gui.py
```

The executable is created at `dist\OrenyaBot.exe`. Keep `config.json` beside the executable after
running Setup. Build on Windows because the bot captures and controls the Windows desktop.

- **F7** — repeat capture, answer, and submit cycles until F10
- **F8** — run one capture/OCR/paste cycle
- **F9** — select the area and Claude box again
- **F10** — quit

Each capture scans the Orenya area using background color `#050806` and prints the detected
answer-section positions. Each position uses the area's right edge minus 10 pixels and the
section's vertical center.

The primary boundary detector uses separator color `#0F1511` between adjacent answer items.

After Claude finishes, answers map to the detected Orenya sections as follows: A to the first,
B to the second, C to the third, D to the fourth, and any other text to the first. The selected
item and click coordinates are printed in the console.

The submit-button position is detected by searching near the area's right edge
minus 120 pixels for a color cluster similar to `#F79346` (with rendering tolerance). Its
detected center is printed and clicked after a valid answer item is selected.
Submit-button color searches are restricted to screen Y coordinates 450 through 900 pixels.
The detector requires a dense filled-color cluster, so a selected answer's thin orange outline is
not mistaken for the filled **Submit answer** button.

After selecting an answer, the bot clicks the detected submit button. In F7 repeat mode it waits
while the button is inactive/background `#080C09`, then rescans for the moved submit control using
color `#774E29`. When found, it prints the new position and starts the next cycle. Press F10 to stop.
After detecting the `#080C09` inactive background, it waits five seconds. If answer-item detection
fails on a later cycle, it uses the last known first-item position and submits that item.

If Claude's recognized response contains `Rate limit exceeded — try again later`, the bot does not
click Orenya. It waits 1 hour and 1 minute (3,660 seconds), then retries automatically. F10 stops
the wait.

All Claude/Orenya state waits have a 20-second no-change watchdog. A stalled state restarts the
F7 workflow. The rate-limit message takes priority and still pauses for 1 hour and 1 minute.
Empty OCR text from either Orenya or Claude also restarts the F7 workflow.
If Orenya OCR contains `Rate limit exceeded`, the bot waits 1 hour and 1 minute without sending
the text to Claude, then continues automatically.

Orenya question text is copied without OCR: the bot clicks the configured area's top-left point,
Shift-clicks its bottom-left point, copies the browser selection, and prints labeled A-D answers
in the console. Claude response reading still uses OCR.

For a single capture/paste without the hotkey loop:

```powershell
py bot.py --once
```

Keep the Orenya content unobscured when pressing F8. The first OCR run can take several seconds while its model initializes.


Rate limit exceeded — try again later
