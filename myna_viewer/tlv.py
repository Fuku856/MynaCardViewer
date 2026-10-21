"""BER-TLV の解析。カードの応答は信頼できない入力として扱う。"""
from dataclasses import dataclass

MAX_EF_SIZE = 0x8000  # READ BINARY のオフセット指定（15ビット）で読める上限


class CardError(Exception):
    """カードが想定外の応答を返したときのエラー（表示用メッセージ付き）。"""


@dataclass
class Tlv:
    tag: int
    value: bytes
    raw: bytes      # タグ・長さを含む、書かれていたとおりのバイト列
    offset: int     # 解析対象の先頭からの位置


def parse_tlv_header(buf: bytes, pos: int = 0):
    """BER-TLV のタグと長さを読み、(tag, length, value_offset) を返す。

    途中で切れたデータや過大な長さは CardError にする。
    """
    try:
        tag = buf[pos]
        pos += 1
        if tag & 0x1F == 0x1F:
            for _ in range(3):  # タグは最大4バイトまで
                b = buf[pos]
                pos += 1
                tag = (tag << 8) | b
                if not b & 0x80:
                    break
            else:
                raise CardError('券面データの形式が想定外です（タグが長すぎます）')
        n = buf[pos]
        pos += 1
        if n & 0x80:
            count = n & 0x7F
            if count == 0 or count > 3 or pos + count > len(buf):
                raise CardError('券面データの形式が想定外です（長さ表現 %02X）' % n)
            n = int.from_bytes(buf[pos:pos + count], 'big')
            pos += count
    except IndexError:
        raise CardError('券面データが途中で切れています') from None
    if n > MAX_EF_SIZE:
        raise CardError('券面データの長さが想定外です（%d バイト）' % n)
    return tag, n, pos


def parse_tlv_items(buf: bytes, base: int = 0):
    """連続する TLV を Tlv のリストにする。末尾の 00 / FF の埋め草で止まる。"""
    items = []
    pos = 0
    while pos < len(buf):
        if buf[pos] in (0x00, 0xFF):  # 埋め草（BER のタグとしても使われない）
            break
        tag, n, vpos = parse_tlv_header(buf, pos)
        if vpos + n > len(buf):
            raise CardError('券面データが途中で切れています')
        items.append(Tlv(tag, buf[vpos:vpos + n], buf[pos:vpos + n], base + pos))
        pos = vpos + n
    return items


def parse_tlv_list(buf: bytes):
    """連続する TLV を {tag: value} にする（先に出たタグを優先）。"""
    items = {}
    for t in parse_tlv_items(buf):
        items.setdefault(t.tag, t.value)
    return items
