@echo off
chcp 65001 > nul
echo ========================================
echo  アポ管理ツール セットアップ＆起動
echo ========================================

REM Python チェック
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [エラー] Pythonがインストールされていません。
    echo https://www.python.org/downloads/ からインストールしてください。
    echo インストール時に "Add Python to PATH" にチェックを入れてください。
    pause
    exit /b 1
)

REM 依存ライブラリのインストール
echo [1/2] ライブラリをインストール中...
pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo [エラー] ライブラリのインストールに失敗しました。
    pause
    exit /b 1
)

REM アプリ起動
echo [2/2] アプリを起動中...
echo.
echo  ブラウザで http://localhost:5000 を開いてください
echo  終了するにはこのウィンドウを閉じるか Ctrl+C を押してください
echo.
start http://localhost:5000
python app.py
pause
