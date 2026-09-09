"""Windows window management and client area utilities using pure ctypes.

Zero external dependencies (does not require pywin32).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import re
import sys
from typing import List, Optional, Tuple

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

# 常量
DWMWA_EXTENDED_FRAME_BOUNDS = 9
WGC_NO_BORDER_MIN_BUILD = 22000


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


class POINT(ctypes.Structure):
    _fields_ = [
        ("x", wintypes.LONG),
        ("y", wintypes.LONG),
    ]


def get_windows_build_number() -> int:
    """获取当前 Windows 系统内部版本号。"""
    try:
        version = sys.getwindowsversion()
        return int(getattr(version, "build", 0))
    except Exception:
        return 0


def get_window_text(hwnd: int) -> str:
    """获取窗口标题。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    return buff.value


def get_window_class_name(hwnd: int) -> str:
    """获取窗口类名。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return ""
    buff = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buff, 256)
    return buff.value


def is_window_valid_and_visible(hwnd: int) -> bool:
    """判断窗口是否有效、可见且未最小化。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if not user32.IsWindowVisible(hwnd):
        return False
    if user32.IsIconic(hwnd):
        # 窗口最小化
        return False
    rect = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    if rect.width <= 10 or rect.height <= 10:
        return False
    return True


def get_window_bounds(hwnd: int) -> Tuple[int, int, int, int]:
    """获取窗口在屏幕上的实际像素边界 (left, top, width, height)。

    优先尝试 DwmGetWindowAttribute 以剔除 Windows 10/11 的无形透明阴影边框。
    """
    rect = RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd,
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect),
        ctypes.sizeof(rect),
    )
    if hr == 0 and rect.width > 0 and rect.height > 0:
        return rect.left, rect.top, rect.width, rect.height

    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.width, rect.height


