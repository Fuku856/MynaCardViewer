"""マイナンバーカード券面確認APの読み取り。

参照した公開情報:
- jpki/myna (MIT License) src/visual.rs / src/cli.rs
  券面確認AP の AID、券面データ EF 0002 の構造、256バイト単位の READ BINARY
- soltia48/myna-card (MIT License) src/ap/surface.rs, src/data.rs
  EF 0002 の全体構造（外側タグ FF20、オフセット表 DF21、DF22 生年月日、DF23 性別、DF24 カード公開鍵、
  DF25 氏名画像 PNG、DF26 住所画像 PNG、DF27 顔写真 JPEG2000、DF28 署名、DF29 有効期限、
  DF2A セキュリティコード画像）、EF 0004 の発行者証明書、照合番号Bの EF 0012、署名コマンド 80 2A
- 照合番号B = 生年月日(券面の和暦の年2桁+月日、元年は01) + 有効期限の西暦年(4桁) + セキュリティコード(4桁)
"""
from dataclasses import dataclass, field

from . import authenticity as auth
from .pcsc import Card
from .tlv import MAX_EF_SIZE, CardError, parse_tlv_header, parse_tlv_items

VISUAL_AP_AID = bytes.fromhex('D3921000310001010402')
EF_PIN_B = bytes.fromhex('0012')
EF_VISUAL = bytes.fromhex('0002')
EF_CERTIFICATE = bytes.fromhex('0004')

TAG_FACE = 0xFF20
TAG_OFFSETS = 0xDF21
TAG_BIRTH = 0xDF22
TAG_SEX = 0xDF23
TAG_CARD_KEY = 0xDF24
TAG_NAME = 0xDF25
TAG_ADDR = 0xDF26
TAG_PHOTO = 0xDF27
TAG_SIGNATURE = 0xDF28
TAG_EXPIRE = 0xDF29


class WrongPinError(CardError):
    def __init__(self, remaining):
        self.remaining = remaining
        super().__init__('照合番号Bが違います（残り %d 回）' % remaining)


class PinLockedError(CardError):
    def __init__(self):
        super().__init__('照合番号Bがロックされています。市区町村の窓口で解除が必要です')


def _sw(sw1, sw2):
    return '%02X %02X' % (sw1, sw2)


def select_ap(card: Card, aid: bytes):
    _, sw1, sw2 = card.transmit(bytes([0x00, 0xA4, 0x04, 0x0C, len(aid)]) + aid)
    if (sw1, sw2) != (0x90, 0x00):
        raise CardError('マイナンバーカードの券面確認APが見つかりません（%s）\n'
                        'スマートフォンではなくカード本体を置いてください' % _sw(sw1, sw2))


def select_ef(card: Card, fid: bytes):
    _, sw1, sw2 = card.transmit(bytes([0x00, 0xA4, 0x02, 0x0C, len(fid)]) + fid)
    if (sw1, sw2) != (0x90, 0x00):
        raise CardError('カード内のファイル %s を選択できません（%s）' % (fid.hex().upper(), _sw(sw1, sw2)))


def pin_b_remaining(card: Card):
    """照合番号Bの残り回数を返す（データ無し VERIFY は回数を消費しない）。"""
    select_ap(card, VISUAL_AP_AID)
    select_ef(card, EF_PIN_B)
    _, sw1, sw2 = card.transmit(bytes([0x00, 0x20, 0x00, 0x80]))
    if sw1 == 0x63 and sw2 & 0xF0 == 0xC0:
        return sw2 & 0x0F
    if (sw1, sw2) in ((0x69, 0x83), (0x69, 0x84)):
        return 0
    raise CardError('照合番号Bの残り回数を取得できません（%s）' % _sw(sw1, sw2))


def verify_pin_b(card: Card, pin: str):
    select_ap(card, VISUAL_AP_AID)
    select_ef(card, EF_PIN_B)
    data = pin.encode('ascii')
    _, sw1, sw2 = card.transmit(bytes([0x00, 0x20, 0x00, 0x80, len(data)]) + data)
    if (sw1, sw2) == (0x90, 0x00):
        return
    if sw1 == 0x63 and sw2 & 0xF0 == 0xC0:
        if sw2 & 0x0F == 0:
            raise PinLockedError()
        raise WrongPinError(sw2 & 0x0F)
    if (sw1, sw2) in ((0x69, 0x83), (0x69, 0x84)):
        raise PinLockedError()
    raise CardError('照合番号Bの照合に失敗しました（%s）' % _sw(sw1, sw2))


