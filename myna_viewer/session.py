"""カード状態の監視スレッドと、カード通信を行う作業スレッド。

どちらも結果を queue に入れ、tkinter のメインスレッドが after() で受け取る。
"""
import queue
import threading
from dataclasses import dataclass

from . import pcsc


@dataclass
class ReaderState:
    present: bool      # カードが置かれていて通信可能
    count: int         # Windows が数えるカード抜き差しの回数（上位16ビット）
    lost: bool = False  # リーダー自体が使えなくなった


class CardMonitor(threading.Thread):
    """1台のリーダーを監視し、状態が変わるたびに ('state', monitor, ReaderState) を送る。"""

    POLL_MS = 500

    def __init__(self, reader: str, out: queue.Queue):
        super().__init__(daemon=True)
        self.reader = reader
        self.out = out
        self._stopping = threading.Event()
        self._ctx = None

    def stop(self):
        self._stopping.set()
        ctx = self._ctx
        if ctx is not None:
            ctx.cancel()

    def run(self):
        try:
            self._ctx = pcsc.Context()
        except pcsc.PCSCError:
            self.out.put(('state', self, ReaderState(False, 0, lost=True)))
            return
        try:
            current = pcsc.SCARD_STATE_UNAWARE
            while not self._stopping.is_set():
                try:
                    res = self._ctx.get_status_change(self.reader, current, self.POLL_MS)
                except pcsc.PCSCError as e:
                    if e.code == pcsc.SCARD_E_CANCELLED or self._stopping.is_set():
                        return
                    self.out.put(('state', self, ReaderState(False, 0, lost=True)))
                    return
                if res is None:
                    continue
                event, _atr = res
                current = event & ~pcsc.SCARD_STATE_CHANGED
                lost = bool(event & (pcsc.SCARD_STATE_UNKNOWN | pcsc.SCARD_STATE_UNAVAILABLE))
                present = bool(event & pcsc.SCARD_STATE_PRESENT) and not event & pcsc.SCARD_STATE_MUTE
                self.out.put(('state', self, ReaderState(present and not lost, event >> 16, lost)))
                if lost:
                    return
        finally:
            self._ctx.close()


def run_card_task(reader: str, task, out: queue.Queue, token, after=None):
    """task(card) を別スレッドで実行し、('done', token, result) か ('error', token, exc) を送る。

    after があれば、カードとの接続を閉じた後に after(result) を同じスレッドで実行する。
    """

    def worker():
        try:
            with pcsc.Context() as ctx, ctx.connect(reader) as card:
                result = task(card)
            if after is not None:
                result = after(result)
            out.put(('done', token, result))
        except Exception as e:  # noqa: BLE001 - 例外は画面側で種類ごとに扱う
            out.put(('error', token, e))

    threading.Thread(target=worker, daemon=True).start()
