@echo off
rem MynaCardViewer.exe をビルドする（初回は仮想環境を作成）
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
    python -m venv .venv || exit /b 1
    .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
)
.venv\Scripts\python.exe -m unittest tests.test_card tests.test_authenticity || exit /b 1
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name MynaCardViewer main.py || exit /b 1
echo.
echo 完成: %~dp0dist\MynaCardViewer.exe