def read_binary(card: Card, offset: int, length: int):
    """READ BINARY を 256 バイト以下に分けて読む（拡張長 APDU を使わない）。"""
    if offset < 0 or offset + length > MAX_EF_SIZE:
        raise CardError('読み出し範囲が想定外です（%d+%d バイト）' % (offset, length))
    out = bytearray()
    while len(out) < length:
        pos = offset + len(out)
        remaining = length - len(out)
        le = 0 if remaining >= 0x100 else remaining
        data, sw1, sw2 = card.transmit(bytes([0x00, 0xB0, (pos >> 8) & 0x7F, pos & 0xFF, le]))
        if (sw1, sw2) != (0x90, 0x00) or not data:
            raise CardError('データの読み出しに失敗しました（%s, %d/%d バイト）' % (_sw(sw1, sw2), len(out), length))
        out += data
    return bytes(out[:length])


def read_tlv_ef(card: Card, fid: bytes):
    """1 つの BER-TLV オブジェクトが入った EF を選択して、そのオブジェクト全体（ヘッダー込み）を返す。"""
    select_ef(card, fid)
    head = read_binary(card, 0, 8)
    _, n, vpos = parse_tlv_header(head)
    if vpos + n > MAX_EF_SIZE:
        raise CardError('データの長さが想定外です（%d バイト）' % (vpos + n))
    return read_binary(card, 0, vpos + n)


def compute_signature(card: Card, digest_info: bytes):
    """券面確認AP のカード自身の鍵で署名させる（80 2A、照合番号不要）。"""
    data, sw1, sw2 = card.transmit(bytes([0x80, 0x2A, 0x00, 0x00, len(digest_info)]) + digest_info + b'\x00')
    if (sw1, sw2) != (0x90, 0x00):
        raise auth.AuthenticityError('カードが署名に応じません（%s）' % _sw(sw1, sw2))
    return data


@dataclass
class VisualInfo:
    birth: str = ''
    sex: str = ''
    expire: str = ''
    name_png: bytes = b''
    addr_png: bytes = b''
    photo_jp2: bytes = b''
    authentic: bool = False
    auth_message: str = '真正性を確認していません'
    extra_tags: dict = field(default_factory=dict)


def _ascii(b: bytes):
    try:
        return b.decode('ascii').strip()
    except UnicodeDecodeError:
        return ''


@dataclass
class CardFace:
    """EF 0002 を解析した結果。表示と署名検証は同じ fields を使う。"""
    fields: dict      # {tag: Tlv}
    offsets_ok: bool


def parse_card_face(raw: bytes) -> CardFace:
    tag, n, vpos = parse_tlv_header(raw)
    if tag != TAG_FACE:
        raise CardError('券面データの形式が想定外です（タグ %04X）' % tag)
    items = parse_tlv_items(raw[vpos:vpos + n], base=vpos)
    fields = {}
    duplicated = False
    for t in items:
        if t.tag in fields:
            duplicated = True  # 同じタグが2回あると、表示と署名検証で別の値を見る恐れがある
            continue
        fields[t.tag] = t
    offsets_ok = not duplicated
    table = fields.get(TAG_OFFSETS)
    if table is not None:
        starts = [t.offset for t in items if t.tag != TAG_OFFSETS]
        declared = [int.from_bytes(table.value[i:i + 2], 'big') for i in range(0, len(table.value), 2)]
        offsets_ok = offsets_ok and declared == starts
    return CardFace(fields, offsets_ok)


def decode_visual(face: CardFace) -> VisualInfo:
    v = {tag: t.value for tag, t in face.fields.items()}
    info = VisualInfo(
        birth=_ascii(v.get(TAG_BIRTH, b'')),
        sex=_ascii(v.get(TAG_SEX, b'')),
        name_png=v.get(TAG_NAME, b''),
        addr_png=v.get(TAG_ADDR, b''),
        photo_jp2=v.get(TAG_PHOTO, b''),
    )
    expire = _ascii(v.get(TAG_EXPIRE, b''))
    if len(expire) == 8 and expire.isdigit():
        info.expire = expire
    info.extra_tags = {tag: len(val) for tag, val in v.items() if tag > TAG_SIGNATURE}
    return info


