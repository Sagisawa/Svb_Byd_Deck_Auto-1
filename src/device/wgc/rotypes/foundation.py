"""Windows.Foundation interfaces."""
import ctypes
from ctypes import c_int64
from .inspectable import IInspectable
from .idldsl import GUID, define_winrt_com_method
from .types import GUID as _GUID
from .delegate import Delegate


class EventRegistrationToken(ctypes.Structure):
    _fields_ = [("value", c_int64)]


@GUID("30D5A761-69DE-4572-A48A-5DAE66DF8664")
class IClosable(IInspectable):
    pass


define_winrt_com_method(IClosable, "Close", vtbl=6)


class TypedEventHandler:
    def __init__(self, sender_cls, args_cls):
        self.sender_cls = sender_cls
        self.args_cls = args_cls

    def delegate(self, callback):
        d = Delegate(callback, ctypes.c_void_p, ctypes.c_void_p)
        d.GUID = _GUID("9DE1C534-6AE6-4067-8764-D59BF5FC8165")
        return d
