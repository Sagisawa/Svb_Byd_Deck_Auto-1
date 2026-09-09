"""WinRT IUnknown and IInspectable base classes."""
import ctypes
from ctypes import c_void_p, WINFUNCTYPE, POINTER, byref, cast, windll
from .idldsl import define_winrt_com_method, funcwrap, _new_rtobj, GUID
from .types import check_hresult, REFGUID, VOIDPP, ULONG, GUID as _GUID
from .winstring import HSTRING

CoTaskMemFree = windll.ole32.CoTaskMemFree
CoTaskMemFree.argtypes = (c_void_p,)


@GUID("00000000-0000-0000-C000-000000000046")
class IUnknown(c_void_p):
    _method_defs = [
        (0, "QueryInterface", WINFUNCTYPE(check_hresult, REFGUID, VOIDPP)(0, "QueryInterface")),
        (1, "AddRef", WINFUNCTYPE(ULONG)(1, "AddRef")),
        (2, "Release", WINFUNCTYPE(ULONG)(2, "Release")),
    ]
    QueryInterface = funcwrap(_method_defs[0][2])
    _AddRef = funcwrap(_method_defs[1][2])
    _Release = funcwrap(_method_defs[2][2])
    _vtblend = 2

    def Release(self):
        if getattr(self, "value", None):
            try:
                self._Release()
            except Exception:
                pass
            self.value = None

    def __del__(self):
        try:
            self.Release()
        except Exception:
            pass

    def astype(self, interface_type):
        iid = interface_type.GUID
        obj = _new_rtobj(interface_type)
        self.QueryInterface(byref(iid), byref(obj))
        return obj

    def __init_subclass__(cls):
        cls._method_defs = []
        cls._vtblend = getattr(cls.__mro__[1], "_vtblend", 2)


@GUID("AF86E2E0-B12D-4C6A-9C5A-D7AA65101E90")
class IInspectable(IUnknown):
    pass


define_winrt_com_method(IInspectable, "GetIids", retval=POINTER(_GUID))
define_winrt_com_method(IInspectable, "GetRuntimeClassName", retval=HSTRING)
define_winrt_com_method(IInspectable, "GetTrustLevel", retval=ctypes.c_int)


@GUID("00000035-0000-0000-C000-000000000046")
class IActivationFactory(IInspectable):
    pass


define_winrt_com_method(IActivationFactory, "ActivateInstance", retval=IInspectable)
