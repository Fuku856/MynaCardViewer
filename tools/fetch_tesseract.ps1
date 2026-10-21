# Tesseract (conda-forge) を取得して vendor/tesseract に必要なファイルだけを置く。
# - micromamba は GitHub の公式リリースから取得し、SHA-256 を照合してから使う
# - パッケージは tools/tesseract-win-64.lock（URL と SHA-256 を固定）のとおりに取得する（違えば micromamba が拒否）
# - PC へのインストールや設定の変更はしない（作業フォルダ .build-cache の中だけで完結）
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$cache = Join-Path $root '.build-cache'
$mmVersion = '2.9.0-0'
$mmSha256 = 'a6d804394b2418991c4e29562853eaace2f2ce9d9da661a98e74e02e8dbb44b0'
$mm = Join-Path $cache 'micromamba.exe'
$env_ = Join-Path $cache 'tesseract-env'

New-Item -ItemType Directory -Force $cache | Out-Null
if (-not (Test-Path $mm) -or (Get-FileHash $mm -Algorithm SHA256).Hash.ToLower() -ne $mmSha256) {
    $url = "https://github.com/mamba-org/micromamba-releases/releases/download/$mmVersion/micromamba-win-64.exe"
    Invoke-WebRequest -Uri $url -OutFile "$mm.download" -UseBasicParsing
    $hash = (Get-FileHash "$mm.download" -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $mmSha256) { Remove-Item "$mm.download"; throw "micromamba の SHA-256 が一致しません: $hash" }
    Move-Item -Force "$mm.download" $mm
}

if (Test-Path $env_) { Remove-Item -Recurse -Force $env_ }
& $mm create --no-rc -r (Join-Path $cache 'mamba-root') -p $env_ --file (Join-Path $PSScriptRoot 'tesseract-win-64.lock') --yes
if ($LASTEXITCODE -ne 0) { throw 'micromamba create に失敗しました' }

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
& $python (Join-Path $PSScriptRoot 'fetch_tesseract.py') $env_ (Join-Path $root 'vendor\tesseract')
if ($LASTEXITCODE -ne 0) { throw 'fetch_tesseract.py に失敗しました' }
