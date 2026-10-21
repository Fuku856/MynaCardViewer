import datetime
import io
import os
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from myna_viewer import card as mc
from myna_viewer import ocr
from myna_viewer import pcsc
from myna_viewer import tlv as tlvmod
from myna_viewer.history import History, HistoryError, Record
from myna_viewer.imaging import PHOTO_FORMATS, TEXT_IMAGE_FORMATS, UnsafeImageError, open_image
from tests.fakecard import PIN, FakeCard

FIXTURE_ADDRESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'address_osaka.png')


class CardTest(unittest.TestCase):
    def test_read_visual_info(self):
        c = FakeCard()
        info = mc.read_visual_info(c, PIN)
        self.assertEqual(info.birth, '19850412')
        self.assertEqual(info.sex, '2')
        self.assertEqual(info.name_png, b'PNGNAME')
        self.assertEqual(info.addr_png, b'PNGADDR')
        self.assertGreater(len(info.photo_jp2), 2000)
        self.assertEqual(info.expire, '20340412')
        reads = [a for a in c.sent if a[1] == 0xB0]
        self.assertTrue(all(len(a) == 5 for a in reads))
        self.assertGreater(len(reads), 10)

    def test_wrong_pin_reports_remaining(self):
        c = FakeCard()
        with self.assertRaises(mc.WrongPinError) as cm:
            mc.read_visual_info(c, PIN[:-4] + '9999')
        self.assertEqual(cm.exception.remaining, 9)

    def test_locked(self):
        with self.assertRaises(mc.PinLockedError):
            mc.read_visual_info(FakeCard(retries=1), '00000000000000')

    def test_remaining_does_not_consume(self):
        c = FakeCard()
        self.assertEqual(mc.pin_b_remaining(c), 10)
        self.assertEqual(c.retries, 10)

    def test_card_removed_midway_raises_connection_lost(self):
        c = FakeCard(remove_after=8)
        with self.assertRaises(pcsc.ConnectionLostError):
            mc.read_visual_info(c, PIN)

    def test_not_visual_ap(self):
        class Phone(FakeCard):
            def transmit(self, apdu):
                if apdu[1] == 0xA4 and apdu[2] == 0x04:
                    return b'', 0x6A, 0x82
                return super().transmit(apdu)
        with self.assertRaises(mc.CardError):
            mc.pin_b_remaining(Phone())

    def test_format(self):
        self.assertEqual(mc.format_date('19850412'), '1985年4月12日（昭和60年4月12日）')
        self.assertEqual(mc.format_date('20190501'), '2019年5月1日（令和元年5月1日）')
        self.assertEqual(mc.format_date('20190430'), '2019年4月30日（平成31年4月30日）')
        self.assertEqual(mc.format_date('19859900'), '19859900')
        self.assertEqual(mc.format_sex('1'), '男')
        with self.assertRaises(ValueError):
            mc.build_pin_b('85041', '2034', '1234')
        self.assertEqual(mc.build_pin_b('850412', '2034', '1234'), '85041220341234')


class AgeTest(unittest.TestCase):
    def test_age(self):
        d = datetime.date
        self.assertEqual(mc.age_range('19700412', d(2026, 4, 11)), (55, 55))
        self.assertEqual(mc.age_range('19700412', d(2026, 4, 12)), (56, 56))
        self.assertEqual(mc.age_range('20060412', d(2026, 4, 11)), (19, 19))
        self.assertEqual(mc.age_range('20060412', d(2026, 4, 12)), (20, 20))
        # 2月29日生まれは平年の3月1日に加齢
        self.assertEqual(mc.age_range('20040229', d(2025, 2, 28)), (20, 20))
        self.assertEqual(mc.age_range('20040229', d(2025, 3, 1)), (21, 21))
        # 月日不明は幅を持つ、年不明は判定不可
        self.assertEqual(mc.age_range('20060000', d(2026, 6, 1)), (19, 20))
        self.assertEqual(mc.age_range('20060499', d(2026, 4, 15)), (19, 20))
        self.assertIsNone(mc.age_range('00000000', d(2026, 6, 1)))
        self.assertEqual(mc.birth_day('19700412'), '12日')
        self.assertEqual(mc.birth_day('19700400'), '')


def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def write_bytes(path, data):
    with open(path, 'wb') as f:
        f.write(data)


