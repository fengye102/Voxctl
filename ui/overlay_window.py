"""状态浮窗窗口（CLAUDE.md §8.11 / 修订 26；修订 30 重做视觉方案）。

用 pywebview 建一个 frameless 置顶小窗，加载 ui/static/overlay.html，
并用 ctypes 给窗口追加 WS_EX_NOACTIVATE，保证它**不抢焦点**——用户说话时焦点
留在原应用（记事本光标不丢）。

**修订 30：圆形裁剪回归（透明方案实测不可行）+ 居中正圆裁剪 + DPI 正确定位**
    - 修订 29 断言「WebView2 transparent=True 真透明」是**误判**。实测（DPI-aware 网格
      采样 + 截图）证伪：`transparent=True` 露出的是 WinForms Form 的白色 BackColor
      （pywebview 6.2.1 winforms 后端不设 TransparencyKey，桌面透不过来），圆点四周是
      **不透明白框**；layered 窗 + LWA_COLORKEY 也无效（WebView2 内容画在子表面，
      父窗口色键刀不到）。两条透明路一律死。
    - 「椭圆黑/白框」真正根因：pywebview / WinForms **不给方窗**——请求 80×80，
      1.5× DPI 下客户区实测 180×120 物理 = **120×80 逻辑**（宽被单向撑大，`min_size=(80,80)`
      压不住 WinForms 的最小宽），于是修订 26 在这个非方矩形上 `CreateEllipticRgn(0,0,cw,ch)`
      裁出的是**椭圆**，加 60px 圆点小于窗口留一圈底色 = 用户吐槽的框。
    - 修订 30 方案：**不强求方窗**（强求不来），改在真实客户区上取 `d=min(cw,ch)` 做
      **居中正圆**区域 `CreateEllipticRgn(ox,oy,ox+d,oy+d)`——无论窗口宽高比，裁出来永远是圆；
      overlay.html 让**状态色铺满整窗**、`.dot`（承载环/波的层）固定 80×80 flex 居中，
      与裁剪圆同心 → 一枚浮在桌面上的纯色圆点，四角/两侧露桌面，无任何边框。
      透明不再需要（transparent=False）。
    - 位置固定屏幕底部居中（修订 31，任务栏上方），用 SystemParametersInfo(SPI_GETWORKAREA)
      取工作区（避开任务栏），SetWindowPos 用**真实**客户区尺寸定位；GetClientRect 拿真实尺寸建区域，不信任常量。

不抢焦点的实现要点：
    - pywebview frameless=True + on_top=True 只保证无边框/置顶，
      **不保证不 activate**（Windows 新窗口默认会抢焦点）。
    - 靠 ctypes.windll.user32 按窗口标题 FindWindowW 拿 HWND，
      GetWindowLong(GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW 回写。
      WS_EX_TOOLWINDOW 顺带让它不出现在 Alt-Tab / 任务栏。

ctypes 是 stdlib，不引新依赖（§8.11 约束）。非 Windows 平台整段跳过（NOACTIVATE /
SetWindowRgn 都是 Win32 概念），浮窗仍显示只是方角可能抢焦点——本项目本就 Windows 专属。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import webview

_LOG = logging.getLogger(__name__)

_OVERLAY_TITLE = "Voxctl 状态"
_OVERLAY_W = 80
_OVERLAY_H = 80
_MARGIN = 20     # 水平方向不用；保留兼容
_BOTTOM_GAP = 140  # 修订 31：距工作区底边（任务栏上方）留白，浮窗上移到屏幕中下区域

# WS_EX 常量（Win32）
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TOPMOST = 0x00000008
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_HWND_TOPMOST = -1
_SPI_GETWORKAREA = 0x0030


def _workarea_bottom_center(win_w: int, win_h: int) -> tuple[int, int]:
    """屏幕**底部居中**工作区坐标（水平居中、贴任务栏上方）。

    修订 31：位置从右下角改为底部居中——水平取工作区中点，垂直贴工作区底边
    （工作区 = 屏幕减任务栏，SPI_GETWORKAREA 已排除任务栏，再留 _MARGIN 间距）。
    拿不到工作区时退化到主屏尺寸，再退化到保守默认。

    与 SetWindowPos 在同一进程调用，DPI 语义一致（都吃当前进程的 DPI 感知级别），
    不跨进程混用逻辑/物理坐标，避免修订 29 那种「算到屏幕中间」的错位。
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        # 工作区 = 屏幕减去任务栏，浮窗水平居中、上移到屏幕中下区域
        if user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            x = rect.left + ((rect.right - rect.left) - win_w) // 2
            y = rect.bottom - win_h - _BOTTOM_GAP
            return x, y
        # 退化：整屏
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        return (sw - win_w) // 2, sh - win_h - _BOTTOM_GAP
    except Exception:  # noqa: BLE001
        return 700, 700


