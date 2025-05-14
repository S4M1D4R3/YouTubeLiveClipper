import os
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
import re
import json
from datetime import datetime
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs
import logging
import traceback
import requests
from pathlib import Path
import yt_dlp
import subprocess
import webbrowser
import threading
import time

# ロギングの設定
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)
socketio = SocketIO(app)

# ディレクトリの設定
DOWNLOADS_DIR = Path('downloads')
DOWNLOADS_DIR.mkdir(exist_ok=True)

def extract_video_id(url):
    """YouTubeのURLからビデオIDを抽出"""
    try:
        parsed_url = urlparse(url)
        if parsed_url.hostname in ['www.youtube.com', 'youtube.com']:
            if parsed_url.path == '/watch':
                return parse_qs(parsed_url.query).get('v', [None])[0]
            elif parsed_url.path.startswith(('/live/', '/shorts/')):
                return parsed_url.path.split('/')[-1]
        elif parsed_url.hostname == 'youtu.be':
            return parsed_url.path[1:]
        return None
    except Exception as e:
        logger.error(f"Error extracting video ID: {e}")
        return None

def get_video_info(video_id):
    """YouTubeの動画情報を取得"""
    try:
        # yt-dlpを使用して動画情報を取得
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return info.get('title', f'video_{video_id}')
    except Exception as e:
        logger.error(f"Error fetching video info: {e}")
        return f'video_{video_id}'

def format_time(seconds):
    """秒数を[HH:MM:SS]形式に変換"""
    try:
        total_seconds = int(float(seconds))
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"
    except Exception as e:
        logger.error(f"Error formatting time: {e}")
        return "[00:00:00]"

def clean_text(text):
    """字幕テキストのクリーニング"""
    try:
        # HTMLタグの削除
        text = re.sub(r'<[^>]+>', '', text)
        # 複数の空白を1つに
        text = re.sub(r'\s+', ' ', text)
        # 特殊文字の正規化
        text = text.replace('\u200b', '').replace('\ufeff', '')
        # 先頭と末尾の空白を削除
        return text.strip()
    except Exception as e:
        logger.error(f"Error cleaning text: {e}")
        return text

def get_subtitles_from_youtube_transcript_api(video_id):
    """YouTube Transcript APIを使用して字幕を取得"""
    try:
        # 言語優先順位
        lang_priority = ['ja', 'ja-JP', 'en']
        transcript = None
        errors = []

        for lang in lang_priority:
            try:
                transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[lang])
                logger.info(f"Successfully fetched transcript in {lang}")
                return transcript, None
            except (NoTranscriptFound, TranscriptsDisabled) as e:
                errors.append(f"{lang}: {str(e)}")
                continue
            except Exception as e:
                errors.append(f"{lang}: Unexpected error - {str(e)}")
                continue

        return None, f"字幕の取得に失敗しました: {'; '.join(errors)}"
    except Exception as e:
        logger.error(f"Error in get_subtitles_from_youtube_transcript_api: {e}")
        return None, str(e)

