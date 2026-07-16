"""源码模式 GUI 窗口冒烟测试（修订 19）：开窗口→等桥就绪→检查→自动关。

验证：pywebview 原生窗口能加载真实 index.html、pywebview.api 桥注入成功、
Api 的关键方法暴露给前端、品牌元素在（index.html 未被破坏）。非交互，自动关闭。
"""
import json
import threading
import time

import webview

from webui.gui import Api, _INDEX_HTML

_result = {"done": False, "checks": None, "error": None}


def _probe(window):
    try:
        # 等 pywebviewready + 前端 init（桥在页面加载后注入）
        time.sleep(5.0)
        checks = window.evaluate_js(
            """(function(){
                var hasBridge = (typeof window.pywebview !== 'undefined') && (typeof window.pywebview.api === 'object');
                var api = hasBridge ? window.pywebview.api : {};
                var need = ['get_state','get_settings','save_settings','voice_start','voice_stop','voice_status','resolve','open_app','add_alias','delete_alias','get_log'];
                var missing = need.filter(function(m){ return typeof api[m] !== 'function'; });
                var hasBrand = !!document.querySelector('.brand h1');
                var bodyLen = document.body ? document.body.innerHTML.length : 0;
                var tabs = document.querySelectorAll('[data-tab], .tab, .tabs button, .tab-btn').length;
                return JSON.stringify({hasBridge:hasBridge, missing:missing, hasBrand:hasBrand, bodyLen:bodyLen, tabCandidates:tabs});
            })()"""
        )
        _result["checks"] = checks
    except Exception as exc:  # noqa: BLE001
        _result["error"] = repr(exc)
    finally:
        _result["done"] = True
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass


def main():
    api = Api()
    window = webview.create_window(
        "VOXCTL 控制台 (smoke)",
        url=_INDEX_HTML.resolve().as_uri(),
        js_api=api,
        width=1180, height=820, background_color="#131009",
    )
    threading.Thread(target=_probe, args=(window,), daemon=True).start()
    webview.start()
    print("SMOKE_CHECKS:", _result["checks"])
    print("SMOKE_ERROR:", _result["error"])
    if _result["checks"]:
        try:
            c = json.loads(_result["checks"])
            ok = c["hasBridge"] and not c["missing"] and c["hasBrand"] and c["bodyLen"] > 1000
            print("SMOKE_RESULT:", "OK" if ok else "FAIL", "| missing_api:", c["missing"], "| tabCandidates:", c["tabCandidates"])
        except Exception as exc:  # noqa: BLE001
            print("SMOKE_RESULT: PARSE_FAIL", exc)
    else:
        print("SMOKE_RESULT: NO_CHECKS (window may not have loaded)")


if __name__ == "__main__":
    main()