def _apply_no_activate() -> None:
    """给浮窗追加 WS_EX_NOACTIVATE + TOOLWINDOW，圆形裁剪，并移到右下角不激活。

    在窗口 shown 后调（此时 HWND / 真实客户区尺寸已稳定）。任何失败静默——
    浮窗还能显示（最多是方角、抢一次焦点），不该让 GUI 崩。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        hwnd = user32.FindWindowW(None, _OVERLAY_TITLE)
        if not hwnd:
            _LOG.warning("overlay window HWND not found by title, skip NOACTIVATE")
            return

        GetWindowLong = user32.GetWindowLongW
        SetWindowLong = user32.SetWindowLongW
        GetWindowLong.restype = ctypes.c_long
        GetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int]
        SetWindowLong.restype = ctypes.c_long
        SetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]

        ex = GetWindowLong(hwnd, _GWL_EXSTYLE)
        ex |= _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW | _WS_EX_TOPMOST
        SetWindowLong(hwnd, _GWL_EXSTYLE, ex)

        # 修订 30：圆形裁剪。用 GetClientRect 拿**真实**客户区尺寸——pywebview / WinForms
        # 会把请求的 80×80 顶成非方形（实测 1.5× DPI 下客户区 180×120 物理 = 120×80 逻辑，
        # 宽被单向撑大，min_size 压不住）。若在这个矩形上直接 CreateEllipticRgn(0,0,cw,ch)
        # 会得到**椭圆**（用户吐槽的"椭圆框"）。改法：取 d=min(cw,ch) 做**居中正圆**区域，
        # 无论窗口宽高比如何，裁出来永远是圆——方窗强求不来就不强求，圆是刀切出来的。
        # 四角/两侧多出的矩形被裁掉露桌面，无任何边框；透明方案实测不可行（见 docstring）。
        crect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(crect))
        cw = crect.right - crect.left
        ch = crect.bottom - crect.top
        if cw > 0 and ch > 0:
            d = min(cw, ch)
            ox = (cw - d) // 2          # 水平居中偏移
            oy = (ch - d) // 2          # 垂直居中偏移
            # 右下边界开区间，+1 让圆吃满直径不缺边
            rgn = gdi32.CreateEllipticRgn(ox, oy, ox + d + 1, oy + d + 1)
            user32.SetWindowRgn(hwnd, rgn, True)  # 系统接管 rgn 生命周期，不自己 DeleteObject

        # 底部居中定位（用真实窗口尺寸算），置顶不激活
        pos_x, pos_y = _workarea_bottom_center(cw, ch)
        user32.SetWindowPos(
            hwnd, wintypes.HWND(_HWND_TOPMOST), int(pos_x), int(pos_y), 0, 0,
            0x0001 | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,  # 0x0001 = SWP_NOSIZE
        )
        _LOG.info(
            "overlay NOACTIVATE + circular clip applied, hwnd=%s client=%dx%d pos=(%d,%d)",
            hwnd, cw, ch, pos_x, pos_y,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("apply NOACTIVATE/clip failed (overlay still shows): %s", exc)


def create_overlay_window(overlay_html: Path, project_root: Path, js_api=None):
    """建圆形浮窗并返回 window 句柄。裁剪 / 定位 / 不抢焦点挂在 shown 事件里。

    修订 31：位置固定屏幕底部居中（不可拖），窗口用 SetWindowRgn 裁成正圆（无边框），
    状态色由 overlay.html 铺满整窗——透明方案实测不可行，改回圆形裁剪。

    Args:
        overlay_html: ui/static/overlay.html 的绝对路径
        project_root: 项目根（保留形参以兼容调用方；位置已固定不再读写文件）
        js_api: 修订 33——暴露给 overlay.html 的桥对象（OverlayBridge，只有 toggle()），
                点击光圈时前端调 pywebview.api.toggle() → 语音启停；None 则无桥（纯展示）。
    Returns:
        pywebview window 对象（调用方持有，用于 evaluate_js 推状态）
    """
    # 初始位置先给底部居中估值；shown 后 _apply_no_activate 会用真实尺寸再精定位一次
    init_x, init_y = _workarea_bottom_center(_OVERLAY_W, _OVERLAY_H)

    window = webview.create_window(
        _OVERLAY_TITLE,
        url=overlay_html.resolve().as_uri(),
        width=_OVERLAY_W,
        height=_OVERLAY_H,
        x=init_x,
        y=init_y,
        frameless=True,
        easy_drag=False,          # 修订 29/30：位置固定，关闭拖动
        on_top=True,
        resizable=False,
        transparent=False,        # 修订 30：透明不可行，改圆形裁剪
        background_color="#000000",  # 裁剪外不可见；圆内被 overlay.html 状态色铺满
        min_size=(_OVERLAY_W, _OVERLAY_H),  # 修订 30：逼出真 80×80，否则被顶到 200×100 → 椭圆
        shadow=False,             # 圆形浮窗不要系统方形阴影
        js_api=js_api,            # 修订 33：点击 toggle 语音的桥
    )

    def _on_shown():
        # 窗口真正出现后 HWND / 客户区尺寸才稳定，这时打 NOACTIVATE + 圆裁 + 定位
        _apply_no_activate()

    try:
        window.events.shown += _on_shown
    except Exception:  # noqa: BLE001
        # 某些 pywebview 版本事件名不同，退化为创建后直接尝试一次
        _apply_no_activate()

    return window


__all__ = ["create_overlay_window"]
