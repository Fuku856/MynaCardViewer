"""Windows 標準の OCR (Windows.Media.Ocr, 日本語) で券面の氏名・住所画像を文字にする。

履歴に残す「氏名の冒頭一字」「住所の都道府県」を得るためだけに使う。
Windows の OCR は白黒2値の画像や余白の大きい画像で何も返さないことがあるため、
前処理を何通りか試し、条件 (accept) を満たした最初の結果を採用する。
OCR が使えない環境や読めなかった場合は空文字を返す。
"""
import asyncio
import io
import re

from PIL import Image, ImageFilter, ImageOps, ImageStat

from .imaging import TEXT_IMAGE_FORMATS, open_image

PREFECTURES = [
    '北海道', '青森県', '岩手県', '宮城県', '秋田県', '山形県', '福島県', '茨城県', '栃木県', '群馬県',
    '埼玉県', '千葉県', '東京都', '神奈川県', '新潟県', '富山県', '石川県', '福井県', '山梨県', '長野県',
    '岐阜県', '静岡県', '愛知県', '三重県', '滋賀県', '京都府', '大阪府', '兵庫県', '奈良県', '和歌山県',
    '鳥取県', '島根県', '岡山県', '広島県', '山口県', '徳島県', '香川県', '愛媛県', '高知県', '福岡県',
    '佐賀県', '長崎県', '熊本県', '大分県', '宮崎県', '鹿児島県', '沖縄県',
]
_JP_CHAR = re.compile(r'[぀-ヿ㐀-鿿豈-﫿]')


async def _recognize(png: bytes):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    engine = OcrEngine.try_create_from_language(Language('ja'))
    if engine is None:
        raise RuntimeError('Japanese OCR engine not available')
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return ''.join(line.text for line in result.lines)


def _png(img):
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def _sources(img):
    """白地に黒文字のグレースケール画像の候補を返す。"""
    out = []
    if 'A' in img.getbands() or (img.mode == 'P' and 'transparency' in img.info):
        rgba = img.convert('RGBA')
        flat = Image.new('RGB', rgba.size, 'white')
        flat.paste(rgba, mask=rgba.getchannel('A'))
        out.append(('flat', flat.convert('L')))
        out.append(('alpha', ImageOps.invert(rgba.getchannel('A'))))  # 透過の濃さを文字として扱う
    else:
        out.append(('plain', img.convert('L')))
    fixed = []
    for name, g in out:
        if ImageStat.Stat(g).mean[0] < 128:  # 黒地に白文字なら反転
            g, name = ImageOps.invert(g), name + '-inv'
        fixed.append((name, g))
    return fixed


def _variants(img):
    for src_name, g in _sources(img):
        ink = g.point(lambda v: 255 if v < 160 else 0)
        box = ink.getbbox()
        if box is None:
            yield src_name + ' empty', None
            continue
        crop = ImageOps.expand(g.crop(box), 16, fill=255)
        yield src_name + ' as-is', g
        for target in (48, 96, 32, 144):
            s = target / crop.height
            if not 0.1 <= s <= 8:
                continue
            r = crop.resize((max(1, int(crop.width * s)), target), Image.Resampling.LANCZOS)
            yield '%s crop h%d' % (src_name, target), r
            yield '%s crop h%d blur' % (src_name, target), r.filter(ImageFilter.GaussianBlur(0.8))


def recognize(image_bytes: bytes, accept=None, diag=None):
    """画像を OCR して空白を除いた文字列を返す。

    accept(text) が真になった最初の結果を返す（省略時は日本語の文字を含めば採用）。
    diag にリストを渡すと、個人情報を含まない診断情報（画像形式・各方式の文字数）を追記する。
    """
    accept = accept or (lambda t: bool(_JP_CHAR.search(t)))
    log = diag.append if diag is not None else (lambda s: None)
    if not image_bytes:
        log('  no image data')
        return ''
    try:
        img = open_image(image_bytes, TEXT_IMAGE_FORMATS)
    except Exception as e:  # noqa: BLE001
        log('  image open failed: %s' % type(e).__name__)
        return ''
    log('  format=%s mode=%s size=%dx%d transparency=%s' % (
        img.format, img.mode, img.width, img.height, 'transparency' in img.info))
    for name, v in _variants(img):
        if v is None:
            log('  %-24s no ink' % name)
            continue
        try:
            text = re.sub(r'\s+', '', asyncio.run(_recognize(_png(v))))
        except Exception as e:  # noqa: BLE001 - OCR は履歴用の補助なので失敗しても読み取りは続ける
            log('  %-24s error %s: %s' % (name, type(e).__name__, str(e)[:80]))
            continue
        ok = accept(text)
        log('  %-24s %dx%d chars=%d jp=%d accepted=%s' % (
            name, v.width, v.height, len(text), len(_JP_CHAR.findall(text)), ok))
        if ok:
            return text
    return ''


def first_char(text: str):
    """氏名の冒頭一字（空白・記号を除いた最初の文字）。"""
    for ch in text:
        if ch.isalnum():
            return ch
    return ''


def prefecture(text: str):
    """住所文字列の先頭付近から都道府県名を探す。"""
    hits = [(text.find(p), p) for p in PREFECTURES if p in text]
    return min(hits)[1] if hits else ''
