"""読み取り履歴。Windows DPAPI で暗号化して保存する。

- 暗号化は CryptProtectData（現在の Windows ユーザーに紐づく鍵）＋アプリ固有のエントロピー。
  同じ PC の別ユーザーや、ファイルを持ち出した第三者は復号できない。
  改ざんされたファイルは復号時に検出される。
- 旧形式の平文 CSV (history.csv) があれば暗号化ファイルへ移し、元の CSV は上書きしてから削除する。
"""
import csv
import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import astuple, dataclass

COLUMNS = ['取得日時', '取得モード', '取得した情報', '氏名の冒頭一字', '住所の都道府県', '生年月日の日', '真正性']
MAGIC = b'MCVH1\n'
ENTROPY = b'MynaCardViewer/history/v1'
CRYPTPROTECT_UI_FORBIDDEN = 0x1

_system32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
_crypt32 = ctypes.WinDLL(os.path.join(_system32, 'crypt32.dll'))
_kernel32 = ctypes.WinDLL(os.path.join(_system32, 'kernel32.dll'))


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


_crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
                                      ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
                                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


class HistoryError(Exception):
    """履歴ファイルを復号・解析できない（破損・改ざん・別ユーザーのファイルなど）。"""


def _blob(data: bytes):
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data) if data else (ctypes.c_ubyte * 1)()
    return DATA_BLOB(len(data), buf), buf


def _call(fn, data: bytes):
    src, _keep1 = _blob(data)
    ent, _keep2 = _blob(ENTROPY)
    out = DATA_BLOB()
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise HistoryError('DPAPI error %d' % ctypes.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    return _call(_crypt32.CryptProtectData, data)


def unprotect(data: bytes) -> bytes:
    return _call(_crypt32.CryptUnprotectData, data)


@dataclass
class Record:
    time: str
    mode: str
    items: str
    name_initial: str = ''
    prefecture: str = ''
    birth_day: str = ''
    authenticity: str = ''  # 真正性の列を追加する前の履歴では空


def default_dir():
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'MynaCardViewer')


def _wipe(path):
    """平文ファイルを 0 で上書きしてから削除する。"""
    try:
        size = os.path.getsize(path)
        with open(path, 'r+b') as f:
            f.write(b'\0' * size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    os.remove(path)


class History:
    def __init__(self, directory=None):
        self.dir = directory or default_dir()
        self.path = os.path.join(self.dir, 'history.dat')
        self.legacy_csv = os.path.join(self.dir, 'history.csv')

    def _read_legacy(self):
        with open(self.legacy_csv, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.reader(f))
        return [Record(*(row + [''] * len(COLUMNS))[:len(COLUMNS)]) for row in rows[1:] if row]

    def migrate_legacy(self):
        """旧形式の平文 CSV を暗号化ファイルへ移す。移した件数を返す。"""
        if not os.path.exists(self.legacy_csv):
            return 0
        legacy = self._read_legacy()
        self._save(self.load() + legacy)
        _wipe(self.legacy_csv)
        return len(legacy)

    def load(self):
        try:
            with open(self.path, 'rb') as f:
                raw = f.read()
        except FileNotFoundError:
            return []
        if not raw.startswith(MAGIC):
            raise HistoryError('unknown file format')
        try:
            rows = json.loads(unprotect(raw[len(MAGIC):]).decode('utf-8'))
            return [Record(*[str(v) for v in row][:len(COLUMNS)]) for row in rows]
        except (ValueError, TypeError) as e:
            raise HistoryError('broken history: %s' % type(e).__name__) from e

    def _save(self, records):
        os.makedirs(self.dir, exist_ok=True)
        payload = json.dumps([list(astuple(r)) for r in records], ensure_ascii=False).encode('utf-8')
        tmp = self.path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(MAGIC + protect(payload))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def append(self, record: Record):
        self._save(self.load() + [record])  # 復号できない場合は HistoryError（既存の履歴を上書きしない）

    def delete(self, indexes):
        drop = set(indexes)
        self._save([r for i, r in enumerate(self.load()) if i not in drop])

    def clear(self):
        self._save([])
