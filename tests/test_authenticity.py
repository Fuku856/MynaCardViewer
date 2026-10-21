import unittest
from unittest import mock

from myna_viewer import authenticity as auth
from myna_viewer import card as mc
from tests.fakecard import (PIN, FakeCard, card_face, cv_certificate, face_fields, public, rsa_key, sign,
                            tlv)


def with_test_ca(test):
    """本物の J-LIS 鍵の代わりに、テスト用 CA 鍵を信頼の起点にする。"""
    return mock.patch.object(auth, 'CA_KEY', public(rsa_key('ca')))(test)


class Pkcs1Test(unittest.TestCase):
    def test_accepts_only_exact_encoding(self):
        key = rsa_key('issuer')
        di = auth.sha256_digest_info(auth.sha256(b'hello'))
        sig = sign(key, di)
        auth.verify_pkcs1(public(key), di, sig)
        with self.assertRaises(auth.AuthenticityError):
            auth.verify_pkcs1(public(key), auth.sha256_digest_info(auth.sha256(b'hellp')), sig)
        with self.assertRaises(auth.AuthenticityError):
            auth.verify_pkcs1(public(key), di, sig[:-1])
        with self.assertRaises(auth.AuthenticityError):
            auth.verify_pkcs1(public(key), di, (int.from_bytes(sig, 'big') + 1).to_bytes(256, 'big'))
        with self.assertRaises(auth.AuthenticityError):  # 法以上の値
            auth.verify_pkcs1(public(key), di, b'\xff' * 256)

    def test_digest_info_lengths(self):
        self.assertEqual(len(auth.sha256_digest_info(b'\xaa' * 32)), 51)
        three = auth.sha256_digest_info(b'\xaa' * 96)
        self.assertEqual(three[:2], b'\x30\x71')
        self.assertEqual(len(three), 115)

    def test_real_ca_key_shape(self):
        self.assertEqual(auth.CA_KEY.n.bit_length(), 2048)
        self.assertEqual(auth.CA_KEY_ID[:7], b'5000023')


class AuthenticityTest(unittest.TestCase):
    @with_test_ca
    def test_genuine_card(self):
        info = mc.read_visual_info(FakeCard(), PIN)
        self.assertTrue(info.authentic, info.auth_message)
        self.assertEqual(info.birth, '19850412')
        self.assertEqual(info.expire, '20340412')

    def test_rejected_with_real_ca_key(self):
        # テスト用 CA で作った証明書は、組み込みの J-LIS 鍵では通らない
        info = mc.read_visual_info(FakeCard(), PIN)
        self.assertFalse(info.authentic)
        self.assertIn('発行者証明書', info.auth_message)

    @with_test_ca
    def test_swapped_photo(self):
        card = rsa_key('card')

        def swap(fields):
            fields[0xDF27] = tlv(0xDF27, b'\x00\x00\x00\x0CjP  \r\n\x87\n' + b'\x01' * 2500)
        files = {b'\x00\x02': card_face(rsa_key('issuer'), card, tamper=swap),
                 b'\x00\x04': cv_certificate(rsa_key('ca'), rsa_key('issuer'))}
        info = mc.read_visual_info(FakeCard(files=files), PIN)
        self.assertFalse(info.authentic)
        self.assertIn('券面データの電子署名', info.auth_message)

    @with_test_ca
    def test_changed_birth_date(self):
        def older(fields):
            fields[0xDF22] = tlv(0xDF22, b'19650412')
        files = {b'\x00\x02': card_face(rsa_key('issuer'), rsa_key('card'), tamper=older),
                 b'\x00\x04': cv_certificate(rsa_key('ca'), rsa_key('issuer'))}
        info = mc.read_visual_info(FakeCard(files=files), PIN)
        self.assertFalse(info.authentic)

    @with_test_ca
    def test_self_signed_issuer(self):
        # 偽造者が自分の鍵で発行者証明書を作っても、CA 鍵が違うので通らない
        files = {b'\x00\x02': card_face(rsa_key('evil'), rsa_key('card')),
                 b'\x00\x04': cv_certificate(rsa_key('evil'), rsa_key('evil'))}
        info = mc.read_visual_info(FakeCard(files=files), PIN)
        self.assertFalse(info.authentic)
        self.assertIn('発行者証明書', info.auth_message)

    @with_test_ca
    def test_cloned_data_without_card_key(self):
        # 本物のデータ（本物のカード公開鍵入り）を複製し、カードの秘密鍵は持っていない偽カード
        files = {b'\x00\x02': card_face(rsa_key('issuer'), rsa_key('card')),
                 b'\x00\x04': cv_certificate(rsa_key('ca'), rsa_key('issuer'))}
        info = mc.read_visual_info(FakeCard(files=files, card_key=rsa_key('evil')), PIN)
        self.assertFalse(info.authentic)
        self.assertIn('実在', info.auth_message)

    @with_test_ca
    def test_card_refuses_to_sign(self):
        info = mc.read_visual_info(FakeCard(refuse_sign=True), PIN)
        self.assertFalse(info.authentic)

    @with_test_ca
    def test_missing_certificate_file(self):
        files = {b'\x00\x02': card_face(rsa_key('issuer'), rsa_key('card'))}
        info = mc.read_visual_info(FakeCard(files=files), PIN)
        self.assertFalse(info.authentic)

    @with_test_ca
    def test_duplicate_tag_is_not_authentic(self):
        # 署名済みの氏名の後ろに別の氏名を足し、表示と検証で別の値を見せる攻撃
        def dup(fields):
            fields[0xDF25] = fields[0xDF25] + tlv(0xDF25, b'OTHERNAME')
        files = {b'\x00\x02': card_face(rsa_key('issuer'), rsa_key('card'), tamper=dup),
                 b'\x00\x04': cv_certificate(rsa_key('ca'), rsa_key('issuer'))}
        info = mc.read_visual_info(FakeCard(files=files), PIN)
        self.assertFalse(info.authentic)

    @with_test_ca
    def test_challenge_is_fresh_and_from_app(self):
        c = FakeCard()
        mc.read_visual_info(c, PIN)
        mc.read_visual_info(c, PIN)
        signs = [a for a in c.sent if a[:2] == b'\x80\x2A']
        self.assertEqual(len(signs), 2)
        self.assertNotEqual(signs[0], signs[1])
        self.assertFalse(any(a[1] == 0x84 for a in c.sent))  # GET CHALLENGE は使わない

    def test_face_fields_helper_signs_three_groups(self):
        self.assertEqual(set(face_fields(rsa_key('card'))), {0xDF22, 0xDF23, 0xDF24, 0xDF25, 0xDF26, 0xDF27})


if __name__ == '__main__':
    unittest.main()