def get_subtitles_from_yt_dlp(video_id):
    """yt-dlpを使用して字幕を取得"""
    try:
        ydl_opts = {
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['ja', 'ja-JP', 'en'],
            'skip_download': True,
            'quiet': True,
            'no_warnings': True,
            'subtitlesformat': 'vtt'
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            subtitles = info.get('subtitles', {})
            automatic_captions = info.get('automatic_captions', {})
            
            # 優先順位に従って字幕を探す
            for lang in ['ja', 'ja-JP', 'en']:
                if lang in subtitles:
                    return subtitles[lang], None
                if lang in automatic_captions:
                    return automatic_captions[lang], None
                    
        return None, "字幕が見つかりませんでした"
    except Exception as e:
        logger.error(f"Error in get_subtitles_from_yt_dlp: {e}")
        return None, str(e)

def download_video_and_subtitles(url):
    """YouTubeの動画から字幕を取得（複数の方法を試行）"""
    try:
        # 進捗状況を0%に設定
        socketio.emit('progress_update', {'task': 'subtitles', 'progress': 0})
        
        video_id = extract_video_id(url)
        if not video_id:
            logger.error("Invalid YouTube URL")
            return None, "無効なYouTube URLです。", None, None

        # 動画情報の取得
        video_title = get_video_info(video_id)
        socketio.emit('progress_update', {'task': 'subtitles', 'progress': 20})
        
        # 方法1: YouTube Transcript APIを使用
        logger.info("Trying YouTube Transcript API...")
        transcript, error1 = get_subtitles_from_youtube_transcript_api(video_id)
        socketio.emit('progress_update', {'task': 'subtitles', 'progress': 40})
        
        if transcript:
            formatted_subtitles = []
            current_text = []
            current_time = None
            
            for item in transcript:
                time = format_time(item['start'])
                text = clean_text(item['text'])
                
                if not text:
                    continue
                    
                if current_time == time:
                    current_text.append(text)
                else:
                    if current_time and current_text:
                        formatted_subtitles.append(f"{current_time} {' '.join(current_text)}")
                    current_time = time
                    current_text = [text]
            
            # 最後の字幕を追加
            if current_time and current_text:
                formatted_subtitles.append(f"{current_time} {' '.join(current_text)}")
            
            socketio.emit('progress_update', {'task': 'subtitles', 'progress': 80})
        else:
            # 方法2: yt-dlpを使用
            logger.info("Trying yt-dlp...")
            transcript, error2 = get_subtitles_from_yt_dlp(video_id)
            
            if not transcript:
                error_msg = f"字幕の取得に失敗しました。\nMethod 1: {error1}\nMethod 2: {error2}"
                logger.error(error_msg)
                return None, error_msg, video_title, None
                
            formatted_subtitles = []
            for item in transcript:
                time = format_time(item.get('start', 0))
                text = clean_text(item.get('text', ''))
                if text:
                    formatted_subtitles.append(f"{time} {text}")

        if not formatted_subtitles:
            logger.error("No subtitle content extracted")
            return None, "字幕の内容を抽出できませんでした。", video_title, None
        
        # 字幕ファイルを保存
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', video_title)
        subtitle_output = DOWNLOADS_DIR / f"{safe_title}_subtitles.txt"
        
        with open(subtitle_output, 'w', encoding='utf-8') as f:
            f.write('\n'.join(formatted_subtitles))
        
        logger.info(f"Successfully saved subtitles to {subtitle_output}")
        socketio.emit('progress_update', {'task': 'subtitles', 'progress': 100})
        return formatted_subtitles, None, video_title, str(subtitle_output)
            
    except Exception as e:
        error_msg = f"エラーが発生しました: {str(e)}"
        logger.error(error_msg)
        logger.error(traceback.format_exc())
        return None, error_msg, None, None

def create_gpt_prompt(subtitles, video_title):
    """ChatGPT用のプロンプトを作成"""
    json_format = '''[
    {
        "title": "セグメントのタイトル",
        "start": "HH:MM:SS",
        "end": "HH:MM:SS",
        "impact": 整数値(1-10),
        "uniqueness": 整数値(1-10),
        "timeliness": 整数値(1-10),
        "entertainment": 整数値(1-10),
        "reason": "選択理由（100-500文字）"
    }
]'''

    base_prompt = f"""あなたの回答は必ずJSONフォーマットで出力してください。説明文や追加のコメントは一切含めないでください。

以下の動画「{video_title}」の字幕データから、切り抜きに適した面白い部分を特定し、JSONで出力してください。

評価基準:
1. インパクト (1-10):
   - 視聴者の感情を強く揺さぶる場面
   - 予想外の展開や驚きのある瞬間
   - 強い印象を残す発言や行動

2. 独自性 (1-10):
   - 一般的な意見や常識とは異なる視点
   - ユニークな体験や独特な解釈
   - オリジナリティのある表現や説明方法

3. 時事性 (1-10):
   - 現在のトレンドや話題との関連性
   - 最新のニュースや出来事への言及
   - 社会的な議論や関心事との接点

4. エンターテイメント性 (1-10):
   - 笑いを誘う要素
   - ドラマチックな展開
   - 視聴者の興味を引く話題性

##
{chr(10).join(subtitles)}
##

出力形式: 必ずJSONで出力してください。以下の形式以外の文章は含めないでください。
{json_format}

制約条件:
1. 必ずJSONとして有効な形式で出力すること
2. 各セグメントは30秒以上、10分以内に収めること
3. 最大5つのセグメントまで選択可能
4. 各評価基準は必ず1から10までの整数値で評価すること
5. セグメントの開始・終了時間は字幕のタイミングに合わせること
6. JSON以外の説明文やコメントを含めないこと
7. 理由は100文字以上、500文字以内で説明すること
8. タイトルは簡潔で内容を表すものにすること

最後の注意: 回答は上記のJSONフォーマットのみとし、それ以外の文章は一切含めないでください。"""
    
    return base_prompt

def filter_subtitles_by_time(subtitles, start_time, end_time):
    """指定された時間範囲内の字幕を抽出"""
    filtered = []
    try:
        def time_to_seconds(time_str):
            h, m, s = map(int, time_str.split(':'))
            return h * 3600 + m * 60 + s

        start_seconds = time_to_seconds(start_time)
        end_seconds = time_to_seconds(end_time)

        for line in subtitles:
            if not line.strip():
                continue
            
            # [HH:MM:SS]形式から時間を抽出
            match = re.match(r'\[(\d{2}):(\d{2}):(\d{2})\](.*)', line)
            if match:
                h, m, s, text = match.groups()
                subtitle_seconds = time_to_seconds(f"{h}:{m}:{s}")
                
                if start_seconds <= subtitle_seconds <= end_seconds:
                    filtered.append(line)

        logger.info(f"Filtered subtitles: {len(filtered)} lines between {start_time} and {end_time}")
        return filtered
    except Exception as e:
        logger.error(f"Error in filter_subtitles_by_time: {e}")
        logger.error(traceback.format_exc())
        return []

def extract_segments(gpt_response, subtitle_file, video_url):
    """ChatGPTのレスポンスから動画セグメントを抽出"""
    try:
        # 進捗状況を0%に設定
        socketio.emit('progress_update', {'task': 'video', 'progress': 0})
        
        # JSONデータのパース
        segments = json.loads(gpt_response)
        if not segments or not isinstance(segments, list):
            logger.error("Invalid segments data: Expected a non-empty array")
            return None

        video_id = extract_video_id(video_url)
        if not video_id:
            logger.error("Invalid video URL")
            return None

        # 元の字幕ファイルを読み込む
        with open(subtitle_file, 'r', encoding='utf-8') as f:
            all_subtitles = f.read().splitlines()

        # 動画全体を一度だけダウンロード
        temp_filename = f"temp_{video_id}.mp4"
        temp_path = DOWNLOADS_DIR / temp_filename

        # yt-dlpのオプション設定
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
            'outtmpl': str(temp_path),
            'quiet': True,
            'no_warnings': True,
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4'
            }],
            'merge_output_format': 'mp4'
        }

        # 動画全体をダウンロード（まだ存在しない場合のみ）
        if not temp_path.exists():
            logger.info("Downloading full video in highest quality...")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        
        socketio.emit('progress_update', {'task': 'video', 'progress': 40})

        results = []
        total_segments = len(segments)
        for i, segment in enumerate(segments, 1):
            try:
                # 進捗状況を更新（40%から90%の間で分配）
                progress = 40 + (50 * i / total_segments)
                socketio.emit('progress_update', {'task': 'video', 'progress': int(progress)})
                
                # 必須フィールドの検証
                required_fields = ['start', 'end', 'impact', 'uniqueness', 'timeliness', 'entertainment', 'reason']
                if not all(field in segment for field in required_fields):
                    logger.error(f"Missing required fields in segment: {segment}")
                    continue

                # 数値フィールドの検証と正規化
                score_fields = ['impact', 'uniqueness', 'timeliness', 'entertainment']
                normalized_scores = {}
                for field in score_fields:
                    try:
                        score = float(segment[field])
                        if not (1 <= score <= 10):
                            score = max(1, min(10, score))
                        normalized_scores[field] = int(round(score))
                    except (ValueError, TypeError):
                        logger.error(f"Invalid score value for {field}: {segment[field]}")
                        normalized_scores[field] = 5

                # 時間形式の検証と正規化
                try:
                    start_time = segment['start'].strip()
                    end_time = segment['end'].strip()
                    
                    # HH:MM:SS形式に正規化
                    time_pattern = re.compile(r'^(\d{1,2}):?(\d{2}):?(\d{2})$')
                    
                    start_match = time_pattern.match(start_time)
                    end_match = time_pattern.match(end_time)
                    
                    if not (start_match and end_match):
                        logger.error(f"Invalid time format: start={start_time}, end={end_time}")
                        continue
                    
                    # 正規化された時間形式
                    start_time = f"{int(start_match.group(1)):02d}:{start_match.group(2)}:{start_match.group(3)}"
                    end_time = f"{int(end_match.group(1)):02d}:{end_match.group(2)}:{end_match.group(3)}"

                    # 時間を秒数に変換
                    def time_to_seconds(time_str):
                        h, m, s = map(int, time_str.split(':'))
                        return h * 3600 + m * 60 + s

                    start_seconds = time_to_seconds(start_time)
                    end_seconds = time_to_seconds(end_time)
                    duration = end_seconds - start_seconds

                    # 指定された時間範囲の字幕を抽出
                    segment_subtitles = filter_subtitles_by_time(all_subtitles, start_time, end_time)
                    
                    # 字幕ファイルを保存
                    segment_subtitle_filename = f"clip_{video_id}_{start_time.replace(':', '_')}_{end_time.replace(':', '_')}_subtitles.txt"
                    segment_subtitle_path = DOWNLOADS_DIR / segment_subtitle_filename
                    with open(segment_subtitle_path, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(segment_subtitles))

                    # セグメントの切り出し
                    output_filename = f"clip_{video_id}_{start_time.replace(':', '_')}_{end_time.replace(':', '_')}.mp4"
                    output_path = DOWNLOADS_DIR / output_filename

                    # FFmpegで動画を切り出し（高品質設定）
                    logger.info(f"Cutting video segment {start_time} - {end_time} with high quality settings...")
                    ffmpeg_cmd = [
                        'ffmpeg',
                        '-i', str(temp_path),
                        '-ss', str(start_seconds),
                        '-t', str(duration),
                        '-c:v', 'libx264',
                        '-preset', 'slow',
                        '-crf', '18',
                        '-c:a', 'aac',
                        '-b:a', '192k',
                        '-avoid_negative_ts', '1',
                        '-y',
                        str(output_path)
                    ]

                    result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        logger.error(f"FFmpeg error: {result.stderr}")
                        continue

                    results.append({
                        'start_time': start_time,
                        'end_time': end_time,
                        'video_title': os.path.basename(str(subtitle_file)).replace('_subtitles.txt', ''),
                        'subtitle_file': str(segment_subtitle_path),
                        'video_file': str(output_path),
                        'title': segment.get('title', f'切り抜き {len(results) + 1}'),
                        'impact': normalized_scores['impact'],
                        'uniqueness': normalized_scores['uniqueness'],
                        'timeliness': normalized_scores['timeliness'],
                        'entertainment': normalized_scores['entertainment'],
                        'reason': str(segment['reason']).strip() if len(str(segment['reason']).strip()) >= 10 else "理由が十分に説明されていません。"
                    })

                except Exception as e:
                    logger.error(f"Error processing time format: {e}")
                    continue

            except Exception as e:
                logger.error(f"Error processing segment: {e}")
                logger.error(traceback.format_exc())
                continue

        # 全てのセグメントの処理が完了したら一時ファイルを削除
        if temp_path.exists():
            logger.info("Cleaning up temporary file...")
            temp_path.unlink()

        if not results:
            logger.error("No valid segments found")
            return None

        # 進捗状況を100%に設定
        socketio.emit('progress_update', {'task': 'video', 'progress': 100})
        return results

    except json.JSONDecodeError as e:
        logger.error(f"JSON parsing error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in extract_segments: {e}")
        logger.error(traceback.format_exc())
        # エラーが発生した場合も一時ファイルを削除
        if 'temp_path' in locals() and temp_path.exists():
            temp_path.unlink()
        return None

