"""voxctl.local.yaml：GUI 控制台维护的本地覆盖（CLAUDE.md 修订 19 / 23）。

定位：GUI 控制台在这里存用户改的配置，独立于 config.yaml——后者满是手写注释，
机器写入会毁注释（同 user_apps.yaml 分家逻辑，§16）。覆盖三块（修订 23 扩到 asr/tts，修订 46 加云端音频字段）：
    - llm.{provider, model, base_url}
    - asr.{model_size, provider, model, base_url}（model_size=本地，其余=云端 OpenAI，修订 46）
    - tts.{voice, provider, tts_voice, model, base_url}（voice=本地 edge，其余=云端 OpenAI，修订 46）

流转（§2 / §6 修订 19 放宽）：
    - 上述非敏感字段：apply_local_overrides() 叠到 config 对应块上
    - api_key：存 OS keyring（service="voxctl", username=api_key_env），启动时注入到
      环境变量——llm / asr / tts 三块各自 api_key_env 去重后逐个注入（修订 46，云端音频
      的 OPENAI_API_KEY 也经此到达语音子进程）；读取路径不变（仍从 env var 读），key 不进
      config.yaml。keyring 不可用时 fallback 到 yaml 旧值（迁移兼容）。
    - 语音子进程无论由 GUI 拉起还是 run.bat 独立启动，都经 from_config_file →
      apply_local_overrides 自己从本文件读并叠加，所以 GUI 启停语音只需普通 Popen

安全：api_key 存 OS keyring，不再以明文写入本文件。旧版 yaml 中残留的 api_key 仍可读（迁移兼容），
但新写入一律走 keyring。

纯机器读写文件，坏了返回空 dict 不阻断启动（仿 executor.load_user_apps）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Union

import yaml

# keyring for secure API key storage; graceful fallback if unavailable
try:
    import keyring as _keyring
except ImportError:
    _keyring = None  # type: ignore[assignment]

_LOG = logging.getLogger(__name__)

# GUI 控制台维护的本地覆盖文件名（项目根，相对 cwd；frozen 下 main/app 已 chdir 到 exe 目录）
LOCAL_SETTINGS_FILE = "voxctl.local.yaml"

# OS keyring service name for storing API key
KEYRING_SERVICE = "voxctl"

# 各块可直接覆盖进 config 的非敏感字段；api_key 走 keyring，不入 config / 不入 yaml
_LLM_OVERRIDE_FIELDS = ("provider", "model", "base_url")
# asr 本地字段 model_size + 云端字段 provider/model/base_url（修订 46）；两组互不干扰，各服务一种 provider
_ASR_OVERRIDE_FIELDS = ("model_size", "provider", "model", "base_url")
# tts 本地字段 voice + 云端字段 provider/tts_voice/model/base_url（修订 46）；云端音色用 tts_voice 与本地 voice 分开
_TTS_OVERRIDE_FIELDS = ("voice", "provider", "tts_voice", "model", "base_url")


def _resolve(path: Union[str, Path, None]) -> Path:
    return Path(path) if path is not None else Path(LOCAL_SETTINGS_FILE)


# ---------------------------------------------------------------------------
# API key: OS keyring storage with yaml fallback (migration)
# ---------------------------------------------------------------------------

def save_api_key(key: str, api_key_env: str = "ANTHROPIC_API_KEY") -> bool:
    """Save API key to OS keyring. Returns True on success, False if keyring unavailable."""
    key = (key or "").strip()
    if not key:
        return False
    if _keyring is None:
        _LOG.warning("keyring package not installed; cannot save API key securely")
        return False
    try:
        _keyring.set_password(KEYRING_SERVICE, api_key_env, key)
        _LOG.info("API key saved to OS keyring (service=%s, username=%s)",
                  KEYRING_SERVICE, api_key_env)
        return True
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("failed to save API key to keyring: %s", exc)
        return False


def load_api_key(
    api_key_env: str = "ANTHROPIC_API_KEY",
    path: Union[str, Path, None] = None,
) -> str:
    """Load API key: try OS keyring first, then fall back to yaml (migration compat).

    Returns the key string (stripped), or empty string if not found anywhere.
    """
    # 1. Try keyring
    if _keyring is not None:
        try:
            val = _keyring.get_password(KEYRING_SERVICE, api_key_env)
            if val and val.strip():
                return val.strip()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("failed to read API key from keyring: %s", exc)

    # 2. Fallback: read from yaml (migration from pre-keyring versions)
    llm_local = load_local_llm(path)
    yaml_key = llm_local.get("api_key")
    if yaml_key is not None and str(yaml_key).strip():
        _LOG.debug("API key loaded from yaml fallback (consider migrating to keyring)")
        return str(yaml_key).strip()

    return ""


def delete_api_key(api_key_env: str = "ANTHROPIC_API_KEY") -> bool:
    """Delete API key from OS keyring. Returns True on success."""
    if _keyring is None:
        return False
    try:
        _keyring.delete_password(KEYRING_SERVICE, api_key_env)
        return True
    except Exception:  # noqa: BLE001
        return False


def load_local_settings(path: Union[str, Path, None] = None) -> dict:
    """读取 voxctl.local.yaml。文件不存在 / 损坏 / 非映射一律返回空 dict。

    path=None 时用模块常量 LOCAL_SETTINGS_FILE（调用时解析，方便测试替换）。
    """
    p = _resolve(path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("failed to load %s, ignoring local settings: %s", p, exc)
        return {}
    if not isinstance(data, dict):
        _LOG.warning("%s is not a mapping, ignoring", p)
        return {}
    return data


def load_local_llm(path: Union[str, Path, None] = None) -> dict:
    """只取 llm 块（GUI get_settings 用）。非映射返回空 dict。"""
    return load_local_section("llm", path)


def load_local_section(section: str, path: Union[str, Path, None] = None) -> dict:
    """取本地覆盖里指定块（llm / asr / tts）。非映射返回空 dict。"""
    blk = load_local_settings(path).get(section)
    return blk if isinstance(blk, dict) else {}


def save_local_settings(
    sections: dict,
    path: Union[str, Path, None] = None,
) -> None:
    """整表写回 voxctl.local.yaml（机器写入，带头注释，修订 23）。

    sections 形如 {"llm": {...}, "asr": {...}, "tts": {...}}，只放要覆盖的键；
    调用方（GUI save_settings）负责过滤空值，这里不做业务校验，只负责落盘。
    空块（无键）会被剔除，避免写出 `asr: {}` 这种噪音。

    Safety: if sections["llm"] contains "api_key", it is stripped before writing to yaml.
    Use save_api_key() to store keys in OS keyring instead.
    """
    p = _resolve(path)
    # Strip api_key from llm section — keys must go through keyring, not yaml
    clean_sections = {}
    for k, v in sections.items():
        if not isinstance(v, dict) or not v:
            continue
        if k == "llm" and "api_key" in v:
            v = {fk: fv for fk, fv in v.items() if fk != "api_key"}
            if not v:
                continue
        clean_sections[k] = v
    body = yaml.safe_dump(
        clean_sections,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    header = (
        "# Voxctl 本地覆盖（GUI 控制台维护，CLAUDE.md 修订 19 / 23）\n"
        "# 机器读写文件，手工编辑可以但注释不会保留。\n"
        "# 覆盖 config.yaml 的 llm / asr / tts 块；provider/model/base_url、\n"
        "# asr.model_size、tts.voice 叠加进配置。\n"
        "# API key 存 OS keyring（service=voxctl），不再写入本文件。\n"
    )
    p.write_text(header + body, encoding="utf-8")


def save_local_llm(
    llm_overrides: dict,
    path: Union[str, Path, None] = None,
) -> None:
    """仅写 llm 块（保留兼容；修订 23 起内部走 save_local_settings 保留 asr/tts）。

    整表写回时若不带上现有 asr/tts 会把它们抹掉，故这里先读回现有 asr/tts 再一并写。
    """
    existing = load_local_settings(path)
    sections = {"llm": llm_overrides}
    for sec in ("asr", "tts"):
        blk = existing.get(sec)
        if isinstance(blk, dict) and blk:
            sections[sec] = blk
    save_local_settings(sections, path)


def apply_local_overrides(
    config: dict,
    path: Union[str, Path, None] = None,
) -> dict:
    """把 voxctl.local.yaml 的覆盖叠到 config（llm/asr/tts 三块），api_key 从 keyring 注入环境变量（修订 23 / 46）。

    - llm.{provider, model, base_url}：本地文件里非空则覆盖 config['llm'] 对应字段
    - asr/tts：本地文件里非空则覆盖对应块字段（本地 + 云端字段并存，修订 46）
    - api_key：对 llm / asr / tts 三块各自 `api_key_env` 去重后逐个注入（修订 46）——
      先查 OS keyring，再 fallback 到 yaml 旧值（迁移兼容）；非空则
      os.environ[api_key_env] = key（**覆盖**已有 env）。语义是"GUI 改的 key 说了算"。
      云端 ASR/TTS 的 OPENAI_API_KEY 也经此注入，否则云端引擎构造即因缺 key 抛异常。
      intent_parser / 云端引擎读取路径不变（仍读 env var），key 不进 config。

    config 原地修改并返回。无本地文件 / 无覆盖时是 no-op。
    """
    data = load_local_settings(path)

    # --- llm 块（纯字段覆盖）---
    llm_local = data.get("llm") if isinstance(data.get("llm"), dict) else {}
    llm_cfg = config.setdefault("llm", {})
    if llm_local:
        for field in _LLM_OVERRIDE_FIELDS:
            val = llm_local.get(field)
            if val is not None and str(val).strip() != "":
                llm_cfg[field] = val
                _LOG.info("local override applied: llm.%s", field)

    # --- asr / tts 块（本地 + 云端字段覆盖，修订 46）---
    _apply_section(config, "asr", data.get("asr"), _ASR_OVERRIDE_FIELDS)
    _apply_section(config, "tts", data.get("tts"), _TTS_OVERRIDE_FIELDS)

    # --- api_key 注入（llm / asr / tts 各自 api_key_env，去重，修订 46）---
    # api_key_env 保留在 config.yaml（非覆盖字段），三块可能指向不同变量名（如 llm 用
    # ANTHROPIC_API_KEY、云端音频用 OPENAI_API_KEY），也可能相同——去重后逐个注入。
    seen_envs: set = set()
    for section in ("llm", "asr", "tts"):
        env_name = str(config.get(section, {}).get("api_key_env", "") or "").strip()
        if not env_name or env_name in seen_envs:
            continue
        seen_envs.add(env_name)
        api_key = load_api_key(env_name, path)
        if api_key:
            os.environ[env_name] = api_key
            _LOG.info(
                "local override applied: injected api_key into env %s (len=%d, source=%s)",
                env_name, len(api_key),
                "keyring" if (_keyring is not None and _keyring.get_password(KEYRING_SERVICE, env_name)) else "yaml_fallback",
            )

    return config


def _apply_section(config: dict, section: str, local: object, fields: tuple) -> None:
    """把 local 块里 fields 的非空值叠到 config[section]。local 非映射时 no-op。"""
    if not isinstance(local, dict) or not local:
        return
    cfg = config.setdefault(section, {})
    for field in fields:
        val = local.get(field)
        if val is not None and str(val).strip() != "":
            cfg[field] = val
            _LOG.info("local override applied: %s.%s", section, field)


# 兼容别名：修订 23 前叫 apply_local_llm_overrides，现统一处理三块（event_loop 调用点不变）
apply_local_llm_overrides = apply_local_overrides


def effective_llm_settings(
    config_llm: dict,
    path: Union[str, Path, None] = None,
) -> dict:
    """给 GUI 展示的"生效值"：config.yaml 的 llm 块叠加本地覆盖。

    Args:
        config_llm: config.yaml 里已加载的 llm 块（GUI 从 ConsoleState 拿）
    Returns:
        {provider, model, base_url, api_key_env, has_local_key, has_local_*}
        api_key 不再返回明文——前端只需 has_local_key 判断"已设置/未设置"。
    """
    local = load_local_llm(path)
    merged = dict(config_llm or {})
    for field in _LLM_OVERRIDE_FIELDS:
        v = local.get(field)
        if v is not None and str(v).strip() != "":
            merged[field] = v
    api_key_env = merged.get("api_key_env", "ANTHROPIC_API_KEY")
    has_key = bool(load_api_key(api_key_env, path))
    return {
        "provider": merged.get("provider", ""),
        "model": merged.get("model", ""),
        "base_url": merged.get("base_url", ""),
        "api_key_env": api_key_env,
        # 标记哪些字段来自本地覆盖（GUI 可给个"已覆盖"提示）
        "overridden": [f for f in _LLM_OVERRIDE_FIELDS
                       if local.get(f) is not None and str(local.get(f)).strip() != ""],
        "has_local_key": has_key,
    }


def effective_section_settings(
    config_section: dict,
    section: str,
    fields: tuple,
    path: Union[str, Path, None] = None,
) -> dict:
    """给 GUI 展示的某块"生效值"：config.yaml 对应块叠加本地覆盖（修订 23）。

    Returns: {**merged_fields, "overridden": [被本地覆盖的字段名]}
    """
    local = load_local_section(section, path)
    merged = dict(config_section or {})
    overridden = []
    for f in fields:
        v = local.get(f)
        if v is not None and str(v).strip() != "":
            merged[f] = v
            overridden.append(f)
    result = {f: merged.get(f, "") for f in fields}
    result["overridden"] = overridden
    return result


__all__ = [
    "LOCAL_SETTINGS_FILE",
    "KEYRING_SERVICE",
    "load_local_settings",
    "load_local_llm",
    "load_local_section",
    "save_local_llm",
    "save_local_settings",
    "save_api_key",
    "load_api_key",
    "delete_api_key",
    "apply_local_overrides",
    "apply_local_llm_overrides",
    "effective_llm_settings",
    "effective_section_settings",
    "_ASR_OVERRIDE_FIELDS",
    "_TTS_OVERRIDE_FIELDS",
]
