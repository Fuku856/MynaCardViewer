"""テスト用の模擬マイナンバーカード（券面確認AP）。

RSA 鍵をその場で作り、CA → 発行者証明書 (EF 0004) → 署名付き券面データ (EF 0002) → カード鍵での
チャレンジ署名 (80 2A) までを本物と同じ構造で再現する。本物の J-LIS 鍵は持てないので、
テストでは authenticity.CA_KEY / CA_KEY_ID をここで作った CA 鍵に差し替えて使う。
"""
import functools
import secrets

from myna_viewer import authenticity as auth
from myna_viewer import card as mc
from myna_viewer import pcsc

PIN = '60041220341234'


def tlv(tag, value):
    t = tag.to_bytes(2 if tag > 0xFF else 1, 'big')
    n = len(value)
    if n < 0x80:
        ln = bytes([n])
    elif n < 0x100:
        ln = bytes([0x81, n])
    else:
        ln = bytes([0x82]) + n.to_bytes(2, 'big')
    return t + ln + value


# ---- RSA（テスト専用の素朴な実装） ----

def _is_probable_prime(n, rounds=24):
    if n < 4:
        return n in (2, 3)
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _is_probable_prime(c):
            return c


@functools.lru_cache(maxsize=None)
def rsa_key(name, bits=2048):
    """(n, e, d)。name ごとに1回だけ生成する。"""
    e = 65537
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        phi = (p - 1) * (q - 1)
        n = p * q
        if p != q and n.bit_length() == bits and phi % e:
            return n, e, pow(e, -1, phi)


def sign(key, digest_info):
    n, _, d = key
    k = (n.bit_length() + 7) // 8
    em = b'\x00\x01' + b'\xff' * (k - len(digest_info) - 3) + b'\x00' + digest_info
    return pow(int.from_bytes(em, 'big'), d, n).to_bytes(k, 'big')


def public(key):
    return auth.RsaKey(key[0], key[1])


def encode_key(key):
    n, e, _ = key
    return tlv(0x90, e.to_bytes(3, 'big')) + tlv(0x91, n.to_bytes(256, 'big'))


# ---- カードのファイル ----

SUBJECT_ID = b'5110000\x08\x05001\x00\x00\x00\x00'


def cv_certificate(ca, issuer, ca_id=auth.CA_KEY_ID):
    body = ca_id + SUBJECT_ID + encode_key(issuer)
    assert len(body) == auth.CV_BODY_LEN
    sig = sign(ca, auth.sha256_digest_info(auth.sha256(body)))
    return tlv(0x7F21, tlv(0x5F4E, body) + tlv(0x5F37, sig))


def face_fields(card_key, photo=b'\x00\x00\x00\x0CjP  \r\n\x87\n' + b'\x00' * 2500):
    return {
        0xDF22: tlv(0xDF22, b'19850412'),
        0xDF23: tlv(0xDF23, b'2'),
        0xDF24: tlv(0xDF24, encode_key(card_key)),
        0xDF25: tlv(0xDF25, b'PNGNAME'),
        0xDF26: tlv(0xDF26, b'PNGADDR'),
        0xDF27: tlv(0xDF27, photo),
    }


def card_face(issuer, card_key, fields=None, tamper=None):
    """EF 0002。tamper(fields) で署名後に中身を書き換えられる。"""
    fields = dict(fields or face_fields(card_key))
    digests = b''.join(auth.sha256(b''.join(fields[t] for t in g)) for g in auth.SIGNED_GROUPS)
    fields[0xDF28] = tlv(0xDF28, sign(issuer, auth.sha256_digest_info(digests)))
    fields[0xDF29] = tlv(0xDF29, b'20340412')
    fields[0xDF2A] = tlv(0xDF2A, b'PNGCODE')
    if tamper:
        tamper(fields)
    objs = [fields[t] for t in sorted(fields)]
    table = tlv(0xDF21, b'\x00\x00' * len(objs))  # 長さを決めるための仮の表
    body = table + b''.join(objs)
    pos = len(tlv(0xFF20, body)) - len(body) + len(table)  # 外側ヘッダー + 表の後ろが最初の項目
    offsets = b''
    for o in objs:
        offsets += pos.to_bytes(2, 'big')
        pos += len(o)
    return tlv(0xFF20, tlv(0xDF21, offsets) + b''.join(objs))


class FakeCard:
    """券面確認APだけを持つカード。remove_after 回目の送信でカードが離れる。"""

    def __init__(self, files=None, pin=PIN, retries=10, remove_after=None, card_key=None, refuse_sign=False):
        self.card_key = card_key or rsa_key('card')
        if files is None:
            files = {b'\x00\x02': card_face(rsa_key('issuer'), self.card_key),
                     b'\x00\x04': cv_certificate(rsa_key('ca'), rsa_key('issuer'))}
        self.files = files
        self.pin = pin.encode()
        self.retries = retries
        self.remove_after = remove_after
        self.refuse_sign = refuse_sign
        self.sent = []
        self.cur = None
        self.verified = False

    def transmit(self, apdu):
        apdu = bytes(apdu)
        self.sent.append(apdu)
        if self.remove_after is not None and len(self.sent) > self.remove_after:
            raise pcsc.ConnectionLostError('SCardTransmit', pcsc.SCARD_W_REMOVED_CARD)
        cla, ins = apdu[0], apdu[1]
        if ins == 0xA4:
            if apdu[2] == 0x04:
                ok = apdu[5:] == mc.VISUAL_AP_AID
                return b'', (0x90 if ok else 0x6A), (0x00 if ok else 0x82)
            fid = apdu[5:7]
            if fid in self.files or fid == b'\x00\x12':
                self.cur = fid
                return b'', 0x90, 0x00
            return b'', 0x6A, 0x82
        if ins == 0x20:
            if len(apdu) == 4:
                return b'', 0x63, 0xC0 | self.retries
            if apdu[5:] == self.pin:
                self.verified = True
                return b'', 0x90, 0x00
            self.retries -= 1
            return b'', 0x63, 0xC0 | self.retries
        if ins == 0xB0:
            assert len(apdu) == 5, 'must not use extended length'
            if self.cur == b'\x00\x02' and not self.verified:
                return b'', 0x69, 0x82
            data = self.files[self.cur]
            pos = (apdu[2] << 8) | apdu[3]
            le = apdu[4] or 256
            return data[pos:pos + le], 0x90, 0x00
        if cla == 0x80 and ins == 0x2A:
            if self.refuse_sign:
                return b'', 0x6A, 0x86
            digest_info = apdu[5:5 + apdu[4]]
            return sign(self.card_key, digest_info), 0x90, 0x00
        return b'', 0x6D, 0x00
