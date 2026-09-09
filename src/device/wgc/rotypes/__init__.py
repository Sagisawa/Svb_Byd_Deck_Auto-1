"""WGC WinRT rotypes package."""
from .types import HRESULT, GUID, REFGUID, S_OK, check_hresult
from .inspectable import IUnknown, IInspectable, IActivationFactory
from .roapi import GetActivationFactory, RoInitialize, safe_ro_initialize, RO_INIT_TYPE
from .winstring import HSTRING
from .delegate import Delegate
from .foundation import IClosable, TypedEventHandler, EventRegistrationToken
from .directx import (
    DirectXPixelFormat,
    IDirect3DDevice,
    IDirect3DSurface,
    IDirect3DDxgiInterfaceAccess,
    CreateDirect3D11DeviceFromDXGIDevice,
)
from .capture import (
    SizeInt32,
    IGraphicsCaptureItem,
    IGraphicsCaptureItemInterop,
    GraphicsCaptureItem,
    Direct3D11CaptureFrame,
    GraphicsCaptureSession,
    Direct3D11CaptureFramePool,
)
from . import d3d11
