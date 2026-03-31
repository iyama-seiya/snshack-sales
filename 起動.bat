@echo off
echo ライブラリをインストール中...
python -m pip install flask anthropic -q
echo 起動中... ブラウザで http://localhost:5000 を開いてください
start http://localhost:5000
python app.py
pause
