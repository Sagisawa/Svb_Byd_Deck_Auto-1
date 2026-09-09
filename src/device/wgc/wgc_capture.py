"""WGC (Windows Graphics Capture) 屏幕截图器。

参考 ok-wuthering-waves (ok-script) 的 WGC 捕获架构设计：
- 基于 Direct3D 11 与 Windows.Graphics.Capture API
- 纯 Python ctypes 机制，零外部重量级依赖，支持 PyInstaller 完美打包
- 窗口客户区自动精准裁剪（去除模拟器/外框标题栏与阴影）
- 严格输出规范的 720p (1280x720) RGB 格式 PIL.Image
- 支持深色模式 Gamma 2.0 校正增强
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Optional

try:
    import cv2
except ImportError:
    cv2 = None

import numpy as np
from PIL import Image

from .rotypes import (
    CreateDirect3D11DeviceFromDXGIDevice,
    Direct3D11CaptureFramePool,
    DirectXPixelFormat,
    GetActivationFactory,
    IDirect3DDxgiInterfaceAccess,
    IGraphicsCaptureItem,
    IGraphicsCaptureItemInterop,
    IInspectable,
    TypedEventHandler,
    d3d11,
)
from .window_utils import (
    WGC_NO_BORDER_MIN_BUILD,
    find_target_window,
    get_client_crop_rect,
    get_windows_build_number,
    is_window_valid_and_visible,
)

logger = logging.getLogger(__name__)

PBYTE = ctypes.POINTER(ctypes.c_ubyte)


class WgcCapture:
    """Windows.Graphics.Capture 屏幕截图器。"""

    def __init__(
        self,
        target_hwnd: int = 0,
        title_keyword: Optional[str] = None,
        logger_instance: Optional[logging.Logger] = None,
    ):
        self.logger = logger_instance or logger
        self.target_hwnd = int(target_hwnd or 0)
        self.title_keyword = title_keyword

        self._lock = threading.RLock()
        self._frame_event = threading.Event()
        self._frame_requested = threading.Event()
        self._running = False
        self._handler = None
        self._frame_token = None
        # D3D11 与 WGC 资源
        self._dxdevice = None
        self._immediatedc = None
        self._rtdevice = None
        self._interop = None
        self._item = None
        self._frame_pool = None
        self._session = None
        self._cputex = None

        # 缓存与状态
        self._last_frame_bgra: Optional[np.ndarray] = None
        self._last_frame_size = None
        self._last_frame_time = 0.0
        self._current_hwnd = 0
        self._window_title = ""

        # Gamma 查找表缓存（对应原版 inv_gamma=0.5 提亮）
        self._lut_deep_color = np.clip(
            np.round(np.power(np.arange(256) / 255.0, 0.5) * 255.0),
            0,
            255,
        ).astype(np.uint8)

    @property
    def is_capturing(self) -> bool:
        return bool(self._running and self._session is not None)

    @property
    def current_hwnd(self) -> int:
        return self._current_hwnd

    @property
    def current_title(self) -> str:
        return self._window_title

    def _init_d3d_device(self) -> bool:
        """初始化 Direct3D 11 硬件设备。"""
        if self._dxdevice is not None and self._immediatedc is not None:
            return True

        try:
            self._interop = GetActivationFactory("Windows.Graphics.Capture.GraphicsCaptureItem").astype(
                IGraphicsCaptureItemInterop
            )
            self._dxdevice = d3d11.ID3D11Device()
            self._immediatedc = d3d11.ID3D11DeviceContext()

            hr = d3d11.D3D11CreateDevice(
                None,
                d3d11.D3D_DRIVER_TYPE_HARDWARE,
                None,
                d3d11.D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                None,
                0,
                d3d11.D3D11_SDK_VERSION,
                ctypes.byref(self._dxdevice),
                None,
                ctypes.byref(self._immediatedc),
            )
            if hr != 0:
                self.logger.error(f"[WGC] D3D11CreateDevice 失败: 0x{hr & 0xFFFFFFFF:08X}")
                return False

            self._rtdevice = CreateDirect3D11DeviceFromDXGIDevice(self._dxdevice)
            return True
        except Exception as e:
            self.logger.exception(f"[WGC] 初始化 D3D 设备异常: {e}")
            return False

    def _on_frame_arrived(self, pool, sender):
        """WGC 帧到达回调：按需转换，平时仅排空帧池，避免后台高频 GPU 拷贝和 COM 竞争。"""
        next_frame = None
        with self._lock:
            if not self._running or self._frame_pool is None:
                return
            try:
                next_frame = self._frame_pool.TryGetNextFrame()
                if next_frame is not None and self._frame_requested.is_set():
                    bgra = self._copy_frame_to_cpu(next_frame)
                    if bgra is not None:
                        self._last_frame_bgra = bgra
                        self._last_frame_time = time.time()
                        self._frame_requested.clear()
                        self._frame_event.set()
            except Exception as e:
                self.logger.debug(f"[WGC] 帧回调处理异常: {e}")
            finally:
                if next_frame is not None:
                    try:
                        next_frame.Release()
                    except Exception:
                        pass
    def _copy_frame_to_cpu(self, frame) -> Optional[np.ndarray]:
        """将 GPU 显存中的 Direct3D11 纹理复制到 CPU 内存。"""
        if self._dxdevice is None or self._immediatedc is None or frame is None:
            return None

        size = frame.ContentSize
        if size.Width <= 0 or size.Height <= 0:
            return None

        surface = None
        dxgi = None
        tex_unk = None
        tex = None
        mapped = False
        try:
            surface = frame.Surface
            dxgi = surface.astype(IDirect3DDxgiInterfaceAccess)
            tex_unk = dxgi.GetInterface(d3d11.ID3D11Texture2D.GUID)
            tex = tex_unk.astype(d3d11.ID3D11Texture2D)

            desc = tex.GetDesc()
            if desc.Width <= 0 or desc.Height <= 0:
                return None

            # 若尚未创建 Staging 纹理或大小改变，重新创建 CPU 可读的 Staging Texture
            if (
                self._cputex is None
                or self._last_frame_size is None
                or self._last_frame_size.Width != desc.Width
                or self._last_frame_size.Height != desc.Height
            ):
                if self._cputex is not None:
                    try:
                        self._cputex.Release()
                    except Exception:
                        pass
                    self._cputex = None

                desc.Usage = d3d11.D3D11_USAGE_STAGING
                desc.CPUAccessFlags = d3d11.D3D11_CPU_ACCESS_READ
                desc.BindFlags = 0
                desc.MiscFlags = 0
                self._cputex = self._dxdevice.CreateTexture2D(ctypes.byref(desc), None)
                self._last_frame_size = desc

            self._immediatedc.CopyResource(self._cputex, tex)
            mapinfo = self._immediatedc.Map(self._cputex, 0, d3d11.D3D11_MAP_READ, 0)
            mapped = True

            img = np.ctypeslib.as_array(
                ctypes.cast(mapinfo.pData, PBYTE),
                (desc.Height, mapinfo.RowPitch // 4, 4),
            )[:, : desc.Width].copy()
            return img
        except Exception as e:
            self.logger.debug(f"[WGC] 拷贝纹理异常: {e}")
            return None
        finally:
            if mapped and self._immediatedc is not None and self._cputex is not None:
                try:
                    self._immediatedc.Unmap(self._cputex, 0)
                except Exception:
                    pass
            if tex is not None:
                try:
                    tex.Release()
                except Exception:
                    pass

    def start(self, hwnd: Optional[int] = None) -> bool:
        """根据 HWND 启动 WGC 捕获会话。"""
        with self._lock:
            target = int(hwnd or self.target_hwnd or 0)
            if not target or not is_window_valid_and_visible(target):
                # 自动发现目标窗口
                found_hwnd, found_title = find_target_window(title_pattern=self.title_keyword)
                if not found_hwnd:
                    return False
                target = found_hwnd
                self._window_title = found_title
            else:
                from .window_utils import get_window_text
                self._window_title = get_window_text(target)

            if self.is_capturing and self._current_hwnd == target:
                return True

            self.close()

            if not self._init_d3d_device():
                return False

            try:
                self._current_hwnd = target
                item = self._interop.CreateForWindow(target, IGraphicsCaptureItem.GUID)
                self._item = item

                item_size = item.Size
                if item_size.Width <= 0 or item_size.Height <= 0:
                    self.logger.warning(f"[WGC] 目标窗口尺寸非法: {item_size.Width}x{item_size.Height}")
                    self.close()
                    return False

                self._frame_pool = Direct3D11CaptureFramePool.CreateFreeThreaded(
                    self._rtdevice,
                    DirectXPixelFormat.B8G8R8A8UIntNormalized,
                    2,
                    item_size,
                )
                self._session = self._frame_pool.CreateCaptureSession(self._item)

                # 关闭光标捕获
                try:
                    self._session.IsCursorCaptureEnabled = False
                except Exception:
                    pass

                # 在 Windows 11 Build >= 22000 上关闭黄色边框提示
                if get_windows_build_number() >= WGC_NO_BORDER_MIN_BUILD:
                    try:
                        self._session.IsBorderRequired = False
                    except Exception:
                        pass

                self._handler = TypedEventHandler(Direct3D11CaptureFramePool, IInspectable).delegate(
                    self._on_frame_arrived
                )
                self._frame_token = self._frame_pool.add_FrameArrived(self._handler)

                self._session.StartCapture()
                self._running = True
                self.logger.info(
                    f"[WGC] 成功启动捕获会话: HWND={self._current_hwnd}, 标题='{self._window_title}', 初始分辨率={item_size.Width}x{item_size.Height}"
                )
                return True
            except Exception as e:
                self.logger.error(f"[WGC] 启动捕获会话失败 (HWND={target}): {e}")
                self.close()
                return False

    def close(self):
        """释放 WGC 资源并停止捕获。"""
        with self._lock:
            self._running = False
            self._frame_event.set()

            if self._session is not None:
                try:
                    self._session.Release()
                except Exception:
                    pass
                self._session = None

            if self._frame_pool is not None:
                if self._frame_token is not None:
                    try:
                        self._frame_pool.remove_FrameArrived(self._frame_token)
                    except Exception:
                        pass
                    self._frame_token = None
                try:
                    self._frame_pool.Release()
                except Exception:
                    pass
                self._frame_pool = None
            self._handler = None

            if self._cputex is not None:
                try:
                    self._cputex.Release()
                except Exception:
                    pass
                self._cputex = None

            if self._item is not None:
                try:
                    self._item.Release()
                except Exception:
                    pass
                self._item = None

            self._last_frame_bgra = None
            self._current_hwnd = 0

    def get_screenshot(
        self,
        timeout: float = 1.0,
        deep_color: bool = False,
    ) -> Optional[Image.Image]:
        """获取当前窗口的 720p (1280x720) 截图 (RGB PIL.Image)。

        若当前未在捕获或窗口不可见，自动尝试重连启动；
        裁剪客户区消除边框和标题栏，并保持为规范 1280x720。
        """
        if not self.is_capturing or not is_window_valid_and_visible(self._current_hwnd):
            if not self.start():
                return None

        # 发起单帧抓取请求
        with self._lock:
            self._last_frame_bgra = None
            self._frame_event.clear()
            self._frame_requested.set()

        # 等待新帧到达并转换完毕（通常仅几毫秒）
        self._frame_event.wait(timeout=max(0.1, float(timeout)))

        with self._lock:
            self._frame_requested.clear()
            frame_bgra = self._last_frame_bgra

        if frame_bgra is None:
            return None

        try:
            fh, fw = frame_bgra.shape[:2]
            if fw <= 0 or fh <= 0:
                return None

            # 1. 裁剪客户区（剔除标题栏、外框、阴影）
            crop_x, crop_y, crop_w, crop_h = get_client_crop_rect(self._current_hwnd, fw, fh)
            if crop_w > 10 and crop_h > 10:
                cropped = frame_bgra[crop_y : crop_y + crop_h, crop_x : crop_x + crop_w]
            else:
                cropped = frame_bgra

            # 2. 转换为 RGB（WGC 输出为 BGRA）
            if cropped.shape[2] == 4:
                if cv2 is not None:
                    rgb = cv2.cvtColor(cropped, cv2.COLOR_BGRA2RGB)
                else:
                    rgb = cropped[:, :, [2, 1, 0]].copy()
            else:
                if cv2 is not None:
                    rgb = cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB)
                else:
                    rgb = cropped[:, :, [2, 1, 0]].copy()

            # 3. 规范化为 720p (1280x720)
            ch, cw = rgb.shape[:2]
            if cw == 1280 and ch == 720:
                final_rgb = rgb
            else:
                if cv2 is not None:
                    interpolation = cv2.INTER_AREA if (cw >= 1280 and ch >= 720) else cv2.INTER_LINEAR
                    final_rgb = cv2.resize(rgb, (1280, 720), interpolation=interpolation)
                else:
                    pil_temp = Image.fromarray(rgb).resize((1280, 720), Image.Resampling.BILINEAR)
                    final_rgb = np.array(pil_temp)

            # 4. 深色模式 Gamma 校正增强（若开启）
            if deep_color:
                if cv2 is not None:
                    final_rgb = cv2.LUT(final_rgb, self._lut_deep_color)
                else:
                    final_rgb = self._lut_deep_color[final_rgb]

            # 5. 生成标准 PIL.Image (RGB 格式，1280x720)
            return Image.fromarray(final_rgb)

        except Exception as e:
            self.logger.debug(f"[WGC] 图像处理异常: {e}")
            return None
