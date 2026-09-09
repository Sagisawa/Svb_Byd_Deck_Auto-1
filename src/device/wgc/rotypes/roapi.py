"""WinRT activation and initialization."""
import ctypes
from ctypes import windll, POINTER, c_void_p, byref
from functools import lru_cache

from .types import check_hresult, REFGUID, INT, RPC_E_CHANGED_MODE
from .winstring import HSTRING

combase = windll.LoadLibrary("combase.dll")
RoGetActivationFactory = combase.RoGetActivationFactory
RoGetActivationFactory.argtypes = (HSTRING, REFGUID, c_void_p)
RoGetActivationFactory.restype = check_hresult


class RO_INIT_TYPE(INT):
    RO_INIT_SINGLETHREADED = 0
    RO_INIT_MULTITHREADED = 1


RoInitialize = combase.RoInitialize
RoInitialize.argtypes = (RO_INIT_TYPE,)
RoInitialize.restype = check_hresult


def safe_ro_initialize(init_type=RO_INIT_TYPE.RO_INIT_MULTITHREADED):
    try:
        RoInitialize(init_type)
    except OSError as e:
        if (e.winerror & 0xFFFFFFFF) == (RPC_E_CHANGED_MODE & 0xFFFFFFFF):
            pass
        else:
            raise


@lru_cache()
def GetActivationFactory(class_id: str):
    from .inspectable import IActivationFactory
    safe_ro_initialize()
    hs = HSTRING(class_id)
    factory = IActivationFactory()
    RoGetActivationFactory(hs, IActivationFactory.GUID, byref(factory))
    return factory
