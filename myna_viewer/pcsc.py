"""Windows の PC/SC (winscard.dll) を ctypes で呼ぶ最小限のラッパー。"""
import ctypes
import os
from ctypes import wintypes

# 名前だけで読み込むと exe と同じフォルダの winscard.dll が優先されうる（DLL 乗っ取り）ので、
# System32 の絶対パスで読み込む
_winscard = ctypes.WinDLL(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'winscard.dll'))

SCARD_SCOPE_USER = 0
SCARD_SHARE_SHARED = 2
SCARD_PROTOCOL_T0 = 1
SCARD_PROTOCOL_T1 = 2
SCARD_LEAVE_CARD = 0
SCARD_ATTR_ATR_STRING = 0x00090303

SCARD_STATE_UNAWARE = 0x0000
SCARD_STATE_CHANGED = 0x0002
SCARD_STATE_UNKNOWN = 0x0004
SCARD_STATE_UNAVAILABLE = 0x0008
SCARD_STATE_EMPTY = 0x0010
SCARD_STATE_PRESENT = 0x0020
SCARD_STATE_MUTE = 0x0200

SCARD_S_SUCCESS = 0x00000000
SCARD_E_CANCELLED = 0x80100002
SCARD_E_UNKNOWN_READER = 0x80100009
SCARD_E_TIMEOUT = 0x8010000A
SCARD_E_NO_SMARTCARD = 0x8010000C
SCARD_F_COMM_ERROR = 0x80100013
SCARD_E_NOT_TRANSACTED = 0x80100016
SCARD_E_READER_UNAVAILABLE = 0x80100017
SCARD_E_NO_SERVICE = 0x8010001D
SCARD_E_SERVICE_STOPPED = 0x8010001E
SCARD_E_NO_READERS_AVAILABLE = 0x8010002E
SCARD_E_COMM_DATA_LOST = 0x8010002F
SCARD_W_UNRESPONSIVE_CARD = 0x80100066
SCARD_W_UNPOWERED_CARD = 0x80100067
SCARD_W_RESET_CARD = 0x80100068
SCARD_W_REMOVED_CARD = 0x80100069

# カードやリーダーとの接続が失われたことを示すエラー
CONNECTION_LOST_CODES = {
    SCARD_E_UNKNOWN_READER, SCARD_E_NO_SMARTCARD, SCARD_F_COMM_ERROR,
    SCARD_E_NOT_TRANSACTED, SCARD_E_READER_UNAVAILABLE, SCARD_E_NO_SERVICE,
    SCARD_E_SERVICE_STOPPED, SCARD_E_NO_READERS_AVAILABLE, SCARD_E_COMM_DATA_LOST,
    SCARD_W_UNRESPONSIVE_CARD, SCARD_W_UNPOWERED_CARD, SCARD_W_RESET_CARD,
    SCARD_W_REMOVED_CARD,
}

SCARDCONTEXT = ctypes.c_size_t
SCARDHANDLE = ctypes.c_size_t


class SCARD_IO_REQUEST(ctypes.Structure):
    _fields_ = [('dwProtocol', wintypes.DWORD), ('cbPciLength', wintypes.DWORD)]


class SCARD_READERSTATEW(ctypes.Structure):
    _fields_ = [
        ('szReader', wintypes.LPCWSTR),
        ('pvUserData', ctypes.c_void_p),
        ('dwCurrentState', wintypes.DWORD),
        ('dwEventState', wintypes.DWORD),
        ('cbAtr', wintypes.DWORD),
        ('rgbAtr', ctypes.c_ubyte * 36),
    ]


def _proto(name, *args):
    fn = getattr(_winscard, name)
    fn.argtypes = list(args)
    fn.restype = ctypes.c_long
    return fn


_EstablishContext = _proto('SCardEstablishContext', wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.POINTER(SCARDCONTEXT))
_ReleaseContext = _proto('SCardReleaseContext', SCARDCONTEXT)
_Cancel = _proto('SCardCancel', SCARDCONTEXT)
_ListReaders = _proto('SCardListReadersW', SCARDCONTEXT, wintypes.LPCWSTR, wintypes.LPWSTR,
                      ctypes.POINTER(wintypes.DWORD))
_Connect = _proto('SCardConnectW', SCARDCONTEXT, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                  ctypes.POINTER(SCARDHANDLE), ctypes.POINTER(wintypes.DWORD))
_Disconnect = _proto('SCardDisconnect', SCARDHANDLE, wintypes.DWORD)
_GetAttrib = _proto('SCardGetAttrib', SCARDHANDLE, wintypes.DWORD, ctypes.c_void_p,
                    ctypes.POINTER(wintypes.DWORD))