@app.route('/')
def index():
    """メインページのHTML"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>YouTube Live Clipper</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;700&display=swap" rel="stylesheet">
        <style>
            :root {
                --primary-color: #FF8A8A;
                --background-color: #F4F5F7;
                --accent-light: #F0EAAC;
                --accent-dark: #CCE0AC;
                --text-color: #2D3436;
                --shadow-color: rgba(0, 0, 0, 0.1);
                --neu-light: #FFFFFF;
                --neu-dark: rgba(0, 0, 0, 0.1);
                --button-primary: #4A90E2;
                --button-secondary: #82B1FF;
            }

            body {
                font-family: 'Noto Sans JP', sans-serif;
                line-height: 1.6;
                color: var(--text-color);
                background: var(--background-color);
                padding: 2rem;
                min-height: 100vh;
            }

            .button-group {
                display: flex;
                gap: 1rem;
                margin-top: 1.5rem;
            }

            .download-button {
                padding: 0.8rem 1.5rem;
                border: none;
                border-radius: 12px;
                background: var(--button-primary);
                color: white;
                font-weight: 600;
                text-decoration: none;
                display: inline-flex;
                align-items: center;
                gap: 0.5rem;
                transition: all 0.3s ease;
                box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            }

            .download-button:hover {
                transform: translateY(-2px);
                box-shadow: 0 6px 8px rgba(0, 0, 0, 0.15);
            }

            .download-button.secondary {
                background: var(--button-secondary);
            }

            .card {
                background: white;
                border-radius: 20px;
                padding: 2rem;
                margin-bottom: 2rem;
                box-shadow: 0 8px 16px rgba(0, 0, 0, 0.1);
            }

            .card-title {
                color: var(--primary-color);
                font-size: 1.5rem;
                margin-bottom: 1rem;
                font-weight: 600;
            }

            .time-info {
                background: var(--accent-light);
                padding: 0.5rem 1rem;
                border-radius: 8px;
                display: inline-block;
                margin-bottom: 1rem;
            }

            .scores {
                display: flex;
                flex-wrap: wrap;
                gap: 0.8rem;
                margin: 1rem 0;
            }

            .score-badge {
                padding: 0.5rem 1rem;
                border-radius: 8px;
                font-weight: 500;
                background: var(--accent-dark);
                color: var(--text-color);
            }

            .container {
                max-width: 1200px;
                margin: 0 auto;
                padding: 2rem;
                position: relative;
            }

            .copyright {
                position: fixed;
                bottom: 1rem;
                right: 1rem;
                font-size: 0.8rem;
                color: var(--text-color);
                opacity: 0.7;
            }

            .neu-box {
                border-radius: 25px;
                background: rgba(255, 255, 255, 0.9);
                box-shadow: 8px 8px 16px var(--neu-dark),
                           -8px -8px 16px var(--neu-light);
                padding: 2.5rem;
                margin: 0 auto 2.5rem;
                max-width: calc(100% - 5rem);  /* コンテナの左右マージンを考慮 */
                transition: all 0.3s ease;
            }

            .prompt-container {
                max-height: 400px;
                overflow-y: auto;
                padding: 1rem;
                border-radius: 15px;
                background: rgba(255, 255, 255, 0.8);
                margin-bottom: 1rem;
            }

            .chatgpt-link {
                display: inline-block;
                margin-bottom: 1rem;
                color: var(--primary-color);
                text-decoration: none;
                font-weight: 600;
                transition: all 0.3s ease;
            }

            .chatgpt-link:hover {
                color: var(--accent-color);
                transform: translateY(-2px);
            }

            .loading {
                display: none;
                text-align: center;
                margin: 2rem 0;
            }

            .loading-spinner {
                width: 60px;
                height: 60px;
                border: 5px solid var(--secondary-color);
                border-top: 5px solid var(--primary-color);
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin: 0 auto;
            }

            .loading-text {
                margin-top: 1rem;
                color: var(--text-color);
                font-weight: 500;
            }

            .progress-container {
                width: 100%;
                height: 4px;
                background: var(--secondary-color);
                margin-top: 1rem;
                border-radius: 2px;
                overflow: hidden;
            }

            .progress-bar {
                height: 100%;
                width: 0;
                background: var(--primary-color);
                transition: width 0.3s ease;
            }

            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }

            @keyframes pulse {
                0% { transform: scale(1); }
                50% { transform: scale(1.05); }
                100% { transform: scale(1); }
            }

            .processing {
                animation: pulse 2s infinite;
            }

            .neu-input {
                width: 100%;
                padding: 1.2rem;
                border: none;
                border-radius: 15px;
                background: var(--background-color);
                box-shadow: inset 6px 6px 12px var(--neu-dark),
                          inset -6px -6px 12px var(--neu-light);
                color: var(--text-color);
                font-size: 1.1rem;
                margin-bottom: 1.5rem;
                transition: all 0.3s ease;
                box-sizing: border-box;  /* パディングを幅に含める */
            }

            textarea.neu-input {
                min-height: 200px;
                resize: vertical;
                width: 100%;
                display: block;
                margin: 1.5rem 0;
            }

            .neu-button {
                padding: 1.2rem 2.5rem;
                border: none;
                border-radius: 15px;
                background: linear-gradient(145deg, var(--background-color), var(--neu-light));
                box-shadow: 8px 8px 16px var(--neu-dark),
                          -8px -8px 16px var(--neu-light);
                color: var(--primary-color);
                font-size: 1.1rem;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s ease;
                margin: 1.5rem 0;
                position: relative;
                overflow: hidden;
            }

            .neu-button:hover {
                box-shadow: 6px 6px 12px var(--neu-dark),
                          -6px -6px 12px var(--neu-light);
                transform: translateY(-2px);
                color: var(--primary-hover);
            }

            .neu-button:active {
                box-shadow: inset 6px 6px 12px var(--neu-dark),
                          inset -6px -6px 12px var(--neu-light);
                transform: translateY(0);
            }

            .error-message {
                display: none;
                color: var(--danger-color);
                background: var(--background-color);
                box-shadow: inset 6px 6px 12px var(--neu-dark),
                          inset -6px -6px 12px var(--neu-light);
                border-radius: 15px;
                padding: 1.2rem;
                margin: 1.5rem 0;
                font-size: 1rem;
            }

            .results {
                margin-top: 3rem;
            }

            .clip-item {
                margin-bottom: 2.5rem;
                transition: all 0.3s ease;
                border-radius: 20px;
                padding: 2rem;
                background: var(--background-color);
                box-shadow: 8px 8px 16px var(--neu-dark),
                          -8px -8px 16px var(--neu-light);
            }

            .clip-item:hover {
                transform: translateY(-3px);
                box-shadow: 10px 10px 20px var(--neu-dark),
                          -10px -10px 20px var(--neu-light);
            }

            .copy-button {
                background: var(--background-color);
                border: none;
                border-radius: 12px;
                padding: 0.8rem 1.5rem;
                color: var(--primary-color);
                font-weight: 600;
                cursor: pointer;
                box-shadow: 6px 6px 12px var(--neu-dark),
                          -6px -6px 12px var(--neu-light);
                transition: all 0.3s ease;
            }

            .copy-button:hover {
                transform: translateY(-2px);
                box-shadow: 8px 8px 16px var(--neu-dark),
                          -8px -8px 16px var(--neu-light);
            }

            .copy-button:active {
                transform: translateY(0);
                box-shadow: inset 4px 4px 8px var(--neu-dark),
                          inset -4px -4px 8px var(--neu-light);
            }

            /* 入力フォームのコンテナスタイルを追加 */
            .input-container {
                width: 100%;
                max-width: calc(100% - 5rem);  /* コンテナの左右マージンを考慮 */
                margin: 0 auto;
            }

            .video-processing {
                margin-top: 1.5rem;
                text-align: center;
            }

            .video-processing .loading-text {
                margin: 1rem 0;
                color: var(--text-color);
                font-weight: 500;
            }

            .video-processing .progress-container {
                width: 100%;
                height: 4px;
                background: var(--secondary-color);
                margin-top: 0.5rem;
                border-radius: 2px;
                overflow: hidden;
            }

            .video-processing .progress-bar {
                height: 100%;
                width: 0;
                background: var(--primary-color);
                transition: width 0.3s ease;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>YouTube Live Clipper</h1>
            
            <div class="neu-box">
                <form id="urlForm">
                    <input type="text" name="url" class="neu-input" placeholder="YouTube URLを入力してください" required>
                    <button type="submit" class="neu-button">字幕を取得</button>
                </form>
            </div>

            <div class="loading">
                <div class="loading-spinner"></div>
                <p class="loading-text">処理中...</p>
                <div class="progress-container">
                    <div class="progress-bar"></div>
                </div>
            </div>

            <div id="resultCard" class="neu-box" style="display: none;">
                <h2>ChatGPT用プロンプト</h2>
                <a href="https://chat.openai.com/" target="_blank" class="chatgpt-link">ChatGPTで開く →</a>
                <div class="prompt-container">
                    <pre id="promptResult" style="white-space: pre-wrap;"></pre>
                </div>
                <button onclick="copyToClipboard()" class="copy-button">コピー</button>
            </div>

            <div id="gptResponseCard" class="neu-box" style="display: none;">
                <h2>ChatGPTの応答を入力</h2>
                <div class="input-container">
                    <textarea id="gptResponseInput" class="neu-input" placeholder="ChatGPTの応答をここに貼り付けてください"></textarea>
                    <button onclick="processGptResponse()" class="neu-button">セグメントを抽出</button>
                    <div class="video-processing" style="display: none;">
                        <p class="loading-text">動画を処理中...</p>
                        <div class="progress-container">
                            <div class="progress-bar"></div>
                        </div>
                    </div>
                </div>
            </div>

            <div id="segmentsContainer"></div>
            
            <div class="copyright">© 2025, RegenRaum, SatsukiRain</div>
        </div>

        <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
        <script>
            let currentVideoUrl = '';
            let currentSubtitleFile = '';
            let processingState = '';
            let socket = io();

            // WebSocketからの進捗状況更新を処理
            socket.on('progress_update', function(data) {
                const loading = document.querySelector('.loading');
                const videoProcessing = document.querySelector('.video-processing');
                
                if (data.task === 'subtitles') {
                    // 字幕取得の進捗を更新
                    loading.style.display = 'block';
                    const progressBar = loading.querySelector('.progress-bar');
                    const loadingText = loading.querySelector('.loading-text');
                    
                    loadingText.textContent = `字幕を取得中... ${data.progress}%`;
                    progressBar.style.width = `${data.progress}%`;
                } else if (data.task === 'video') {
                    // 動画処理の進捗を更新
                    videoProcessing.style.display = 'block';
                    const progressBar = videoProcessing.querySelector('.progress-bar');
                    const loadingText = videoProcessing.querySelector('.loading-text');
                    
                    loadingText.textContent = `動画を処理中... ${data.progress}%`;
                    progressBar.style.width = `${data.progress}%`;
                }
            });

            function updateProgress(state, percent) {
                const loading = document.querySelector('.loading');
                loading.style.display = 'block';
            }

            document.getElementById('urlForm').addEventListener('submit', async (e) => {
                e.preventDefault();
                
                const loading = document.querySelector('.loading');
                const resultCard = document.getElementById('resultCard');
                const gptResponseCard = document.getElementById('gptResponseCard');
                const promptResult = document.getElementById('promptResult');
                const segmentsContainer = document.getElementById('segmentsContainer');
                
                loading.style.display = 'block';
                resultCard.style.display = 'none';
                gptResponseCard.style.display = 'none';
                segmentsContainer.innerHTML = '';
                
                try {
                    const formData = new FormData(e.target);
                    currentVideoUrl = formData.get('url');
                    
                    const response = await fetch('/process', {
                        method: 'POST',
                        body: formData
                    });
                    
                    const data = await response.json();
                    
                    if (data.error) {
                        alert(data.error);
                        return;
                    }
                    
                    currentSubtitleFile = data.subtitle_file;
                    promptResult.textContent = data.prompt;
                    resultCard.style.display = 'block';
                    gptResponseCard.style.display = 'block';
                } catch (error) {
                    alert('エラーが発生しました: ' + error.message);
                } finally {
                    loading.style.display = 'none';
                }
            });

            async function processGptResponse() {
                const videoProcessing = document.querySelector('.video-processing');
                const segmentsContainer = document.getElementById('segmentsContainer');
                const gptResponse = document.getElementById('gptResponseInput').value;
                
                if (!gptResponse) {
                    alert('ChatGPTの応答を入力してください。');
                    return;
                }
                
                videoProcessing.style.display = 'block';
                
                try {
                    const response = await fetch('/extract', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            gpt_response: gptResponse,
                            video_url: currentVideoUrl,
                            subtitle_file: currentSubtitleFile
                        })
                    });
                    
                    const data = await response.json();
                    
                    if (data.error) {
                        alert(data.error);
                        return;
                    }
                    
                    segmentsContainer.innerHTML = '';
                    data.segments.forEach((segment, index) => {
                        const card = document.createElement('div');
                        card.className = 'card segment-card';
                        
                        // ファイル名の取得
                        const subtitleFileName = segment.subtitle_file.split('/').pop();
                        const videoFileName = segment.video_file.split('/').pop();
                        
                        card.innerHTML = `
                            <div class="card-body">
                                <h5 class="card-title">${segment.title}</h5>
                                <p class="time-info">⏱ ${segment.start_time} - ${segment.end_time}</p>
                                <div class="scores">
                                    <span class="score-badge">インパクト: ${segment.impact}</span>
                                    <span class="score-badge">独自性: ${segment.uniqueness}</span>
                                    <span class="score-badge">時事性: ${segment.timeliness}</span>
                                    <span class="score-badge">エンターテイメント性: ${segment.entertainment}</span>
                                </div>
                                <p class="card-text">${segment.reason}</p>
                                <div class="button-group">
                                    <a href="/download/subtitle/${encodeURIComponent(subtitleFileName)}" 
                                       class="download-button secondary" 
                                       download="${subtitleFileName}">
                                       <span>📝</span>字幕をダウンロード
                                    </a>
                                    <a href="/download/video/${encodeURIComponent(videoFileName)}" 
                                       class="download-button" 
                                       download="${videoFileName}">
                                       <span>🎬</span>動画をダウンロード
                                    </a>
                                </div>
                            </div>
                        `;
                        segmentsContainer.appendChild(card);
                    });
                } catch (error) {
                    alert('エラーが発生しました: ' + error.message);
                } finally {
                    videoProcessing.style.display = 'none';
                }
            }

            function copyToClipboard() {
                const promptResult = document.getElementById('promptResult');
                navigator.clipboard.writeText(promptResult.textContent)
                    .then(() => {
                        const copyButton = document.querySelector('.copy-button');
                        copyButton.textContent = 'コピーしました！';
                        setTimeout(() => {
                            copyButton.textContent = 'コピー';
                        }, 2000);
                    })
                    .catch(err => alert('コピーに失敗しました: ' + err));
            }
        </script>
    </body>
    </html>
    """

