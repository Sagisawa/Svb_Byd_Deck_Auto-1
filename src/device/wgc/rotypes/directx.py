"""Windows.Graphics.DirectX interfaces."""
import ctypes
from .idldsl import CtypesEnum, GUID, define_winrt_com_method
from .inspectable import IInspectable
from .foundation import IClosable
from .types import REFGUID


class DirectXPixelFormat(CtypesEnum):
    Unknown = 0
    R32G32B32A32Typeless = 1
    R8G8B8A8UIntNormalized = 28
    B8G8R8A8UIntNormalized = 87


@GUID("A37624AB-8D5F-4650-9D3E-9EAE3D9BC670")
class IDirect3DDevice(IClosable):
    pass


@GUID("0BF4A146-13C1-4694-BEE3-7ABF15EAF586")
class IDirect3DSurface(IClosable):
    pass


from .inspectable import IUnknown

@GUID("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")
class IDirect3DDxgiInterfaceAccess(IUnknown):
    pass


define_winrt_com_method(IDirect3DDxgiInterfaceAccess, "GetInterface", REFGUID, retval=IUnknown)


def CreateDirect3D11DeviceFromDXGIDevice(dxgi_device):
    d3d11_device = ctypes.windll.d3d11
    CreateDirect3D11DeviceFromDXGIDevice_fn = ctypes.windll.LoadLibrary("d3d11.dll").CreateDirect3D11DeviceFromDXGIDevice
    CreateDirect3D11DeviceFromDXGIDevice_fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(IDirect3DDevice)]
    CreateDirect3D11DeviceFromDXGIDevice_fn.restype = ctypes.c_long

    rtdevice = IDirect3DDevice()
    hr = CreateDirect3D11DeviceFromDXGIDevice_fn(dxgi_device, ctypes.byref(rtdevice))
    if hr != 0:
        raise OSError(f"CreateDirect3D11DeviceFromDXGIDevice failed: 0x{hr & 0xFFFFFFFF:08X}")
    return rtdevice