def check_authenticity(card: Card, face: CardFace):
    """真正性を 3 段階で検証する。失敗したら AuthenticityError。詳細は authenticity.py。"""
    if not face.offsets_ok:
        raise auth.AuthenticityError('券面データの構造が不正です（オフセット表の不一致または重複したタグ）')
    # 1. 発行者証明書（EF 0004）を J-LIS の CA 鍵で検証
    issuer = auth.verify_cv_certificate(read_tlv_ef(card, EF_CERTIFICATE))
    # 2. 券面データの署名を発行者の鍵で検証し、署名で守られたカード公開鍵を取り出す
    card_key = auth.verify_card_face(face.fields, issuer)
    # 3. アプリが作った乱数にカード自身の鍵で署名させ、本物のカードが今ここにあることを確かめる
    challenge = auth.new_challenge()
    digest_info = auth.sha256_digest_info(auth.sha256(challenge))
    try:
        auth.verify_pkcs1(card_key, digest_info, compute_signature(card, digest_info))
    except auth.AuthenticityError:
        raise auth.AuthenticityError('カードの実在確認に失敗しました（データを複製した偽カードの可能性）') from None


def read_visual_info(card: Card, pin: str) -> VisualInfo:
    verify_pin_b(card, pin)
    face = parse_card_face(read_tlv_ef(card, EF_VISUAL))
    info = decode_visual(face)
    try:
        check_authenticity(card, face)
    except (auth.AuthenticityError, CardError) as e:  # 接続切れ (ConnectionLostError) は呼び出し元で中断
        info.authentic, info.auth_message = False, str(e)
    else:
        info.authentic, info.auth_message = True, 'J-LIS の電子署名とカードの実在を確認しました'
    return info


# ---- 表示用の変換 ----

ERAS = [  # (開始日 YYYYMMDD, 元号)
    (20190501, '令和'), (19890108, '平成'), (19261225, '昭和'), (19120730, '大正'), (18680125, '明治'),
]


def format_date(yyyymmdd: str):
    """'19850412' -> '1985年4月12日（昭和60年4月12日）'。不明部分を含む場合はそのまま返す。"""
    if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return yyyymmdd
    y, m, d = int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:])
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return yyyymmdd
    seireki = '%d年%d月%d日' % (y, m, d)
    value = int(yyyymmdd)
    for start, era in ERAS:
        if value >= start:
            n = y - start // 10000 + 1
            return '%s（%s%s年%d月%d日）' % (seireki, era, '元' if n == 1 else n, m, d)
    return seireki


def age_range(yyyymmdd: str, today):
    """満年齢の (最小, 最大) を返す。月日が不明（00/99）なら幅を持たせ、年が不明なら None。

    年齢計算ニ関スル法律により誕生日の前日の終了時に加齢するので、
    2月29日生まれは平年では3月1日に1歳増える（下の比較で自然にそうなる）。
    """
    if len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    y, m, d = int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:])
    if y == 0:
        return None
    m_known = 1 <= m <= 12
    d_known = m_known and 1 <= d <= 31
    latest = (m, d) if d_known else ((m, 31) if m_known else (12, 31))
    earliest = (m, d) if d_known else ((m, 1) if m_known else (1, 1))
    base = today.year - y
    youngest = base - ((today.month, today.day) < latest)
    oldest = base - ((today.month, today.day) < earliest)
    return youngest, oldest


def birth_day(yyyymmdd: str):
    """生年月日の「日」だけ（履歴用）。不明なら空文字。"""
    if len(yyyymmdd) == 8 and yyyymmdd.isdigit() and 1 <= int(yyyymmdd[6:]) <= 31:
        return '%d日' % int(yyyymmdd[6:])
    return ''


def format_sex(code: str):
    return {'1': '男', '2': '女'}.get(code, code)


def build_pin_b(birth_yymmdd: str, expire_yyyy: str, security_code: str):
    """入力値を検証して照合番号B（14桁）を返す。不正なら ValueError。"""
    parts = [('生年月日', birth_yymmdd, 6), ('有効期限の西暦年', expire_yyyy, 4),
             ('セキュリティコード', security_code, 4)]
    for label, value, size in parts:
        if len(value) != size or not value.isdigit():
            raise ValueError('%sは%d桁の数字で入力してください' % (label, size))
    return birth_yymmdd + expire_yyyy + security_code
