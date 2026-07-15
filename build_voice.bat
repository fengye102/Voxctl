@echo off
rem Build Voxctl voice-assistant exe (CLAUDE.md rev 18; VAD onnx rev 21)
rem Output: dist\voxctl\voxctl.exe (onedir, ~300MB after rev21 drops torch)
rem
rem  - Copy the WHOLE dist\voxctl folder to the target machine.
rem  - First run downloads the whisper model (~500MB, needs internet).
rem  - Set ANTHROPIC_API_KEY on the target machine (env var, never in config.yaml).
rem  - onedir (NOT onefile): ctranslate2/onnxruntime DLLs are large; onefile would
rem    re-extract to temp on every launch (slow startup). onedir starts instantly.
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
rem  (rev 21) VAD runs silero_vad.onnx via onnxruntime+numpy, NO torch:
rem   torch/torchaudio explicitly excluded (~470MB, only ever for silero-vad);
rem   the 2.3MB onnx model ships via --add-data.
pyinstaller --noconfirm --name voxctl ^
    --console ^
    --collect-all ctranslate2 ^
    --collect-all faster_whisper ^
    --collect-all onnxruntime ^
    --collect-data opencc ^
    --collect-all sounddevice ^
    --collect-data anthropic ^
    --collect-data openai ^
    --collect-data certifi ^
    --exclude-module torch ^
    --exclude-module torchaudio ^
    --hidden-import nlu.providers.anthropic_provider ^
    --hidden-import nlu.providers.openai_provider ^
    --hidden-import _cffi_backend ^
    --add-data "%~dp0audio\silero_vad.onnx;." ^
    --distpath dist --workpath build --specpath build ^
    "%~dp0main.py"

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
echo [build] set ANTHROPIC_API_KEY on the target machine before running.
