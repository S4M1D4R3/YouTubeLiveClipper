# YouTube Live Clipper

YouTubeの動画から字幕を取得し、ChatGPTを使用して面白い部分を抽出・クリップするWebアプリケーションです。

## 必要条件

- Python 3.8以上
- FFmpeg
- インターネット接続

## セットアップ手順

1. FFmpegのインストール

   ### Windows
   ```bash
   # chocolateyを使用する場合
   choco install ffmpeg

   # または公式サイトからダウンロード:
   # https://ffmpeg.org/download.html
   ```

   ### macOS
   ```bash
   # Homebrewを使用する場合
   brew install ffmpeg
   ```

   ### Linux (Ubuntu/Debian)
   ```bash
   sudo apt update
   sudo apt install ffmpeg
   ```

2. Pythonの仮想環境を作成してアクティベート

   ```bash
   # 仮想環境の作成
   python -m venv .venv

   # 仮想環境のアクティベート
   ## Windows
   .venv\Scripts\activate
   ## macOS/Linux
   source .venv/bin/activate
   ```

3. 必要なパッケージのインストール

   ```bash
   pip install -r requirements.txt
   ```

4. アプリケーションの起動

   ```bash
   python app.py
   ```

   アプリケーションが起動したら、ブラウザで `http://localhost:8000` (または表示されたポート番号) にアクセスしてください。

## 使用方法

1. YouTubeのURLを入力フィールドに貼り付けます。
2. 「字幕を取得」ボタンをクリックします。
3. 生成されたプロンプトをコピーし、ChatGPTに貼り付けます。
4. ChatGPTの応答をコピーし、アプリケーションの「ChatGPTの応答を入力」フィールドに貼り付けます。
5. 「セグメントを抽出」ボタンをクリックすると、指定された部分の動画と字幕が切り出されます。
6. 各セグメントの「動画をダウンロード」ボタンから、切り出された動画をダウンロードできます。

## 注意事項

- 動画は最高品質でダウンロードされるため、処理に時間がかかる場合があります。
- 長い動画の場合、十分なディスク容量があることを確認してください。
- ダウンロードした動画は `downloads` フォルダに保存されます。

## ライセンス

© 2025, RegenRaum, SatsukiRain 