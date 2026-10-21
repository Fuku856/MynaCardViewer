"""券面事項確認AP のデータの真正性（偽造・複製カードでないこと）の検証。

検証の方式・データ構造・CA 公開鍵は soltia48/myna-card (MIT License,
Copyright (c) 2026 KIRISHIKI Yudai) の src/ap/surface.rs, src/data.rs, src/ca.rs による。

3 段階すべてに通ったときだけ「真正」とする。
1. 発行者証明書: EF 0004 のカード検証可能証明書 (CV 証明書, 7F21) を、アプリに組み込んだ
   J-LIS の CA 公開鍵（証明者鍵ID 5000023）で検証する。カードから来た鍵は信頼の起点にしない。
2. 券面データ: EF 0002 の署名 DF28 を 1 の証明書の公開鍵で検証する。
   署名対象は 3 グループ（DF22 生年月日・DF23 性別・DF24 カード公開鍵 / DF25 氏名画像・DF26 住所画像 /
   DF27 顔写真）それぞれの SHA-256 を連結したもの。DF29 有効期限・DF2A セキュリティコード画像は対象外。
3. カードの実在: アプリが作った乱数にカード自身の鍵で署名させ（80 2A, 照合番号不要）、
   2 で真正と確かめた DF24 の公開鍵で検証する。データだけを複製した偽カードはここで失敗する。
   乱数はカードの GET CHALLENGE ではなくアプリ側で作る（偽カードが録画した応答を使い回せないように）。

失効（紛失・廃止したカード）の確認はしていない。
"""
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from .tlv import CardError, parse_tlv_header, parse_tlv_list

# 証明者鍵ID "5000023" — 券面事項確認AP の CV 証明書を発行する J-LIS の鍵（本番系のみ。試験用カードの鍵は入れない）
CA_KEY_ID = bytes.fromhex('35303030303233080530303100000000')
CA_MODULUS = int(
    'B2F740F0AD86D17CF3F537D23B4B3C65A3CB58474D857F55D0B9BC0411B1CB0849068DE332A50D63B3E6921C9CCBE615'
    '6496EFB04BE8FFCC74FDE9A228A36AD7EDCE9793204E35973DC911108365002C40169E0B60018DFB99E1E2310D9A110F'
    'F0FC269F1F220C63B8A1510DE5A6898220C78908FC280D8AA95D5D113B7CF00700F3FF5AD8B3D6AD2EB53C7D563498B4'
    'A938DEFFC9BDA32BF4146C1B07EF870D99EB35DE5D70AC18715874277A13151D217D04D42E97923923A40FBC7D06FAA8'
    '338CCBE876620F9B8EF4471CD4B9E29D4EA621E0FB8F1DC04B487210097DBAAF3D8E0550C76A5B97CF202FA47C7A2622'
    'F1C084E65C13D13B8E7AB226F5F51F87', 16)
CA_EXPONENT = 65537

# AlgorithmIdentifier { sha256, NULL }
_SHA256_ALG = bytes.fromhex('300D06096086480165030402010500')
CV_BODY_LEN = 297  # 証明者鍵ID 16 + 被証明者鍵ID 16 + RSA-2048 公開鍵 265
SIGNED_GROUPS = ((0xDF22, 0xDF23, 0xDF24), (0xDF25, 0xDF26), (0xDF27,))


class AuthenticityError(Exception):
    """真正性を確認できなかった（理由を表示用メッセージで持つ）。"""


@dataclass(frozen=True)
class RsaKey:
    n: int
    e: int

    @property
    def size(self):
        return (self.n.bit_length() + 7) // 8


CA_KEY = RsaKey(CA_MODULUS, CA_EXPONENT)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def sha256_digest_info(digest: bytes) -> bytes:
    """PKCS#1 v1.5 の SHA-256 DigestInfo。券面データでは 96 バイト（3 個連結）の digest を入れる。"""
    inner = len(_SHA256_ALG) + 2 + len(digest)
    length = bytes([inner]) if inner < 0x80 else bytes([0x81, inner])
    return b'\x30' + length + _SHA256_ALG + bytes([0x04, len(digest)]) + digest


