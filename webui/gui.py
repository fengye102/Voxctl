"""PyWebView 原生 GUI 控制台（CLAUDE.md 修订 19）。

把 webui 界面装进一个原生窗口（无浏览器 / 无 localhost，走 pywebview js_api 桥），
作为 GUI exe 的"应用正脸"：复用 webui 的别名管理 / 解析 / 打开 / 日志，新增 LLM 配置
编辑和语音子进程启停。

安全边界（§6 修订 19）：只暴露非破坏性操作——改 LLM 配置、启停语音、别名注册、
解析测试、打开应用、看日志。绝不暴露关闭应用 / 删文件 / 跑命令（仍只走语音 + 二次确认）。

语音以独立子进程运行（§4 / §16：主循环不依赖 GUI）：voice_start 拉起
`sys.executable --voice`（frozen）或 `python main.py`（源码），voice_stop 杀掉它。
子进程自己经 core.local_settings 从 voxctl.local.yaml 读 api_key 注入环境变量，
所以这里启停只需普通 Popen，不必在 spawn 时注入 env。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Union

import webview
import yaml

# PyInstaller frozen 模式：静态资源在 _MEIPASS 解包目录，项目根 = exe 所在目录
# （修订 24 从已删的 webui/server.py 内联：GUI 是这套底层的唯一消费者）
if getattr(sys, "frozen", False):
    _PROJECT_ROOT = Path(sys.executable).resolve().parent
    _STATIC_DIR = Path(getattr(sys, "_MEIPASS", ".")) / "webui_static"
    # config.yaml / voxctl.local.yaml / user_apps.yaml / logs/ 都按 exe 所在目录解析
    os.chdir(_PROJECT_ROOT)
else:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
    _STATIC_DIR = Path(__file__).resolve().parent / "static"
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))

from audio.output_mode import OutputMode, detect_output_mode
from executor import Executor, USER_APPS_FILE, load_user_apps
from executor.app_index import AppIndex, MatchStatus
from executor.whitelist import Whitelist

from core.local_settings import (
    _ASR_OVERRIDE_FIELDS,
    _TTS_OVERRIDE_FIELDS,
    effective_llm_settings,
    effective_section_settings,
    load_api_key,
    load_local_section,
    save_api_key,
    save_local_settings,
)
from core.logger import configure_root_logging
from nlu.schema import OpenAppIntent

_LOG = logging.getLogger(__name__)

_AUDIT_LOG = _PROJECT_ROOT / "logs" / "actions.log"
_INDEX_HTML = _STATIC_DIR / "index.html"
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"


# 别名路径只允许这两类目标：可执行文件和快捷方式
_ALLOWED_SUFFIXES = {".exe", ".lnk"}


def read_user_apps(path: Union[str, Path, None] = None) -> dict[str, str]:
    """读取用户别名表。path=None 时用模块常量（调用时解析，方便测试替换）。"""
    return load_user_apps(path if path is not None else USER_APPS_FILE)


def write_user_apps(apps: dict[str, str], path: Union[str, Path, None] = None) -> None:
    """整表写回 user_apps.yaml（机器写入文件，无注释保留问题）。"""
    p = Path(path) if path is not None else Path(USER_APPS_FILE)
    content = yaml.safe_dump(
        {"apps": apps}, allow_unicode=True, default_flow_style=False, sort_keys=True,
    )
    header = (
        "# Voxctl 用户别名表（GUI 控制台维护，CLAUDE.md 修订 16/24）\n"
        "# 机器读写文件，手工编辑可以但注释不会保留。\n"
        "# 主程序启动时合并进 whitelist.apps，config.yaml 同名别名优先。\n"
    )
    p.write_text(header + content, encoding="utf-8")


def validate_alias(name: str, target: str) -> Optional[str]:
    """校验别名注册请求。返回错误信息，None 表示合法。"""
    name = (name or "").strip()
    target = (target or "").strip().strip('"')
    if not name:
        return "应用名不能为空"
    if len(name) > 50:
        return "应用名过长（最多 50 字符）"
    p = Path(target)
    if not target or not p.is_absolute():
        return "路径必须是绝对路径（如 D:\\Tools\\app.exe）"
    if p.suffix.lower() not in _ALLOWED_SUFFIXES:
        return "只支持 .exe 或 .lnk 文件"
    if not p.is_file():
        return f"文件不存在：{target}"
    return None


class ConsoleState:
    """控制台共享状态：config + executor + 索引，别名变更后原子重建。"""

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        with open(self._config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        whitelist_cfg = config["whitelist"]
        executor = Executor.from_config(whitelist_cfg)

        # 解析测试用：独立持有 whitelist（含合并别名）和 index
        merged_apps = dict(whitelist_cfg.get("apps") or {})
        for k, v in read_user_apps().items():
            merged_apps.setdefault(k, v)
        wl = Whitelist.from_config({**whitelist_cfg, "apps": merged_apps})
        index: Optional[AppIndex] = None
        if bool(whitelist_cfg.get("allow_open_any_app", True)):
            try:
                index = AppIndex.build()
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("AppIndex build failed: %s", exc)

        with self._lock:
            self._config = config
            self.executor = executor
            self.whitelist = wl
            self.index = index
            self.config_apps = dict(whitelist_cfg.get("apps") or {})

    def resolve(self, query: str) -> dict:
        """解析测试：白名单 → 索引，返回给前端的结构化结果。"""
        query = (query or "").strip()
        if not query:
            return {"status": "not_found", "query": query}
        wl_path = self.whitelist.get_app_path(query)
        if wl_path is not None:
            return {
                "status": "found", "query": query, "resolved_via": "whitelist",
                "display_name": query, "target": str(wl_path),
            }
        if self.index is not None:
            m = self.index.resolve(query)
            if m.status == MatchStatus.FOUND:
                return {
                    "status": "found", "query": query, "resolved_via": "index",
                    "display_name": m.entry.display_name, "target": str(m.entry.target),
                }
            if m.status == MatchStatus.AMBIGUOUS:
                return {"status": "ambiguous", "query": query,
                        "candidates": list(m.candidates)}
        return {"status": "not_found", "query": query}


# Windows：隐藏语音子进程的控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 语音子进程崩溃原因回读来源（修订 20）：
#   _VOICE_LOG     —— 子进程自身 logger 写的 init 崩溃 traceback（含 RuntimeError 行）
#   _VOICE_OUT_LOG —— Popen 重定向的 stdout/stderr，兜住连自身 logging 都没配好的早期崩溃
_VOICE_LOG = _PROJECT_ROOT / "logs" / "voxctl.log"
_VOICE_OUT_LOG = _PROJECT_ROOT / "logs" / "voxctl-voice.out"


def _voice_command() -> list:
    """语音子进程启动命令。frozen → 自身 exe --voice；源码 → python main.py。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--voice"]
    return [sys.executable, str(_PROJECT_ROOT / "main.py")]