_Transmit = _proto('SCardTransmit', SCARDHANDLE, ctypes.POINTER(SCARD_IO_REQUEST), ctypes.c_void_p,
                   wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
_GetStatusChange = _proto('SCardGetStatusChangeW', SCARDCONTEXT, wintypes.DWORD,
                          ctypes.POINTER(SCARD_READERSTATEW), wintypes.DWORD)


class PCSCError(Exception):
    def __init__(self, func, code):
        self.code = code & 0xFFFFFFFF
        super().__init__('%s failed: 0x%08X' % (func, self.code))


class ConnectionLostError(PCSCError):
    """カードが離れた・リーダーが抜けたなど、通信相手を失ったときのエラー。"""


def _check(func, rc):
    rc &= 0xFFFFFFFF
    if rc == SCARD_S_SUCCESS:
        return
    if rc in CONNECTION_LOST_CODES:
        raise ConnectionLostError(func, rc)
    raise PCSCError(func, rc)


class Context:
    def __init__(self):
        self.handle = SCARDCONTEXT()
        _check('SCardEstablishContext',
               _EstablishContext(SCARD_SCOPE_USER, None, None, ctypes.byref(self.handle)))

    def close(self):
        if self.handle.value:
            _ReleaseContext(self.handle)
            self.handle = SCARDCONTEXT()

    def cancel(self):
        """別スレッドで待機中の get_status_change を中断させる。"""
        if self.handle.value:
            _Cancel(self.handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def list_readers(self):
        size = wintypes.DWORD(0)
        rc = _ListReaders(self.handle, None, None, ctypes.byref(size)) & 0xFFFFFFFF
        if rc == SCARD_E_NO_READERS_AVAILABLE:
            return []
        _check('SCardListReaders', rc)
        buf = ctypes.create_unicode_buffer(size.value)
        rc = _ListReaders(self.handle, None, buf, ctypes.byref(size)) & 0xFFFFFFFF
        if rc == SCARD_E_NO_READERS_AVAILABLE:
            return []
        _check('SCardListReaders', rc)
        return [r for r in buf[:size.value].split('\0') if r.strip()]

    def get_status_change(self, reader, current_state, timeout_ms):
        """リーダーの状態を返す (event_state, atr)。timeout なら None。"""
        st = SCARD_READERSTATEW()
        st.szReader = reader
        st.dwCurrentState = current_state
        rc = _GetStatusChange(self.handle, timeout_ms, ctypes.byref(st), 1) & 0xFFFFFFFF
        if rc == SCARD_E_TIMEOUT:
            return None
        _check('SCardGetStatusChange', rc)
        return st.dwEventState, bytes(st.rgbAtr[:st.cbAtr])

    def connect(self, reader):
        return Card(self, reader)


class Card:
    def __init__(self, ctx, reader):
        self.handle = SCARDHANDLE()
        proto = wintypes.DWORD()
        _check('SCardConnect', _Connect(ctx.handle, reader, SCARD_SHARE_SHARED,
                                        SCARD_PROTOCOL_T0 | SCARD_PROTOCOL_T1,
                                        ctypes.byref(self.handle), ctypes.byref(proto)))
        self.protocol = proto.value

    def close(self):
        if self.handle.value:
            _Disconnect(self.handle, SCARD_LEAVE_CARD)
            self.handle = SCARDHANDLE()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def atr(self):
        buf = (ctypes.c_ubyte * 64)()
        size = wintypes.DWORD(len(buf))
        _check('SCardGetAttrib', _GetAttrib(self.handle, SCARD_ATTR_ATR_STRING, buf, ctypes.byref(size)))
        return bytes(buf[:size.value])

    def transmit(self, apdu):
        """APDU を送り (data, sw1, sw2) を返す。"""
        pci = SCARD_IO_REQUEST(self.protocol, ctypes.sizeof(SCARD_IO_REQUEST))
        send = (ctypes.c_ubyte * len(apdu)).from_buffer_copy(bytes(apdu))
        recv = (ctypes.c_ubyte * 65538)()
        size = wintypes.DWORD(len(recv))
        _check('SCardTransmit', _Transmit(self.handle, ctypes.byref(pci), send, len(apdu),
                                          None, recv, ctypes.byref(size)))
        if size.value < 2:
            raise PCSCError('SCardTransmit(short response)', 0)
        res = bytes(recv[:size.value])
        return res[:-2], res[-2], res[-1]