def get_client_crop_rect(hwnd: int, frame_w: int, frame_h: int) -> Tuple[int, int, int, int]:
    """计算客户区相对于截取到的整体窗口图像的裁剪区域 (x, y, w, h)。

    frame_w, frame_h 为 WGC 实际捕获到的图像分辨率。
    若窗口全屏或无标题栏，则返回 (0, 0, frame_w, frame_h)。
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return 0, 0, frame_w, frame_h

    # 1. 获取客户区逻辑大小
    client_rect = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client_rect))
    cw, ch = client_rect.width, client_rect.height
    if cw <= 0 or ch <= 0:
        return 0, 0, frame_w, frame_h

    # 2. 获取客户区左上角在屏幕中的坐标
    pt = POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    client_screen_x, client_screen_y = pt.x, pt.y

    # 3. 获取窗口在屏幕中的捕获边界
    # WGC (Windows.Graphics.Capture) 捕获的是 DWM 渲染边界 (DWMWA_EXTENDED_FRAME_BOUNDS)，
    # 不包含 Windows 10/11 的隐形外发光/透明边框 (GetWindowRect 会多出约 7~8px)。
    # 必须使用 DWM 边界作为相对原点和缩放基准，否则会错误地把底部 8px 裁剪掉。
    dwm_rect = RECT()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd,
        DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(dwm_rect),
        ctypes.sizeof(dwm_rect),
    )
    if hr == 0 and dwm_rect.width > 0 and dwm_rect.height > 0:
        win_left = dwm_rect.left
        win_top = dwm_rect.top
        ww, wh = dwm_rect.width, dwm_rect.height
    else:
        win_rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(win_rect))
        win_left = win_rect.left
        win_top = win_rect.top
        ww, wh = win_rect.width, win_rect.height

    if ww <= 0 or wh <= 0:
        return 0, 0, frame_w, frame_h

    # 4. 计算缩放比率（例如在多显示器 DPI 缩放下，frame_w 可能与 ww 不一致）
    scale_x = frame_w / float(ww) if ww > 0 else 1.0
    scale_y = frame_h / float(wh) if wh > 0 else 1.0

    offset_x = int(round((client_screen_x - win_left) * scale_x))
    offset_y = int(round((client_screen_y - win_top) * scale_y))
    crop_w = int(round(cw * scale_x))
    crop_h = int(round(ch * scale_y))

    # 边界保护
    offset_x = max(0, min(offset_x, frame_w - 1))
    offset_y = max(0, min(offset_y, frame_h - 1))
    if offset_x + crop_w > frame_w:
        crop_w = frame_w - offset_x
    if offset_y + crop_h > frame_h:
        crop_h = frame_h - offset_y

    return offset_x, offset_y, crop_w, crop_h


# 常见模拟器与游戏主窗口类名/标题匹配特征
COMMON_EMULATOR_PATTERNS = [
    # MuMu 模拟器 (MuMuPlayer / MuMu12)
    (r"MuMu|Nemu|nemu", r".*"),
    # 雷电模拟器 (LDPlayer)
    (r"LDPlayer|雷电|dnplayer", r".*"),
    # 逍遥模拟器
    (r"MEmu|逍遥", r".*"),
    # 夜神模拟器
    (r"Nox|夜神", r".*"),
    # 蓝叠
    (r"BlueStacks", r".*"),
    # 影之诗原生或游戏窗口
    (r".*", r"Shadowverse|影之诗|Svb|SVB"),
]


def find_target_window(
    title_pattern: Optional[str] = None,
    class_pattern: Optional[str] = None,
    target_hwnd: Optional[int] = None,
) -> Tuple[int, str]:
    """寻找最合适的游戏或模拟器渲染窗口。

    返回值: (hwnd, window_title)
    若未找到返回 (0, "")
    """
    if target_hwnd and user32.IsWindow(target_hwnd):
        if is_window_valid_and_visible(target_hwnd):
            return target_hwnd, get_window_text(target_hwnd)

    results: List[Tuple[int, str, str, int]] = []

    def enum_windows_callback(hwnd, lparam):
        if not is_window_valid_and_visible(hwnd):
            return True

        title = get_window_text(hwnd)
        cname = get_window_class_name(hwnd)
        if not title and not cname:
            return True

        rect = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        area = rect.width * rect.height

        # 优先匹配用户指定的模式
        if title_pattern or class_pattern:
            matched_title = True if not title_pattern else bool(re.search(title_pattern, title, re.IGNORECASE))
            matched_class = True if not class_pattern else bool(re.search(class_pattern, cname, re.IGNORECASE))
            if matched_title and matched_class:
                results.append((hwnd, title, cname, area))
                return True

        # 自动匹配已知模拟器或游戏
        for c_pat, t_pat in COMMON_EMULATOR_PATTERNS:
            if re.search(c_pat, cname, re.IGNORECASE) and re.search(t_pat, title, re.IGNORECASE):
                # 排除小托盘或悬浮窗
                if rect.width >= 480 and rect.height >= 270:
                    results.append((hwnd, title, cname, area))
                    return True

        # 如果标题包含明显的关键词
        if any(k in title for k in ["Shadowverse", "影之诗", "MuMu", "雷电", "LDPlayer"]):
            if rect.width >= 480 and rect.height >= 270:
                results.append((hwnd, title, cname, area))
                return True

        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)

    if not results:
        return 0, ""

    # 优先选取客户区面积最大的候选窗口（通常是主渲染窗口）
    best = max(results, key=lambda x: x[3])
    return best[0], best[1]


def list_candidate_windows() -> List[Tuple[int, str, str, bool]]:
    """列出当前桌面所有可捕获的候选应用窗口。

    返回值: [(hwnd, display_label, window_title, is_game_or_emulator), ...]
    游戏或模拟器窗口排在列表最前列。
    """
    matched: List[Tuple[int, str, str, bool]] = []
    others: List[Tuple[int, str, str, bool]] = []

    def enum_cb(hwnd, lparam):
        if not is_window_valid_and_visible(hwnd):
            return True
        title = get_window_text(hwnd).strip()
        cname = get_window_class_name(hwnd).strip()
        if not title:
            return True
        # 排除文件资源管理器、桌面和任务栏窗口
        if cname in {"CabinetWClass", "ExploreWClass", "Shell_TrayWnd", "Progman", "WorkerW"}:
            return True
        rect = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        if rect.width < 320 or rect.height < 200:
            return True

        is_svb_game = bool(
            re.search(r"^Shadowverse|影之诗", title, re.IGNORECASE)
            or cname == "UnityWndClass"
        )
        is_emulator = False
        if not is_svb_game:
            for c_pat, t_pat in COMMON_EMULATOR_PATTERNS:
                if re.search(c_pat, cname, re.IGNORECASE) and re.search(t_pat, title, re.IGNORECASE):
                    is_emulator = True
                    break
            if not is_emulator and any(k in title for k in ["MuMu", "雷电", "LDPlayer", "Nox", "MEmu", "BlueStacks"]):
                is_emulator = True
        label = f"{title}  ({rect.width}x{rect.height})"
        if is_svb_game:
            # 游戏窗口排最首位
            matched.insert(0, (hwnd, f"★ [游戏] {label}", title, True))
        elif is_emulator:
            matched.append((hwnd, f"★ [模拟器] {label}", title, True))
        else:
            others.append((hwnd, label, title, False))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

    return matched + others