def _load_config_section(section: str) -> dict:
    """读 config.yaml 的指定块（llm / asr / tts，给 get_settings 展示生效值）。失败返回空 dict。"""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        blk = cfg.get(section)
        return blk if isinstance(blk, dict) else {}
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("failed to read config.yaml %s block: %s", section, exc)
        return {}


def _load_config_llm() -> dict:
    """读 config.yaml 的 llm 块（给 get_settings 展示生效值）。失败返回空 dict。"""
    return _load_config_section("llm")




# 全局热键默认值（修订 33）：config.yaml ui 块缺字段时兜底
_DEFAULT_HOTKEY = "ctrl+alt+v"


def _effective_hotkey() -> dict:
    """热键生效设置：config.yaml 的 ui 块叠加 voxctl.local.yaml 的 ui 覆盖（修订 33）。

    热键是 GUI 侧生命周期配置（不进语音子进程的 config 覆盖链），故这里直接读、
    本地覆盖优先。返回 {hotkey, enabled, overridden}。任何读取失败退回默认值 + 启用。
    """
    hotkey = _DEFAULT_HOTKEY
    enabled = True
    overridden = False
    try:
        blk = _load_config_section("ui")
        if isinstance(blk.get("hotkey"), str) and blk["hotkey"].strip():
            hotkey = blk["hotkey"].strip()
        if "hotkey_enabled" in blk:
            enabled = bool(blk["hotkey_enabled"])
    except Exception:  # noqa: BLE001
        pass
    try:
        local = load_local_section("ui")
        if isinstance(local.get("hotkey"), str) and local["hotkey"].strip():
            hotkey = local["hotkey"].strip()
            overridden = True
        if "hotkey_enabled" in local:
            enabled = bool(local["hotkey_enabled"])
            overridden = True
    except Exception:  # noqa: BLE001
        pass
    return {"hotkey": hotkey, "enabled": enabled, "overridden": overridden}


