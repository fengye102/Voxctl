@echo off
rem Build Voxctl GUI-console exe (CLAUDE.md rev 19)
rem Output: dist\voxctl\voxctl.exe (onedir, ~300MB after rev21 drops torch)
rem
rem  - Default launch opens the PyWebView native GUI console. The --voice
rem    subprocess role reuses THIS SAME exe (app.py --voice), so it still needs
rem    the full torch/whisper stack bundled below.
rem  - Copy the WHOLE dist\voxctl folder to the target machine.
rem  - First run downloads the whisper model (~500MB, needs internet).
rem  - Set ANTHROPIC_API_KEY on the target machine (env var), or fill it in the GUI.
rem  - onedir (NOT onefile): torch unpacked is huge; onefile would re-extract
rem    ~2GB to temp on every launch (1-2 min startup). onedir starts instantly.
cd /d "%~dp0"
call venv\Scripts\activate

pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [build] installing pyinstaller...
    pip install pyinstaller
)

rem Native/data deps PyInstaller's static analysis misses without --collect-*:
rem   ctranslate2 = native DLLs; onnxruntime = native DLL (runs the VAD .onnx);
rem   opencc = conversion dicts; sounddevice = PortAudio DLL;
rem   anthropic/openai = tokenizer + cacert data; providers = hidden imports.
rem   webview/clr_loader/pythonnet/proxy_tools/bottle = PyWebView native stack
rem   (Windows drives WebView2 through pythonnet/clr); clr = hidden import.
rem   main = imported at runtime by app.py's --voice branch; static analysis
rem   cannot follow it, so pull it in explicitly.
rem  (rev 21) VAD now runs silero_vad.onnx via onnxruntime+numpy, NO torch:
rem   torch/torchaudio are explicitly excluded (were ~470MB, only ever used for
rem   silero-vad); the 2.3MB onnx model ships via --add-data.
pyinstaller --noconfirm --name voxctl ^
    --windowed ^
    --collect-all ctranslate2 ^
    --collect-all faster_whisper ^
    --collect-all onnxruntime ^
    --collect-data opencc ^
    --collect-all sounddevice ^
    --collect-data anthropic ^
    --collect-data openai ^
    --collect-data certifi ^
    --collect-all webview ^
    --collect-all clr_loader ^
    --collect-all pythonnet ^
    --collect-all proxy_tools ^
    --collect-all bottle ^
    --exclude-module torch ^
    --exclude-module torchaudio ^
    --hidden-import clr ^
    --hidden-import nlu.providers.anthropic_provider ^
    --hidden-import nlu.providers.openai_provider ^
    --hidden-import _cffi_backend ^
    --hidden-import main ^
    --add-data "%~dp0audio\silero_vad.onnx;." ^
    --add-data "%~dp0webui\static\index.html;webui_static" ^
    --distpath dist --workpath build --specpath build ^
    "%~dp0app.py"

if errorlevel 1 (
    echo [build] FAILED
    exit /b 1
)

rem config.yaml is user-editable and NOT bundled; place a copy next to the exe.
copy /y config.yaml dist\voxctl\config.yaml >nul

echo.
echo [build] done: dist\voxctl\voxctl.exe
echo [build] copy the WHOLE dist\voxctl folder to the target machine.
echo [build] first run downloads the whisper model (~500MB, needs internet).
echo [build] set ANTHROPIC_API_KEY on the target machine, or fill it in the GUI.
