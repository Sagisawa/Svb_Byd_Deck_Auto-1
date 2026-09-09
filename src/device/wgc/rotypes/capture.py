"""Windows.Graphics.Capture interfaces from ok-script."""
import ctypes
from ctypes import c_bool, c_int32, c_int64
from ctypes.wintypes import HWND

from .foundation import IClosable, TypedEventHandler
from .directx import DirectXPixelFormat, IDirect3DDevice
from .idldsl import (
    GUID,
    define_winrt_com_method,
    runtimeclass,
    runtimeclass_add_statics,
)
from .inspectable import IInspectable, IUnknown
from .types import REFGUID
from .winstring import HSTRING


class SizeInt32(ctypes.Structure):
    _fields_ = [
        ("Width", c_int32),
        ("Height", c_int32),
    ]


@GUID("FA50C623-38DA-4B32-ACF3-FA9734AD800E")
class IDirect3D11CaptureFrame(IInspectable):
    pass


@GUID("24EB6D22-1975-422E-82E7-780DBD8DDF24")
class IDirect3D11CaptureFramePool(IInspectable):
    pass


@GUID("589B103F-6BBC-5DF5-A991-02E28B3B66D5")
class IDirect3D11CaptureFramePoolStatics2(IInspectable):
    pass


@GUID("79C3F95B-31F7-4EC2-A464-632EF5D30760")
class IGraphicsCaptureItem(IInspectable):
    pass


@GUID("814E42A9-F70F-4AD7-939B-FDDCC6EB880D")
class IGraphicsCaptureSession(IInspectable):
    pass


@GUID("2C39AE40-7D2E-5044-804E-8B6799D4CF9E")
class IGraphicsCaptureSession2(IInspectable):
    pass


@GUID("F2CDD966-22AE-5EA1-9596-3A289344C3BE")
class IGraphicsCaptureSession3(IInspectable):
    pass


@GUID("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356")
class IGraphicsCaptureItemInterop(IUnknown):
    pass


class Direct3D11CaptureFrame(IDirect3D11CaptureFrame, IClosable):
    pass


class Direct3D11CaptureFramePool(IDirect3D11CaptureFramePool, IClosable):
    @classmethod
    def CreateFreeThreaded(cls, device, pixel_format, number_of_buffers, size):
        from .roapi import GetActivationFactory
        factory = GetActivationFactory("Windows.Graphics.Capture.Direct3D11CaptureFramePool").astype(
            IDirect3D11CaptureFramePoolStatics2
        )
        return factory.CreateFreeThreaded(device, pixel_format, number_of_buffers, size)


class GraphicsCaptureItem(IGraphicsCaptureItem):
    pass


class GraphicsCaptureSession(
    IGraphicsCaptureSession,
    IGraphicsCaptureSession2,
    IGraphicsCaptureSession3,
    IClosable,
):
    pass


define_winrt_com_method(IDirect3D11CaptureFrame, "get_Surface", propget=IInspectable)
define_winrt_com_method(IDirect3D11CaptureFrame, "get_SystemRelativeTime", propget=c_int64)
define_winrt_com_method(IDirect3D11CaptureFrame, "get_ContentSize", propget=SizeInt32)

define_winrt_com_method(
    IDirect3D11CaptureFramePool,
    "Recreate",
    IDirect3DDevice,
    DirectXPixelFormat,
    c_int32,
    SizeInt32,
)
define_winrt_com_method(IDirect3D11CaptureFramePool, "TryGetNextFrame", retval=Direct3D11CaptureFrame)
define_winrt_com_method(
    IDirect3D11CaptureFramePool,
    "add_FrameArrived",
    ctypes.c_void_p,
    retval=c_int64,
)
define_winrt_com_method(IDirect3D11CaptureFramePool, "remove_FrameArrived", c_int64)
define_winrt_com_method(
    IDirect3D11CaptureFramePool,
    "CreateCaptureSession",
    GraphicsCaptureItem,
    retval=GraphicsCaptureSession,
)

define_winrt_com_method(
    IDirect3D11CaptureFramePoolStatics2,
    "CreateFreeThreaded",
    IDirect3DDevice,
    DirectXPixelFormat,
    c_int32,
    SizeInt32,
    retval=Direct3D11CaptureFramePool,
)

define_winrt_com_method(IGraphicsCaptureItem, "get_DisplayName", propget=HSTRING)
define_winrt_com_method(IGraphicsCaptureItem, "get_Size", propget=SizeInt32)
define_winrt_com_method(
    IGraphicsCaptureItem,
    "add_Closed",
    ctypes.c_void_p,
    retval=c_int64,
)
define_winrt_com_method(IGraphicsCaptureItem, "remove_Closed", c_int64)

define_winrt_com_method(IGraphicsCaptureSession, "StartCapture")

define_winrt_com_method(IGraphicsCaptureSession2, "get_IsCursorCaptureEnabled", propget=c_bool)
define_winrt_com_method(IGraphicsCaptureSession2, "put_IsCursorCaptureEnabled", propput=c_bool)

define_winrt_com_method(IGraphicsCaptureSession3, "get_IsBorderRequired", propget=c_bool)
define_winrt_com_method(IGraphicsCaptureSession3, "put_IsBorderRequired", propput=c_bool)

define_winrt_com_method(IGraphicsCaptureItemInterop, "CreateForWindow", HWND, REFGUID, retval=GraphicsCaptureItem)
define_winrt_com_method(IGraphicsCaptureItemInterop, "CreateForMonitor", ctypes.c_void_p, REFGUID, retval=GraphicsCaptureItem)