class Api:
    """pywebview js_api：前端用 window.pywebview.api.<method>() 调用。

    pywebview 在工作线程上调用这些方法，故对共享的语音子进程句柄加锁。
    只暴露非破坏性方法（§6 修订 19）；下划线开头的不会被 pywebview 暴露给前端。
    """

    def __init__(self) -> None:
        # ConsoleState 构造会扫描开始菜单建 AppIndex（约数秒），延迟到首次访问，
        # 让窗口先秒开、前端显示"载入中…"，不阻塞 GUI 启动。
        self._state_obj: Optional[ConsoleState] = None
        self._state_lock = threading.Lock()
        self._voice_proc: Optional[subprocess.Popen] = None
        self._voice_lock = threading.Lock()
        self._voice_user_stopped = False   # 区分"用户主动停"与"意外崩溃退出"（修订 20）
        self._voice_log_fh = None          # 子进程 stdout/stderr 重定向的文件句柄
        self._toggle_lock = threading.Lock()  # 点击/热键 toggle 串行化（修订 33）

    @property
    def _state(self) -> ConsoleState:
        if self._state_obj is None:
            with self._state_lock:
                if self._state_obj is None:
                    _LOG.info("building ConsoleState (scanning app index)...")
                    self._state_obj = ConsoleState(_CONFIG_PATH)
        return self._state_obj

    # ---- 别名 / 解析 / 打开 / 日志（复用 webui 逻辑）--------------------
    def get_version(self) -> dict:
        """返回版本信息供前端显示。"""
        from core.version import __version__, get_commit_hash
        return {"version": __version__, "commit": get_commit_hash()}

    def get_state(self) -> dict:
        st = self._state
        return {
            "config_apps": {k: str(v) for k, v in st.config_apps.items()},
            "user_apps": read_user_apps(),
            "index_count": len(st.index) if st.index is not None else 0,
            "index_enabled": st.index is not None,
        }

    def get_log(self, n: int = 30) -> dict:
        try:
            n = min(200, max(1, int(n)))
        except (ValueError, TypeError):
            n = 30
        entries: list = []
        if _AUDIT_LOG.is_file():
            # 审计日志可能长期增长；只从文件末尾读够 n 行，不把整份日志载入内存。
            with _AUDIT_LOG.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                pos = stream.tell()
                blocks: list[bytes] = []
                newline_count = 0
                while pos > 0 and newline_count <= n:
                    start = max(0, pos - 8192)
                    stream.seek(start)
                    block = stream.read(pos - start)
                    blocks.append(block)
                    newline_count += block.count(b"\n")
                    pos = start
            for line in b"".join(reversed(blocks)).splitlines()[-n:]:
                try:
                    entries.append(json.loads(line.decode("utf-8", errors="replace")))
                except json.JSONDecodeError:
                    continue
        entries.reverse()  # 最新在前
        return {"entries": entries}

    def add_alias(self, name: str, path: str) -> dict:
        name = (name or "").strip()
        target = (path or "").strip().strip('"')
        err = validate_alias(name, target)
        if err:
            return {"ok": False, "error": err}
        if name in self._state.config_apps:
            return {"ok": False,
                    "error": f"{name!r} 已在 config.yaml 里定义，config 优先，请换个名字"}
        apps = read_user_apps()
        apps[name] = target
        write_user_apps(apps)
        self._state.reload()
        _LOG.info("[gui] alias added: %s -> %s", name, target)
        return {"ok": True, "name": name, "path": target}

    def delete_alias(self, name: str) -> dict:
        name = (name or "").strip()
        apps = read_user_apps()
        if name not in apps:
            return {"ok": False,
                    "error": f"别名 {name!r} 不存在（config.yaml 里的别名不能在这里删）"}
        del apps[name]
        write_user_apps(apps)
        self._state.reload()
        _LOG.info("[gui] alias deleted: %s", name)
        return {"ok": True, "name": name}

    def resolve(self, query: str) -> dict:
        return self._state.resolve(query or "")

    def open_app(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "应用名不能为空"}
        result = self._state.executor.execute(
            OpenAppIntent(app_name=name), raw_text=f"[gui] 打开 {name}",
        )
        return {"ok": result.success, "error": result.error, "payload": result.payload}

    # ---- 输出模式 / 打断可用性诚实提示（修订 52，迭代 C-A）----------------------
    def get_audio_mode(self) -> dict:
        """返回当前有效输出模式 + 打断是否可用，供 GUI 诚实提示。

        逻辑与语音主循环一致（core/event_loop.py 启动横幅）：
        - 输出模式：读 config.audio.output_mode，auto 则复用 detect_output_mode()
          自动检测（失败保守默认 SPEAKER，见 CLAUDE.md §8.5）
        - 打断可用性：耳机模式始终可打断；外放模式仅当 vad.speaker_interrupt_enabled
          为真才可打断（外放 TTS 泄漏使阈值方案无效，默认关，见 §8.4）

        纯展示：不含 api_key / 绝对路径 / 破坏性入口。
        """
        audio_cfg = _load_config_section("audio")
        vad_cfg = _load_config_section("vad")
        override = str(audio_cfg.get("output_mode", "auto") or "auto").strip().lower()
        if override not in ("auto", "headphone", "speaker"):
            override = "auto"
        speaker_interrupt = bool(vad_cfg.get("speaker_interrupt_enabled", False))

        try:
            mode = detect_output_mode(override)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("get_audio_mode: detect failed (%s), defaulting to SPEAKER", exc)
            mode = OutputMode.SPEAKER

        is_headphone = mode == OutputMode.HEADPHONE
        interrupt_available = is_headphone or speaker_interrupt
        mode_cn = "耳机" if is_headphone else "外放"
        if is_headphone:
            hint = "当前耳机模式，播报期间可以随时开口打断。"
        elif speaker_interrupt:
            hint = "当前外放模式，已手动开启打断（可能误触发）。追求稳定全双工请插耳机。"
        else:
            hint = "当前外放模式，播报期间无法打断（请等播完）。要边播边打断，请插耳机。"

        return {
            "mode": mode.value,
            "mode_cn": mode_cn,
            "interrupt_available": interrupt_available,
            "speaker_interrupt_enabled": speaker_interrupt,
            "auto_detected": override == "auto",
            "hint": hint,
        }

    # ---- 设置（LLM + ASR + TTS，写 voxctl.local.yaml，修订 19 / 23 / 46）--------
    def get_settings(self) -> dict:
        eff = effective_llm_settings(_load_config_llm())
        asr_cfg = _load_config_section("asr")
        tts_cfg = _load_config_section("tts")
        asr = effective_section_settings(asr_cfg, "asr", _ASR_OVERRIDE_FIELDS)
        tts = effective_section_settings(tts_cfg, "tts", _TTS_OVERRIDE_FIELDS)
        hk = _effective_hotkey()
        # 云端音频各自 api_key_env（config.yaml 里，非覆盖字段）；判断 keyring 是否已存 key
        asr_key_env = str(asr_cfg.get("api_key_env", "OPENAI_API_KEY"))
        tts_key_env = str(tts_cfg.get("api_key_env", "OPENAI_API_KEY"))
        # 不回传 api_key 明文；前端密码框始终留空，靠 has_local_key 提示"已设置"
        return {
            "provider": eff["provider"],
            "model": eff["model"],
            "base_url": eff["base_url"],
            "api_key_env": eff["api_key_env"],
            "has_local_key": eff["has_local_key"],
            "overridden": eff["overridden"],
            # ASR 生效值：本地 model_size + 云端 provider/model/base_url（修订 46）
            "asr_model": asr["model_size"],
            "asr_provider": asr["provider"] or "local",
            "asr_cloud_model": asr["model"],
            "asr_base_url": asr["base_url"],
            "asr_key_env": asr_key_env,
            "asr_has_key": bool(load_api_key(asr_key_env)),
            "asr_overridden": asr["overridden"],
            # TTS 生效值：本地 voice + 云端 provider/tts_voice/model/base_url（修订 46）
            "tts_voice": tts["voice"],
            "tts_provider": tts["provider"] or "edge",
            "tts_cloud_voice": tts["tts_voice"],
            "tts_cloud_model": tts["model"],
            "tts_base_url": tts["base_url"],
            "tts_key_env": tts_key_env,
            "tts_has_key": bool(load_api_key(tts_key_env)),
            "tts_overridden": tts["overridden"],
            # 全局热键（修订 33）：改热键需重启 GUI 才生效（监听器 GUI 启动时注册）
            "hotkey": hk["hotkey"],
            "hotkey_enabled": hk["enabled"],
            "hotkey_overridden": hk["overridden"],
            "voice_running": self._is_voice_running(),
        }

    def save_settings(self, provider: str = "", model: str = "",
                      base_url: str = "", api_key: str = "",
                      asr_model: str = "", tts_voice: str = "",
                      hotkey: str = "", hotkey_enabled: Optional[bool] = None,
                      asr_provider: str = "", asr_cloud_model: str = "",
                      asr_base_url: str = "", asr_api_key: str = "",
                      tts_provider: str = "", tts_cloud_voice: str = "",
                      tts_cloud_model: str = "", tts_base_url: str = "",
                      tts_api_key: str = "") -> dict:
        provider = (provider or "").strip()
        model = (model or "").strip()
        base_url = (base_url or "").strip()
        api_key = (api_key or "").strip()
        asr_model = (asr_model or "").strip()
        tts_voice = (tts_voice or "").strip()
        hotkey = (hotkey or "").strip()
        # 云端 ASR / TTS 字段（修订 46）
        asr_provider = (asr_provider or "").strip()
        asr_cloud_model = (asr_cloud_model or "").strip()
        asr_base_url = (asr_base_url or "").strip()
        asr_api_key = (asr_api_key or "").strip()
        tts_provider = (tts_provider or "").strip()
        tts_cloud_voice = (tts_cloud_voice or "").strip()
        tts_cloud_model = (tts_cloud_model or "").strip()
        tts_base_url = (tts_base_url or "").strip()
        tts_api_key = (tts_api_key or "").strip()

        if provider and provider not in ("anthropic", "openai"):
            return {"ok": False, "error": "provider 只能是 anthropic 或 openai"}
        if asr_provider and asr_provider not in ("local", "openai"):
            return {"ok": False, "error": "ASR provider 只能是 local 或 openai"}
        if tts_provider and tts_provider not in ("edge", "openai"):
            return {"ok": False, "error": "TTS provider 只能是 edge 或 openai"}

        # 热键改动先校验格式（修订 33），非法直接拒绝，不写坏值进本地文件
        if hotkey:
            try:
                from ui.hotkey import parse_hotkey
                parse_hotkey(hotkey)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"热键格式非法：{exc}"}

        # 空参数表示“不改该字段”，热键单独保存不能抹掉既有 LLM 覆盖。
        llm: dict = dict(load_local_section("llm"))
        if provider:
            llm["provider"] = provider
        if model:
            llm["model"] = model
        if base_url:
            llm["base_url"] = base_url
        # api_key：非空则存入 OS keyring（不再写 yaml 明文）
        if api_key:
            env_name = self._state._config.get("llm", {}).get("api_key_env", "ANTHROPIC_API_KEY")
            if not save_api_key(api_key, env_name):
                return {"ok": False, "error": "API key 保存到系统密钥管理器失败，请检查 keyring 是否可用"}

        # ASR 覆盖（修订 23/46）：非空才写；空 = 保留现有本地覆盖，不清空
        asr = dict(load_local_section("asr"))
        if asr_model:
            asr["model_size"] = asr_model
        if asr_provider:
            asr["provider"] = asr_provider
        if asr_cloud_model:
            asr["model"] = asr_cloud_model
        if asr_base_url:
            asr["base_url"] = asr_base_url
        # 云端 ASR api_key 存 keyring（键名 = asr.api_key_env，默认 OPENAI_API_KEY）
        if asr_api_key:
            asr_env = str(self._state._config.get("asr", {}).get("api_key_env", "OPENAI_API_KEY"))
            if not save_api_key(asr_api_key, asr_env):
                return {"ok": False, "error": "云端 ASR API key 保存失败，请检查 keyring 是否可用"}

        # TTS 覆盖（修订 23/46）
        tts = dict(load_local_section("tts"))
        if tts_voice:
            tts["voice"] = tts_voice
        if tts_provider:
            tts["provider"] = tts_provider
        if tts_cloud_voice:
            tts["tts_voice"] = tts_cloud_voice
        if tts_cloud_model:
            tts["model"] = tts_cloud_model
        if tts_base_url:
            tts["base_url"] = tts_base_url
        # 云端 TTS api_key 存 keyring（键名 = tts.api_key_env，默认 OPENAI_API_KEY）
        if tts_api_key:
            tts_env = str(self._state._config.get("tts", {}).get("api_key_env", "OPENAI_API_KEY"))
            if not save_api_key(tts_api_key, tts_env):
                return {"ok": False, "error": "云端 TTS API key 保存失败，请检查 keyring 是否可用"}

        # ui 热键覆盖（修订 33）：hotkey 非空则写；hotkey_enabled 显式传才写
        ui = dict(load_local_section("ui"))
        if hotkey:
            ui["hotkey"] = hotkey
        if hotkey_enabled is not None:
            ui["hotkey_enabled"] = bool(hotkey_enabled)

        try:
            save_local_settings({"llm": llm, "asr": asr, "tts": tts, "ui": ui})
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("save_settings failed")
            return {"ok": False, "error": f"保存失败：{exc}"}
        _LOG.info("[gui] settings saved: provider=%s model=%s base_url=%s key=%s "
                  "asr=%s asr_provider=%s tts=%s tts_provider=%s hotkey=%s",
                  provider or "-", model or "-", base_url or "-",
                  "set" if api_key else "-",
                  asr_model or "-", asr_provider or "-",
                  tts_voice or "-", tts_provider or "-", hotkey or "-")
        # 热键改了需重启 GUI 才重新注册；回传标记让前端提示用户
        return {"ok": True, "hotkey_restart_required": bool(hotkey) or hotkey_enabled is not None}

    # ---- 云端模型列表拉取（修订 57）--------------------------------------
    def fetch_models(self, section: str = "llm",
                     base_url: str = "", api_key: str = "") -> dict:
        """从 base_url/models 拉取可用模型列表（OpenAI 标准端点）。

        section: "llm" / "asr" / "tts"
        base_url: 前端表单当前值（可能未保存），空串 → 读已存配置
        api_key: 前端表单当前值，空串 → 从 keyring 读已存 key
        """
        import urllib.request
        import urllib.error

        section = (section or "llm").strip().lower()
        base_url = (base_url or "").strip()
        api_key = (api_key or "").strip()

        # 解析 base_url：前端传了用前端的，否则读已存配置
        if not base_url:
            if section == "llm":
                eff = effective_llm_settings(_load_config_llm())
                base_url = eff.get("base_url", "")
            else:
                cfg = _load_config_section(section)
                sec = effective_section_settings(
                    cfg, section,
                    _ASR_OVERRIDE_FIELDS if section == "asr" else _TTS_OVERRIDE_FIELDS,
                )
                base_url = sec.get("base_url", "")

        if not base_url:
            # 无 base_url 时按 provider 给默认端点
            if section == "llm":
                prov = effective_llm_settings(_load_config_llm()).get("provider", "")
                if prov == "openai":
                    base_url = "https://api.openai.com/v1"
                else:
                    return {"ok": False, "error": "官方 Anthropic 无需拉取模型列表；如用中转站请填写 base_url"}
            else:
                base_url = "https://api.openai.com/v1"

        # 解析 api_key：前端传了用前端的，否则从 keyring/env 读
        if not api_key:
            if section == "llm":
                env_name = effective_llm_settings(_load_config_llm())["api_key_env"]
                api_key = load_api_key(env_name, legacy_llm_env=env_name) or os.environ.get(env_name, "")
            else:
                cfg = _load_config_section(section)
                env_name = str(cfg.get("api_key_env", "OPENAI_API_KEY"))
                api_key = load_api_key(env_name) or os.environ.get(env_name, "")

        if not api_key:
            return {"ok": False, "error": "缺少 API Key，请先填写并保存"}

        url = base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"HTTP {exc.code}：{exc.reason}"}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": f"网络错误：{exc.reason}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"请求失败：{exc}"}

        raw = data.get("data", [])
        if not isinstance(raw, list):
            return {"ok": False, "error": "返回格式不符合 OpenAI /v1/models 规范"}

        models = sorted(
            [str(m.get("id", "")) for m in raw if isinstance(m, dict) and m.get("id")],
        )
        return {"ok": True, "models": models}

    # ---- 语音子进程启停（修订 20：加固失败反馈，不再静默崩溃）------------
    def voice_start(self) -> dict:
        with self._voice_lock:
            if self._voice_proc is not None and self._voice_proc.poll() is None:
                return {"ok": True, "running": True, "pid": self._voice_proc.pid}

            # 防线 1：启动前预检 API Key——本地文件或 env 都没有就别白起一个必崩的进程。
            # 这正是用户实测遇到的场景（子进程读空 ANTHROPIC_API_KEY → RuntimeError → 退出）。
            eff = effective_llm_settings(_load_config_llm())
            env_name = eff.get("api_key_env") or "ANTHROPIC_API_KEY"
            has_key = eff.get("has_local_key") or bool(os.environ.get(env_name, "").strip())
            if not has_key:
                return {"ok": False, "running": False,
                        "error": f"未配置 API Key：请在上方 api_key 填写并保存，"
                                 f"或设置系统环境变量 {env_name} 后重启本程序"}

            # 防线 2：子进程 stdout/stderr 重定向到日志，兜住连自身 logging 都没配好的早期崩溃。
            try:
                self._voice_log_fh = open(
                    _VOICE_OUT_LOG, "w", encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                self._voice_log_fh = None

            self._voice_user_stopped = False
            try:
                self._voice_proc = subprocess.Popen(
                    _voice_command(),
                    cwd=str(_PROJECT_ROOT),
                    stdout=self._voice_log_fh or subprocess.DEVNULL,
                    stderr=(subprocess.STDOUT if self._voice_log_fh
                            else subprocess.DEVNULL),
                    creationflags=_CREATE_NO_WINDOW,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("voice_start failed")
                self._close_voice_log()
                return {"ok": False, "running": False, "error": f"启动失败：{exc}"}

            # 防线 3：短等探活。源码模式启动期崩溃（缺 key 之外的异常）约 2s 内暴露，
            # 当场回读原因；frozen 导入慢、崩在窗口之后的交给 voice_status 轮询兜底（防线 4）。
            try:
                code = self._voice_proc.wait(timeout=2.5)
            except subprocess.TimeoutExpired:
                _LOG.info("voice subprocess started pid=%s", self._voice_proc.pid)
                return {"ok": True, "running": True, "pid": self._voice_proc.pid}

            reason = self._read_voice_failure_reason()
            _LOG.error("voice subprocess exited early code=%s: %s", code, reason)
            self._voice_proc = None
            self._close_voice_log()
            return {"ok": False, "running": False,
                    "error": f"语音助手启动后随即退出（code={code}）：{reason}"}

    def voice_stop(self) -> dict:
        with self._voice_lock:
            self._voice_user_stopped = True
            if self._voice_proc is None or self._voice_proc.poll() is not None:
                self._voice_proc = None
                self._close_voice_log()
                return {"ok": True, "running": False}
            try:
                self._voice_proc.terminate()
                try:
                    self._voice_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._voice_proc.kill()
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("voice_stop failed")
                return {"ok": False, "running": self._is_voice_running(),
                        "error": f"停止失败：{exc}"}
            _LOG.info("voice subprocess stopped")
            self._voice_proc = None
            self._close_voice_log()
            return {"ok": True, "running": False}

    def voice_status(self) -> dict:
        with self._voice_lock:
            proc = self._voice_proc
            if proc is None:
                return {"running": False, "pid": None}
            code = proc.poll()
            if code is None:
                return {"running": True, "pid": proc.pid}
            # 进程已退出——防线 4：区分用户主动停 vs 意外崩溃，后者回传原因让前端 toast。
            self._voice_proc = None
            self._close_voice_log()
            if self._voice_user_stopped:
                return {"running": False, "pid": None}
            reason = self._read_voice_failure_reason()
            _LOG.error("voice subprocess crashed code=%s: %s", code, reason)
            return {"running": False, "pid": None, "crashed": True,
                    "error": f"语音助手意外退出（code={code}）：{reason}"}

    def _is_voice_running(self) -> bool:
        return self._voice_proc is not None and self._voice_proc.poll() is None

    def toggle_voice(self) -> dict:
        """全局热键 / GUI 按钮的统一入口（修订 33，修订 50 起浮窗已删）：跑着就停，停着就启。

        返回 voice_start / voice_stop 的结果，附带 action 字段（"started"/"stopped"）
        供调用方（热键闭包 / GUI 按钮回调）决定是否 toast。启停本身是非破坏性生命周期操作
        （§6 修订 19/33），不需二次确认。加锁避免热键与按钮并发触发时启停打架。"""
        with self._toggle_lock:
            if self._is_voice_running():
                res = self.voice_stop()
                res["action"] = "stopped"
            else:
                res = self.voice_start()
                res["action"] = "started"
            _LOG.info("[gui] toggle_voice -> %s (ok=%s)",
                      res.get("action"), res.get("ok"))
            return res

    def _close_voice_log(self) -> None:
        fh = self._voice_log_fh
        self._voice_log_fh = None
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _read_voice_failure_reason() -> str:
        """回读语音子进程崩溃原因：扫日志尾部找最后一条异常行（修订 20）。"""
        for logf in (_VOICE_LOG, _VOICE_OUT_LOG):
            try:
                if not logf.is_file():
                    continue
                lines = logf.read_text(
                    encoding="utf-8", errors="replace").strip().splitlines()
            except Exception:  # noqa: BLE001
                continue
            if not lines:
                continue
            for line in reversed(lines[-60:]):
                s = line.strip()
                if s and any(tok in s for tok in
                             ("Error:", "Exception:", "错误", "失败", "Traceback")):
                    return s[:300]
            return lines[-1].strip()[:300]
        return "未知原因，请查看 logs/voxctl.log"




def _setup_gui_logging() -> None:
    """GUI 进程日志写 logs/voxctl-gui.log（windowed 无控制台）。

    共用逻辑已收敛到 core/logger.py（修订 25），这里只传 GUI 进程的参数。
    mkdir/handler 建失败时静默跳过——GUI windowed 下建不起日志也不能让窗口崩。
    """
    try:
        configure_root_logging(
            _PROJECT_ROOT / "logs" / "voxctl-gui.log",
            file_level=logging.INFO,
            console_level=logging.INFO,
            max_bytes=5 * 1024 * 1024,
            backup_count=2,
        )
    except Exception:  # noqa: BLE001
        return


def _apply_dark_title_bar() -> None:
    """用 DWM API 把主窗口标题栏切成暗色，与页面背景融合。

    模块级函数（不依赖 run_gui 闭包）：通过 FindWindowW 按标题查 hwnd，
    非 Windows / 找不到窗口 / DwmSetWindowAttribute 失败都静默跳过。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        hwnd = user32.FindWindowW(None, "VOXCTL 控制台")
        if not hwnd:
            return
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        val = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:  # noqa: BLE001
        pass


def run_gui() -> int:
    """启动 PyWebView 原生控制台窗口。阻塞直到窗口关闭。"""
    _setup_gui_logging()
    if not _INDEX_HTML.is_file():
        _LOG.error("index.html not found at %s", _INDEX_HTML)
        print(f"[错误] 找不到界面文件：{_INDEX_HTML}", file=sys.stderr)
        return 1

    _LOG.info("starting Voxctl GUI console; index=%s", _INDEX_HTML)
    api = Api()
    window = webview.create_window(
        "VOXCTL 控制台",
        url=_INDEX_HTML.resolve().as_uri(),
        js_api=api,
        width=1180,
        height=820,
        min_size=(920, 640),
        background_color="#131009",
    )

    hotkey_listener = None

    # 全局热键的 toggle 闭包（修订 33，修订 50 起浮窗已删）：调 api.toggle_voice()。
    def _toggle_and_sync() -> dict:
        return api.toggle_voice()

    # 全局热键 toggle（修订 33）：config ui.hotkey_enabled 为真才注册。
    # 注册失败（热键被占用 / 非 Windows）只记日志、不影响 GUI——GUI 控制台按钮仍可 toggle。
    hk = _effective_hotkey()
    if hk["enabled"]:
        try:
            from ui.hotkey import HotkeyListener
            hotkey_listener = HotkeyListener(on_trigger=_toggle_and_sync)
            err = hotkey_listener.start(hk["hotkey"])
            if err:
                _LOG.warning("global hotkey not active: %s", err)
            else:
                _LOG.info("global hotkey registered: %s", hk["hotkey"])
        except Exception:  # noqa: BLE001
            _LOG.exception("failed to start hotkey listener (non-fatal)")
            hotkey_listener = None

    # 窗口关闭时杀掉还在跑的语音子进程，避免残留；停热键
    def _on_closing():
        if hotkey_listener is not None:
            try:
                hotkey_listener.stop()
            except Exception:  # noqa: BLE001
                pass
        api.voice_stop()

    try:
        window.events.shown += _apply_dark_title_bar
        window.events.closing += _on_closing
    except Exception:  # noqa: BLE001
        pass

    try:
        webview.start()
    except Exception as exc:  # noqa: BLE001
        _LOG.exception("webview.start failed")
        print(f"[错误] GUI 启动失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if hotkey_listener is not None:
            try:
                hotkey_listener.stop()
            except Exception:  # noqa: BLE001
                pass
        api.voice_stop()  # 兜底：确保退出时不留语音子进程
    return 0


if __name__ == "__main__":
    sys.exit(run_gui())
