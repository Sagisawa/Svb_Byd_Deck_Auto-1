"""WGC (Windows Graphics Capture) 极速屏幕截图模块。"""
from .wgc_capture import WgcCapture
from .window_utils import (
    find_target_window,
    is_window_valid_and_visible,
    get_window_text,
    list_candidate_windows,
)

__all__ = [
    "WgcCapture",
    "find_target_window",
    "is_window_valid_and_visible",
    "get_window_text",
    "list_candidate_windows",
]
