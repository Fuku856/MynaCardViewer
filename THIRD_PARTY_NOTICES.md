# 第三者ソフトウェア・資料の表示

## 同梱しているソフトウェア
exe には OCR エンジン **Tesseract 5.5.3**（Apache License 2.0）と、その実行に必要なライブラリ
（Leptonica、ICU、libpng、libjpeg-turbo、libtiff、OpenJPEG、libwebp、zlib、libcurl、OpenSSL など。conda-forge のビルド）、
日本語学習データ jpn.traineddata（Apache License 2.0）を同梱しています。
各ライセンスの全文は、リリースに添付の `MynaCardViewer-<タグ>-licenses.zip`（`tools/fetch_tesseract.ps1` で作られる
`vendor/tesseract/licenses/`）に入っています。取得するパッケージと SHA-256 は `tools/tesseract-win-64.lock` のとおりです。

## 参考にした仕様・資料

このアプリは、次の MIT License のプロジェクトで公開されている仕様の調査結果と CA 公開鍵を参考に、Python で実装しています。

## soltia48/myna-card
https://github.com/soltia48/myna-card

参考にした内容: 券面事項確認AP の EF 0002 / EF 0004 の構造、券面データの署名方式（3 グループのハッシュ）、
CV 証明書の形式、署名コマンド (80 2A)、J-LIS の CA 公開鍵（証明者鍵ID 5000023）。
`myna_viewer/authenticity.py` と `myna_viewer/card.py` がこれに当たります。

```
MIT License

Copyright (c) 2026 KIRISHIKI Yudai

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## jpki/myna
https://github.com/jpki/myna

参考にした内容: 券面確認AP の AID、EF 0002 の項目の並び、READ BINARY の分割読み出し。

```
MIT License

Copyright (c) 2017 HAMANO Tsukasa <hamano@osstech.co.jp>
```
（全文は上記の MIT License と同じ）
