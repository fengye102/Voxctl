"""voxctl.local.yaml：GUI 控制台维护的本地覆盖（CLAUDE.md 修订 19 / 23）。

定位：GUI 控制台在这里存用户改的配置，独立于 config.yaml——后者满是手写注释，
机器写入会毁注释（同 user_apps.yaml 分家逻辑，§16）。覆盖三块（修订 23 从"仅 llm"扩展）：
    - llm.{provider, model, base_url, api_key}
    - asr.model_size
    - tts.voice

流转（§2 / §6 修订 19 放宽）：
    - provider / model / base_url / asr.model_size / tts.voice：
      apply_local_overrides() 叠到 config 对应块上
    - api_key：明文存本文件（用户在 AskUserQuestion 明确选择），启动时注入到
      环境变量 config['llm']['api_key_env']，intent_parser 读取路径不变（仍从 env var 读），
      key 不进 config.yaml
    - 语音子进程无论由 GUI 拉起还是 run.bat 独立启动，都经 from_config_file →
      apply_local_overrides 自己从本文件读并叠加，所以 GUI 启停语音只需普通 Popen

安全：本文件含 api_key 明文，随 exe 文件夹走——勿分享 / 勿传 git（gitignore）。

纯机器读写文件，坏了返回空 dict 不阻断启动（仿 executor.load_user_apps）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Union

import yaml

_LOG = logging.getLogger(__name__)

# GUI 控制台维护的本地覆盖文件名（项目根，相对 cwd；frozen 下 main/app 已 chdir 到 exe 目录）
LOCAL_SETTINGS_FILE = "voxctl.local.yaml"

# 各块可直接覆盖进 config 的非敏感字段；api_key 特殊处理（注入 env，不入 config）
_LLM_OVERRIDE_FIELDS = ("provider", "model", "base_url")
_ASR_OVERRIDE_FIELDS = ("model_size",)
_TTS_OVERRIDE_FIELDS = ("voice",)


def _resolve(path: Union[str, Path, None]) -> Path:
    return Path(path) if path is not None else Path(LOCAL_SETTINGS_FILE)


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
    """
    p = _resolve(path)
    clean = {k: v for k, v in sections.items() if isinstance(v, dict) and v}
    body = yaml.safe_dump(
        clean,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    header = (
        "# Voxctl 本地覆盖（GUI 控制台维护，CLAUDE.md 修订 19 / 23）\n"
        "# 机器读写文件，手工编辑可以但注释不会保留。\n"
        "# 覆盖 config.yaml 的 llm / asr / tts 块；provider/model/base_url、\n"
        "# asr.model_size、tts.voice 叠加进配置，api_key 明文存这里，\n"
        "# 启动时注入到环境变量再供 intent_parser 读取。\n"
        "# ⚠ 本文件含 api_key 明文，随 exe 文件夹走——勿分享 / 勿传 git。\n"
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
    """把 voxctl.local.yaml 的覆盖叠到 config（llm/asr/tts 三块），api_key 注入环境变量（修订 23）。

    - llm.{provider, model, base_url}：本地文件里非空则覆盖 config['llm'] 对应字段
    - llm.api_key：本地文件里非空则 os.environ[api_key_env] = key（**覆盖**已有 env）
      —— 语义是"GUI 改的 key 说了算"：用户在 GUI 里改了 key 就该生效，即便系统里
      还留着旧的环境变量。intent_parser 读取路径不变（仍读 env var），key 不进 config。
      本地文件无 api_key 时不动 os.environ（用户仍可纯用系统环境变量，见 §2）。
    - asr.model_size / tts.voice：本地文件里非空则覆盖 config 对应块字段（修订 23）

    config 原地修改并返回。无本地文件 / 无覆盖时是 no-op。
    """
    data = load_local_settings(path)
    if not data:
        return config

    # --- llm 块（含 api_key 注入 env）---
    llm_local = data.get("llm") if isinstance(data.get("llm"), dict) else {}
    if llm_local:
        llm_cfg = config.setdefault("llm", {})
        for field in _LLM_OVERRIDE_FIELDS:
            val = llm_local.get(field)
            if val is not None and str(val).strip() != "":
                llm_cfg[field] = val
                _LOG.info("local override applied: llm.%s", field)

        api_key = llm_local.get("api_key")
        if api_key is not None and str(api_key).strip() != "":
            env_name = str(llm_cfg.get("api_key_env", "ANTHROPIC_API_KEY"))
            os.environ[env_name] = str(api_key).strip()
            _LOG.info(
                "local override applied: injected api_key into env %s (len=%d)",
                env_name, len(str(api_key).strip()),
            )

    # --- asr / tts 块（纯字段覆盖，修订 23）---
    _apply_section(config, "asr", data.get("asr"), _ASR_OVERRIDE_FIELDS)
    _apply_section(config, "tts", data.get("tts"), _TTS_OVERRIDE_FIELDS)

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
        {provider, model, base_url, api_key, api_key_env, has_local_*}
        api_key 直接返回明文（本地 GUI 用密码框展示，用户要能改）。
    """
    local = load_local_llm(path)
    merged = dict(config_llm or {})
    for field in _LLM_OVERRIDE_FIELDS:
        v = local.get(field)
        if v is not None and str(v).strip() != "":
            merged[field] = v
    api_key = local.get("api_key")
    return {
        "provider": merged.get("provider", ""),
        "model": merged.get("model", ""),
        "base_url": merged.get("base_url", ""),
        "api_key_env": merged.get("api_key_env", "ANTHROPIC_API_KEY"),
        "api_key": str(api_key).strip() if api_key else "",
        # 标记哪些字段来自本地覆盖（GUI 可给个"已覆盖"提示）
        "overridden": [f for f in _LLM_OVERRIDE_FIELDS
                       if local.get(f) is not None and str(local.get(f)).strip() != ""],
        "has_local_key": bool(api_key and str(api_key).strip()),
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
    "load_local_settings",
    "load_local_llm",
    "load_local_section",
    "save_local_llm",
    "save_local_settings",
    "apply_local_overrides",
    "apply_local_llm_overrides",
    "effective_llm_settings",
    "effective_section_settings",
    "_ASR_OVERRIDE_FIELDS",
    "_TTS_OVERRIDE_FIELDS",
]
