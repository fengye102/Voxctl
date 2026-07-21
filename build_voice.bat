@echo off
rem Build Voxctl voice-assistant exe (CLAUDE.md rev 18; onnx rev 21; TTS/keyring rev 34; plugins rev 42)
rem Output: dist\voxctl\voxctl.exe (onedir, ~271MB)
rem
rem  - Copy the WHOLE dist\voxctl folder to the target machine.
rem  - First run downloads the whisper model (~500MB, needs internet).
rem  - Set ANTHROPIC_API_KEY on the target machine (env var, never in config.yaml).
rem  - onedir (NOT onefile): ctranslate2/onnxruntime DLLs are large; onefile would
rem    re-extract to temp on every launch (slow startup). onedir starts instantly.
cd /d "%~dp0"
call venv\Scripts\activate

rem Write current git commit hash for frozen version info (core/version.py reads it).
for /f "delims=" %%h in ('git rev-parse --short HEAD 2^>nul') do echo %%h> core\_build_hash
if not exist core\_build_hash echo unknown> core\_build_hash

pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [build] installing pyinstaller...
    pip install pyinstaller
)

rem Native/data deps PyInstaller's static analysis misses without --collect-*:
rem   ctranslate2 = native DLLs; onnxruntime = native DLL (runs the VAD .onnx);
rem   opencc = conversion dicts; sounddevice = PortAudio DLL;
rem   av = PyAV bundles FFmpeg native DLLs (TTS streaming MP3 decode, rev 34);
rem   anthropic/openai = tokenizer + cacert data; providers = hidden imports.
rem   pyttsx3 = lazy-imported local TTS fallback (rev 34), static analysis misses it.
rem   keyring = OS keyring backend discovery via entry points (rev 34).
rem  (rev 21) VAD runs silero_vad.onnx via onnxruntime+numpy, NO torch:
rem   torch/torchaudio explicitly excluded (~470MB, only ever for silero-vad);
rem   the 2.3MB onnx model ships via --add-data.
rem  --exclude-module: insurance against large packages that may be pulled in
rem   transitively by onnxruntime/numpy/etc. None are imported by Voxctl code.
pyinstaller --noconfirm --name voxctl ^
    --console ^
    --collect-all ctranslate2 ^
    --collect-all faster_whisper ^
    --collect-all onnxruntime ^
    --collect-data opencc ^
    --collect-all sounddevice ^
    --collect-all av ^
    --collect-data anthropic ^
    --collect-data openai ^
    --collect-data certifi ^
    --collect-all keyring ^
    --exclude-module torch ^
    --exclude-module torchaudio ^
    --exclude-module scipy ^
    --exclude-module matplotlib ^
    --exclude-module pandas ^
    --exclude-module sympy ^
    --exclude-module transformers ^
    --hidden-import nlu.providers.anthropic_provider ^
    --hidden-import nlu.providers.openai_provider ^
    --hidden-import asr.providers.openai_asr ^
    --hidden-import tts.providers.openai_tts ^
    --hidden-import _cffi_backend ^
    --hidden-import pyttsx3 ^
    --hidden-import plugins ^
    --hidden-import plugins.base ^
    --add-data "%~dp0audio\silero_vad.onnx;." ^
    --add-data "%~dp0core\_build_hash;core" ^
    --distpath dist --workpath build --specpath build ^
    "%~dp0main.py"

if errorlevel 1 (
    echo [build] FAILED
    exit /b 1
)

rem config.yaml is user-editable and NOT bundled; place a copy next to the exe.
copy /y config.yaml dist\voxctl\config.yaml >nul

rem 修订 48 S4：分发版默认禁用插件（用户自行开启）。
rem 开发模式仓库版 config.yaml 的 plugins.enabled=true；分发给第三方时默认关闭，
rem 让用户显式启用更安全（AST 扫描是深度防御非绝对屏障）。
rem 正则只匹配 plugins 块紧接的注释 + enabled 行，不影响其他 enabled 字段。
powershell -NoProfile -Command "$f='dist\voxctl\config.yaml'; $c=[IO.File]::ReadAllText($f); $c = $c -replace '(?m)(^plugins:\r?\n\s*#[^\r\n]*\r?\n\s*enabled:\s*)true', '$1false'; [IO.File]::WriteAllText($f, $c)"
if errorlevel 1 (
    echo [build] WARNING: failed to disable plugins in dist config, leaving as-is
) else (
    echo [build] plugins.enabled set to false in dist config (rev 48 S4)
)

echo.
echo [build] done: dist\voxctl\voxctl.exe
echo [build] copy the WHOLE dist\voxctl folder to the target machine.
echo [build] first run downloads the whisper model (~500MB, needs internet).
echo [build] set ANTHROPIC_API_KEY on the target machine before running.