@app.route('/process', methods=['POST'])
def process_url():
    url = request.form.get('url')
    if not url:
        return jsonify({'error': 'URLが提供されていません。'})
    
    # 動画情報と字幕を取得
    subtitles, error, video_title, subtitle_file = download_video_and_subtitles(url)
    if error:
        return jsonify({'error': error})
    
    # プロンプトを生成
    prompt = create_gpt_prompt(subtitles, video_title)
    
    return jsonify({
        'success': True,
        'prompt': prompt,
        'video_title': video_title,
        'subtitle_file': subtitle_file
    })

@app.route('/extract', methods=['POST'])
def extract_clips():
    data = request.get_json()
    gpt_response = data.get('gpt_response')
    video_url = data.get('video_url')
    subtitle_file = data.get('subtitle_file')
    
    if not all([gpt_response, video_url, subtitle_file]):
        return jsonify({'error': '必要な情報が不足しています。'})
    
    segments = extract_segments(gpt_response, subtitle_file, video_url)
    if segments is None:
        return jsonify({'error': 'セグメントの抽出に失敗しました。'})
    
    return jsonify({
        'success': True,
        'segments': segments
    })

@app.route('/download/subtitle/<path:filename>')
def download_subtitle(filename):
    """字幕ファイルのダウンロード"""
    try:
        file_path = DOWNLOADS_DIR / filename
        if not file_path.exists():
            return jsonify({'error': 'ファイルが見つかりません。'}), 404

        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='text/plain'
        )
    except Exception as e:
        logger.error(f"Error downloading subtitle: {e}")
        return jsonify({'error': 'ファイルのダウンロードに失敗しました。'}), 404