def verify_pkcs1(key: RsaKey, digest_info: bytes, signature: bytes):
    """RSASSA-PKCS1-v1_5 の署名を検証する。

    復号した値を解析するのではなく、期待する符号化ブロック全体を組み立てて比較する
    （ゆるい解析による署名偽造を招かないように）。
    """
    k = key.size
    if len(signature) != k or k < len(digest_info) + 11:
        raise AuthenticityError('署名の長さが不正です')
    s = int.from_bytes(signature, 'big')
    if s >= key.n:
        raise AuthenticityError('署名の値が不正です')
    em = pow(s, key.e, key.n).to_bytes(k, 'big')
    expected = b'\x00\x01' + b'\xff' * (k - len(digest_info) - 3) + b'\x00' + digest_info
    if not hmac.compare_digest(em, expected):
        raise AuthenticityError('電子署名が一致しません')


def parse_rsa_key(data: bytes) -> RsaKey:
    """90（公開指数）と 91（法）の BER-TLV から公開鍵を作る。"""
    try:
        items = parse_tlv_list(data)
    except CardError as e:
        raise AuthenticityError('公開鍵の形式が不正です') from e
    e, n = items.get(0x90), items.get(0x91)
    if not e or not n:
        raise AuthenticityError('公開鍵の形式が不正です')
    key = RsaKey(int.from_bytes(n, 'big'), int.from_bytes(e, 'big'))
    if key.n.bit_length() < 2048 or key.e < 3 or key.e % 2 == 0:
        raise AuthenticityError('公開鍵が弱すぎるか不正です')
    return key


def verify_cv_certificate(raw: bytes) -> RsaKey:
    """EF 0004 の CV 証明書を検証し、証明された公開鍵（券面データの署名鍵）を返す。"""
    try:
        tag, n, vpos = parse_tlv_header(raw)
        items = parse_tlv_list(raw[vpos:vpos + n]) if tag == 0x7F21 else {}
    except CardError as e:
        raise AuthenticityError('発行者証明書の形式が不正です') from e
    body, sig = items.get(0x5F4E), items.get(0x5F37)
    if body is None or sig is None or len(body) != CV_BODY_LEN:
        raise AuthenticityError('発行者証明書の形式が不正です')
    if not hmac.compare_digest(body[:16], CA_KEY_ID):
        raise AuthenticityError('発行者証明書が J-LIS（本番）の鍵で発行されたものではありません')
    try:
        verify_pkcs1(CA_KEY, sha256_digest_info(sha256(body)), sig)
    except AuthenticityError:
        raise AuthenticityError('発行者証明書の電子署名が一致しません') from None
    return parse_rsa_key(body[32:])


def verify_card_face(fields: dict, issuer: RsaKey) -> RsaKey:
    """EF 0002 の署名 DF28 を検証し、署名で守られたカード公開鍵 (DF24) を返す。

    fields は {tag: Tlv}。表示に使うのと同じ Tlv の raw（タグ・長さ込み）をハッシュする。
    """
    try:
        digests = b''.join(sha256(b''.join(fields[t].raw for t in group)) for group in SIGNED_GROUPS)
        signature = fields[0xDF28].value
    except KeyError:
        raise AuthenticityError('券面データに署名または署名の対象がありません') from None
    try:
        verify_pkcs1(issuer, sha256_digest_info(digests), signature)
    except AuthenticityError:
        raise AuthenticityError('券面データの電子署名が一致しません（改ざん・偽造の可能性）') from None
    return parse_rsa_key(fields[0xDF24].value)


def new_challenge() -> bytes:
    return secrets.token_bytes(32)
