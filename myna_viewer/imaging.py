"""カードから受け取った画像を安全に開く。

カードの中身は信頼できない入力（偽造カード・スマホのカードエミュレーション等）として扱い、
- 想定した形式（顔写真は JPEG2000、氏名・住所は PNG）以外のデコーダーを使わない
- 画素数・データ量に上限を設け、巨大画像（解凍爆弾）でメモリを使い果たさない
"""
import io
import warnings

from PIL import Image

MAX_PIXELS = 4_000_000   # 券面画像は数十万画素程度
MAX_BYTES = 0x8000       # EF 全体の上限と同じ

PHOTO_FORMATS = ['JPEG2000']
TEXT_IMAGE_FORMATS = ['PNG']


class UnsafeImageError(Exception):
    pass


def open_image(data: bytes, formats):
    if len(data) > MAX_BYTES:
        raise UnsafeImageError('image data too large')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            img = Image.open(io.BytesIO(data), formats=formats)
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as e:
            raise UnsafeImageError('decompression bomb') from e
        if img.width * img.height > MAX_PIXELS:
            raise UnsafeImageError('too many pixels')
        img.load()
    return img
