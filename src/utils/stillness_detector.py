"""通用连续帧差分 / 相似度静止检测模块。

通过对连续帧进行轻量降采样、灰度化、高斯去噪与二值化差分计算，
准确判断画面动画是否结束并达到稳定静止状态，替代死板的硬编码 sleep。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from skimage.metrics import structural_similarity as ssim

logger = logging.getLogger(__name__)


def wait_for_screen_stable(
    device_state: Any,
    timeout: float = 10.0,
    threshold: float = 0.93,
    interval: float = 0.2,
    max_checks: int = 2,
    desc: str = "",
) -> bool:
    """等待设备屏幕稳定 (参考 auto_szb 实现，基于灰度图 SSIM 结构相似度检测)。

    以 0.4s 为采样间隔，避免动作刚触发未及动画展开时的早熟误判；
    采用灰度图的 SSIM 结构相似度（默认阈值 0.93），有效过滤动态背景/光效微噪；
    连续 2 次（0.8s）高于阈值判定为画面已稳定。

    :param device_state: 设备状态对象 (需提供 take_screenshot() 与 logger)
    :param timeout: 超时时间（秒），默认 10.0s
    :param threshold: 图像相似度阈值，默认 0.93
    :param interval: 截图间隔时间（秒），默认 0.4s
    :param max_checks: 连续稳定画面的次数，默认 2 次
    :param desc: 操作描述标签（用于日志前缀）
    :return: 如果屏幕稳定则返回 True，超时返回 False
    """
    log_func = getattr(device_state, "logger", logger)
    prefix = f"[{desc}] " if desc else ""
    start_time = time.time()
    last_screenshot = None
    stable_count = 0
    change_logged = False
    last_score = 0.0

    while time.time() - start_time < timeout:
        if hasattr(device_state, "check_interrupt"):
            device_state.check_interrupt()

        screenshot = device_state.take_screenshot()
        if screenshot is None:
            time.sleep(interval)
            continue

        # 将截图转换为灰度图
        if isinstance(screenshot, Image.Image):
            arr = np.array(screenshot)
            frame = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) if len(arr.shape) == 3 else arr
        elif isinstance(screenshot, np.ndarray):
            frame = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY) if len(screenshot.shape) == 3 else screenshot
        else:
            time.sleep(interval)
            continue

        if last_screenshot is not None:
            # 计算 SSIM
            try:
                score, _ = ssim(last_screenshot, frame, full=True)
            except Exception:
                score = 1.0
            last_score = float(score)

            if score > threshold:
                stable_count += 1
                change_logged = False
            else:
                if not change_logged:
                    log_func.info(f"{prefix}画面特效持续中... (稳定度: {score:.3f})")
                    change_logged = True
                stable_count = 0

            if stable_count >= max_checks:
                log_func.info(f"{prefix}画面已稳定 (稳定度: {score:.3f})")
                return True

        last_screenshot = frame
        time.sleep(interval)

    log_func.warning(f"{prefix}等待画面稳定超时 (最终稳定度: {last_score:.3f})")
    return False


def wait_for_stillness(
    device_state: Any,
    timeout: float = 10.0,
    *,
    threshold: float = 0.93,
    interval: float = 0.2,
    consecutive_stable: int = 2,
    max_checks: Optional[int] = None,
    poll_interval: Optional[float] = None,
    min_wait: float = 0.0,
    motion_timeout: float = 0.8,
    wait_for_motion: bool = False,
    pixel_threshold: int = 18,
    roi: Optional[Tuple[int, int, int, int]] = None,
    desc: str = "",
) -> bool:
    """等待画面动画结束并达到静止（映射到 wait_for_screen_stable）。

    为保持向下兼容，接受旧版参数，底层统一由 SSIM 稳定度算法驱动。
    """
    actual_interval = poll_interval if poll_interval is not None else interval
    actual_checks = max_checks if max_checks is not None else consecutive_stable
    if min_wait > 0:
        if hasattr(device_state, "sleep"):
            device_state.sleep(min_wait)
        else:
            time.sleep(min_wait)
    return wait_for_screen_stable(
        device_state,
        timeout=timeout,
        threshold=threshold,
        interval=actual_interval,
        max_checks=actual_checks,
        desc=desc,
    )
