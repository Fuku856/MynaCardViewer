"""conda-forge から取得した Tesseract の環境から、実行に必要なファイルだけを vendor/tesseract に集める。

使い方: python tools/fetch_tesseract.py <conda 環境のフォルダ> [出力先 (既定: vendor/tesseract)]

- tesseract.exe が読み込む DLL を、PE のインポート表をたどって環境内から集める
  （Windows 標準の DLL は集めない）
- 日本語の学習データ jpn.traineddata だけを tessdata に入れる
"""
import json
import os
import shutil
import sys

import pefile

SYSTEM_DLL_PREFIXES = ('api-ms-win-', 'ext-ms-win-')


def imports(path):
    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
                                           pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT']])
    names = set()
    for attr in ('DIRECTORY_ENTRY_IMPORT', 'DIRECTORY_ENTRY_DELAY_IMPORT'):
        for entry in getattr(pe, attr, []):
            names.add(entry.dll.decode('ascii').lower())
    pe.close()
    return names


def closure(bin_dir, start):
    available = {f.lower(): f for f in os.listdir(bin_dir) if f.lower().endswith(('.dll', '.exe'))}
    todo, found = [start.lower()], set()
    while todo:
        name = todo.pop()
        if name in found or name not in available:
            continue  # 環境に無いもの = Windows 標準の DLL
        found.add(name)
        for dep in imports(os.path.join(bin_dir, available[name])):
            if not dep.startswith(SYSTEM_DLL_PREFIXES):
                todo.append(dep)
    return sorted(available[n] for n in found)


def copy_licenses(env, files, out):
    """同梱したファイルを含む conda パッケージのライセンス文書を licenses/<パッケージ名>/ に写す。"""
    wanted = {'library/bin/' + f.lower() for f in files} | {'share/tessdata/jpn.traineddata'}
    meta = os.path.join(env, 'conda-meta')
    for name in sorted(os.listdir(meta)):
        if not name.endswith('.json'):
            continue
        with open(os.path.join(meta, name), encoding='utf-8') as fp:
            pkg = json.load(fp)
        if not wanted & {p.replace('\\', '/').lower() for p in pkg.get('files', [])}:
            continue
        src = os.path.join(pkg.get('extracted_package_dir', ''), 'info', 'licenses')
        dst = os.path.join(out, '%s-%s' % (pkg['name'], pkg['version']))
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            os.makedirs(dst)
            with open(os.path.join(dst, 'LICENSE-NOT-FOUND.txt'), 'w', encoding='utf-8') as fp:
                fp.write('license: %s\n' % pkg.get('license', 'unknown'))


def main(env, out):
    bin_dir = os.path.join(env, 'Library', 'bin')
    files = closure(bin_dir, 'tesseract.exe')
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, 'tessdata'))
    for f in files:
        shutil.copy2(os.path.join(bin_dir, f), out)
    shutil.copy2(os.path.join(env, 'share', 'tessdata', 'jpn.traineddata'), os.path.join(out, 'tessdata'))
    copy_licenses(env, files, os.path.join(out, 'licenses'))
    total = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(out) for f in fs)
    print('copied %d files (%.1f MB) to %s' % (len(files) + 1, total / 1e6, out))
    for f in files:
        print('  ' + f)


if __name__ == '__main__':
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, 'vendor', 'tesseract'))