@app.route('/download/video/<path:filename>')
def download_video(filename):
    """動画ファイルのダウンロード"""
    try:
        file_path = DOWNLOADS_DIR / filename
        if not file_path.exists():
            return jsonify({'error': 'ファイルが見つかりません。'}), 404

        return send_file(
            file_path,
            as_attachment=True,
            download_name=filename,
            mimetype='video/mp4'
        )
    except Exception as e:
        logger.error(f"Error downloading video: {e}")
        return jsonify({'error': 'ファイルのダウンロードに失敗しました。'}), 404

def cleanup_port(port):
    """使用中のポートをクリーンアップ"""
    try:
        import psutil
        
        for proc in psutil.process_iter(['pid', 'name', 'connections']):
            try:
                # プロセスの接続情報を取得
                connections = proc.connections()
                for conn in connections:
                    if hasattr(conn, 'laddr') and conn.laddr.port == port:
                        # プロセスを終了
                        psutil.Process(proc.pid).terminate()
                        logger.info(f"Terminated process {proc.pid} using port {port}")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except Exception as e:
        logger.error(f"Error cleaning up port: {e}")

def find_available_port(start_port=8000, max_port=9000):
    """利用可能なポートを探す"""
    import socket
    
    for port in range(start_port, max_port):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('', port))
                logger.info(f"Found available port: {port}")
                return port
        except OSError:
            continue
    
    raise RuntimeError("No available ports found")

def open_browser(port):
    """指定されたポートでブラウザを開く"""
    # サーバーの起動を少し待つ
    time.sleep(3.0)
    webbrowser.open(f'http://localhost:{port}')

if __name__ == '__main__':
    try:
        # 環境変数をチェックしてメインプロセスかどうかを判断
        if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
            port = find_available_port()
            cleanup_port(port)
            logger.info(f"Starting application on port {port}")
            
            # ブラウザを開くスレッドを起動（メインプロセスのみ）
            threading.Thread(target=open_browser, args=(port,)).start()
        else:
            # リローダープロセスの場合は既存のポートを使用
            port = find_available_port()
            
        # アプリケーションを起動
        socketio.run(app, debug=True, port=port, host='0.0.0.0')
    except Exception as e:
        logger.error(f"Error starting application: {e}")
        logger.error(traceback.format_exc()) 