"""Windows 原生后台输入代理 (WindowsInputProxy)。

参考 ok-wuthering-waves (ok-script) 的 PC 游戏交互架构：
- 针对 Unity/DirectX 原生端游（如 ShadowverseWB），采用 SendInput 硬件输入模拟
- 结合瞬态光标还原（Instant Cursor Restore）：点击/滑动后立即将物理鼠标瞬移回原位，不抢占、不干扰用户正常操作
- 针对安卓模拟器亦支持纯后台 PostMessage 模式
- 自动将标准 1280x720 坐标映射为当前窗口客户区的实际屏幕物理像素
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import random
import time
from typing import Any, List, Optional, Tuple

import numpy as np

from .wgc.window_utils import (
    RECT,
    get_window_text,
    is_window_valid_and_visible,
    user32,
)

logger = logging.getLogger(__name__)
try:
    user32.SetProcessDPIAware()
except Exception:
    pass


# Windows 消息与输入常量
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001
WM_ACTIVATE = 0x0006
WA_ACTIVE = 1

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_KEYUP = 0x0002
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_RETURN = 0x0D


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", _INPUTUNION),
    ]


class _TouchProxy:
    """提供与 uiautomator2.touch 兼容的触摸事件代理。"""

    def __init__(self, parent: WindowsInputProxy):
        self._parent = parent

    def down(self, x: Any, y: Any) -> None:
        self._parent._mouse_down_at(x, y)

    def move(self, x: Any, y: Any) -> None:
        self._parent._mouse_move_to(x, y)

    def up(self, x: Any = None, y: Any = None) -> None:
        self._parent._mouse_up_at(x, y)


class WindowsInputProxy:
    """Windows 原生输入代理。"""

    def __init__(
        self,
        hwnd: int = 0,
        logger_instance: Optional[logging.Logger] = None,
        mode: str = "sendinput",
    ):
        self.hwnd = int(hwnd or 0)
        self.logger = logger_instance or logger
        self.mode = str(mode or "sendinput").lower()
        self._last_pos = (640, 360)
        self.touch = _TouchProxy(self)

    @property
    def exists(self) -> bool:
        return bool(self.hwnd and user32.IsWindow(self.hwnd))

    def map_coords_to_client(self, x: Any, y: Any) -> Tuple[int, int]:
        """将 720p 规范坐标 (1280x720) 映射为窗口当前客户区像素。"""
        try:
            x_val = float(x)
            y_val = float(y)
        except Exception:
            x_val, y_val = 0.0, 0.0

        if not self.exists:
            return int(round(x_val)), int(round(y_val))

        rect = RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        cw = rect.width
        ch = rect.height

        if cw <= 0 or ch <= 0:
            return int(round(x_val)), int(round(y_val))

        scale_x = cw / 1280.0
        scale_y = ch / 720.0
        rx = int(round(x_val * scale_x))
        ry = int(round(y_val * scale_y))

        rx = max(0, min(rx, cw - 1))
        ry = max(0, min(ry, ch - 1))
        self._last_pos = (rx, ry)
        return rx, ry

    def map_coords_to_screen(self, x: Any, y: Any) -> Tuple[int, int]:
        """将 720p 规范坐标转换为全屏幕绝对像素坐标。"""
        cx, cy = self.map_coords_to_client(x, y)
        if not self.exists:
            return cx, cy

        pt = wintypes.POINT(cx, cy)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def click(self, x: Any, y: Any) -> None:
        """在指定 720p 坐标执行点击。

        针对 Unity/PC 端游优先使用硬件级 SendInput 并瞬态恢复物理光标；
        既保证 100% 触发游戏响应，又不干扰用户操作。
        """
        if not self.exists:
            return

        sx, sy = self.map_coords_to_screen(x, y)

        if self.mode == "postmessage":
            cx, cy = self.map_coords_to_client(x, y)
            lp = (int(cy) << 16) | (int(cx) & 0xFFFF)
            user32.PostMessageW(self.hwnd, WM_ACTIVATE, WA_ACTIVE, 0)
            time.sleep(0.01)
            user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, 0, lp)
            time.sleep(0.01)
            user32.PostMessageW(self.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
            time.sleep(random.uniform(0.03, 0.05))
            user32.PostMessageW(self.hwnd, WM_LBUTTONUP, 0, lp)
            return

        # ========== SendInput 瞬态光标还原模式 ==========
        # 1. 记录用户当前鼠标位置
        pt_orig = wintypes.POINT()
        has_orig = bool(user32.GetCursorPos(ctypes.byref(pt_orig)))

        try:
            # 2. 临时让游戏窗口获得输入焦点
            user32.SetForegroundWindow(self.hwnd)
            time.sleep(0.04)

            # 3. 移动到目标点击位置并等待 Unity 产生 PointerEnter 状态 (50ms，约3帧)
            user32.SetCursorPos(sx, sy)
            time.sleep(0.05)

            # 4. 触发物理级左键点击并维持充足时长 (65ms-95ms，跨越多个游戏帧，避免丢帧)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(random.uniform(0.065, 0.095))
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

            # 5. 关键：在目标位置留出 50ms 缓冲，确保 Unity 在按钮范围内完成 OnClick 触发后再恢复光标！
            time.sleep(0.05)

        finally:
            # 6. 瞬间恢复用户原本的物理光标位置（不影响用户工作）
            if has_orig:
                user32.SetCursorPos(pt_orig.x, pt_orig.y)

    def swipe(
        self,
        x1: Any,
        y1: Any,
        x2: Any,
        y2: Any,
        duration: float = 0.25,
    ) -> None:
        """执行平滑拟人化贝塞尔曲线拖拽（出牌、随从攻击等）。"""
        if not self.exists:
            return

        sx, sy = self.map_coords_to_screen(x1, y1)
        ex, ey = self.map_coords_to_screen(x2, y2)

        dur = max(0.1, min(1.2, float(duration or 0.25)))
        steps = max(20, int(dur * 80))

        # 生成平滑三次贝塞尔曲线点
        p0 = np.array([sx, sy], dtype=float)
        p3 = np.array([ex, ey], dtype=float)
        dist = np.linalg.norm(p3 - p0)

        offset = dist * 0.06
        p1 = p0 + (p3 - p0) * 0.33 + np.random.uniform(-offset, offset, 2)
        p2 = p0 + (p3 - p0) * 0.66 + np.random.uniform(-offset, offset, 2)

        t = np.linspace(0.0, 1.0, steps)
        points: List[Tuple[int, int]] = []
        for val in t:
            pt = (
                (1.0 - val) ** 3 * p0
                + 3.0 * (1.0 - val) ** 2 * val * p1
                + 3.0 * (1.0 - val) * val ** 2 * p2
                + val ** 3 * p3
            )
            points.append((int(round(pt[0])), int(round(pt[1]))))

        if self.mode == "postmessage":
            start_cx, start_cy = self.map_coords_to_client(x1, y1)
            lp_start = (int(start_cy) << 16) | (int(start_cx) & 0xFFFF)
            user32.PostMessageW(self.hwnd, WM_ACTIVATE, WA_ACTIVE, 0)
            time.sleep(0.01)
            user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, 0, lp_start)
            time.sleep(0.02)
            user32.PostMessageW(self.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp_start)
            time.sleep(0.02)
            step_sleep = max(0.005, dur / float(len(points)))
            for px, py in points[1:]:
                # 转换回客户区坐标
                cpt = wintypes.POINT(px, py)
                user32.ScreenToClient(self.hwnd, ctypes.byref(cpt))
                lp = (int(cpt.y) << 16) | (int(cpt.x) & 0xFFFF)
                user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, MK_LBUTTON, lp)
                time.sleep(step_sleep)
            end_cx, end_cy = self.map_coords_to_client(x2, y2)
            lp_end = (int(end_cy) << 16) | (int(end_cx) & 0xFFFF)
            time.sleep(0.02)
            user32.PostMessageW(self.hwnd, WM_LBUTTONUP, 0, lp_end)
            return

        # ========== SendInput 瞬态光标还原拖拽 ==========
        pt_orig = wintypes.POINT()
        has_orig = bool(user32.GetCursorPos(ctypes.byref(pt_orig)))

        try:
            user32.SetForegroundWindow(self.hwnd)
            time.sleep(0.04)

            start_x, start_y = points[0]
            user32.SetCursorPos(start_x, start_y)
            time.sleep(0.05)  # 等待卡牌捕获光标悬停
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)  # 保持按压以吸附卡牌

            step_sleep = max(0.006, dur / float(len(points)))
            for px, py in points[1:]:
                user32.SetCursorPos(px, py)
                time.sleep(step_sleep)

            # 到达目标区域后稍作停留，确保触发放置判定
            time.sleep(0.06)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.05)  # 等待放置动画生效

        finally:
            if has_orig:
                user32.SetCursorPos(pt_orig.x, pt_orig.y)

    def drag(self, x1: Any, y1: Any, x2: Any, y2: Any, duration: float = 0.25) -> None:
        """drag 别名。"""
        self.swipe(x1, y1, x2, y2, duration=duration)

    def press(self, key: str) -> None:
        """按键输入。"""
        if not self.exists:
            return

        vk = VK_ESCAPE
        k = str(key or "").lower()
        if k in ("back", "escape", "esc"):
            vk = VK_ESCAPE
        elif k in ("enter", "return"):
            vk = VK_RETURN
        elif k == "space":
            vk = VK_SPACE

        if self.mode == "postmessage":
            user32.PostMessageW(self.hwnd, 0x0100, vk, 0)
            time.sleep(0.03)
            user32.PostMessageW(self.hwnd, 0x0101, vk, 0)
        else:
            user32.SetForegroundWindow(self.hwnd)
            time.sleep(0.01)
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.03)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def app_start(self, pkg: str) -> None:
        self.logger.info(f"[Windows 原生] 目标应用保持前后台激活: {pkg}")

    def app_stop(self, pkg: str) -> None:
        self.logger.info(f"[Windows 原生] 请求停止应用: {pkg}")

    def app_current(self) -> dict:
        return {"package": "ShadowverseWB", "activity": ""}