class HistoryTest(unittest.TestCase):
    def test_roundtrip_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = History(os.path.join(tmp, 'sub'))
            self.assertEqual(h.load(), [])
            for i in range(3):
                h.append(Record('2026/10/21 15:30:0%d' % i, '年齢の確認', '顔写真・氏名・年齢', '山', '', ''))
            self.assertEqual(len(h.load()), 3)
            h.delete([0, 2])
            self.assertEqual([r.time for r in h.load()], ['2026/10/21 15:30:01'])
            h.clear()
            self.assertEqual(h.load(), [])

    def test_file_is_encrypted(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = History(tmp)
            h.append(Record('2026/10/21 15:30:00', '選択した情報を表示', '氏名・住所', '鈴', '神奈川県', '12日'))
            raw = read_bytes(h.path)
            for plain in ('鈴', '神奈川県', '12日', '2026/10/21'):
                self.assertNotIn(plain.encode('utf-8'), raw)
            self.assertEqual(h.load()[0].prefecture, '神奈川県')

    def test_tampered_file_is_detected_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = History(tmp)
            h.append(Record('t', 'm', 'i'))
            raw = bytearray(read_bytes(h.path))
            raw[-10] ^= 0xFF
            write_bytes(h.path, raw)
            with self.assertRaises(HistoryError):
                h.load()
            with self.assertRaises(HistoryError):
                h.append(Record('t2', 'm', 'i'))
            self.assertEqual(read_bytes(h.path), bytes(raw))  # 壊れた履歴を勝手に上書きしない
            h.clear()
            self.assertEqual(h.load(), [])

    def test_migrate_legacy_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'history.csv'), 'w', encoding='utf-8-sig', newline='') as f:
                f.write('取得日時,取得モード,取得した情報,氏名の冒頭一字,住所の都道府県,生年月日の日\n'
                        '2026/10/21 15:30:00,年齢の確認,顔写真・氏名・年齢,山,,\n')
            h = History(tmp)
            self.assertEqual(h.migrate_legacy(), 1)
            self.assertFalse(os.path.exists(h.legacy_csv))
            self.assertEqual(h.load()[0].name_initial, '山')
            self.assertEqual(h.migrate_legacy(), 0)


class HostileCardDataTest(unittest.TestCase):
    """偽造カード等から来る不正なデータで落ちたり暴走したりしないこと。"""

    def test_truncated_tlv(self):
        for bad in (b'\xDF',b'\xDF\x22', b'\xDF\x22\x82\x01', b'\xDF\x22\x05ab'):
            with self.assertRaises(mc.CardError):
                tlvmod.parse_tlv_list(bad)

    def test_huge_length_rejected_before_reading(self):
        c = FakeCard(files={b'\x00\x02': b'\xFF\x20\x83\xFF\xFF\xFF' + b'\x00' * 10})
        with self.assertRaises(mc.CardError):
            mc.read_visual_info(c, PIN)
        self.assertLess(sum(1 for a in c.sent if a[1] == 0xB0), 3)

    def test_overlong_tag_rejected(self):
        with self.assertRaises(mc.CardError):
            tlvmod.parse_tlv_header(b'\x1F\x81\x82\x83\x84\x05\x00')

    def test_image_format_and_size_limits(self):
        def png(img):
            b = io.BytesIO()
            img.save(b, 'PNG')
            return b.getvalue()
        small = png(Image.new('1', (100, 30), 1))
        self.assertEqual(open_image(small, TEXT_IMAGE_FORMATS).size, (100, 30))
        with self.assertRaises(Exception):
            open_image(small, PHOTO_FORMATS)  # PNG を顔写真(JPEG2000)としては開かない
        bomb = png(Image.new('1', (5000, 5000), 1))  # 2500万画素（圧縮後は小さい）
        with self.assertRaises(UnsafeImageError):
            open_image(bomb, TEXT_IMAGE_FORMATS)


class OcrTesseractTest(unittest.TestCase):
    def test_refuses_non_png_input(self):
        # 画像でないものを渡すと Tesseract がファイル名（URL）の一覧として読むので、渡さないこと
        with self.assertRaises(ValueError):
            ocr._recognize(b'https://example.com/a.png\n')

    def test_name_initial_majority_vote(self):
        answers = iter(['阿部樹', '岡部樹', '阿部樹', '阿部樹', '岡部'])
        img = Image.new('L', (200, 40), 255)
        ImageDraw.Draw(img).rectangle((20, 10, 60, 30), fill=0)  # 前処理が「文字あり」と判断する程度の黒
        b = io.BytesIO()
        img.save(b, 'PNG')
        with mock.patch.object(ocr, '_recognize', lambda png: next(answers)):
            self.assertEqual(ocr.name_initial(b.getvalue(), votes=5), '阿')

    @unittest.skipUnless(ocr.available(), 'vendor/tesseract がない（tools/fetch_tesseract.ps1 で取得）')
    def test_real_tesseract_reads_prefecture(self):
        # 日本語フォントの無い環境（GitHub Actions など）でも試せるよう、架空の住所を描いた白黒 PNG を使う
        self.assertEqual(ocr.prefecture_of(read_bytes(FIXTURE_ADDRESS)), '大阪府')


class OcrHelperTest(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(ocr.first_char('　山田太郎'), '山')
        self.assertEqual(ocr.prefecture('京都府京都市'), '京都府')
        self.assertEqual(ocr.prefecture('東京都府中市'), '東京都')
        self.assertEqual(ocr.prefecture('不明'), '')


if __name__ == '__main__':
    unittest.main()
