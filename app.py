import streamlit as st
import threading
import time
import datetime
import os
import logging
import random
from typing import Optional

from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import google.generativeai as genai
from dotenv import load_dotenv

# --- 初期設定 ---
# .envファイルから環境変数を読み込み
load_dotenv()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OAUTH_FILE = os.getenv("OAUTH_FILE", "client_secret.json")
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Gemini API設定
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-1.5-flash")

# Streamlitページ設定
st.set_page_config(page_title="YouTubeチャットBot", layout="wide")

# --- AIペルソナ定義 ---
PERSONAS = {
    "デフォルト": "あなたはライブ配信を盛り上げる、親切でフレンドリーなアシスタントAIです。",
    "原神": "あなたは原神の世界『テイワット』から来た知識豊富な冒険者パイモンのようなAIです。元気で少し食いしん坊な口調で、初心者へのアドバイスやゲーム内のネタを交えながらコメントに答えてください。",
    "鳴潮": "あなたは未来的な世界観を持つ『鳴潮』の冷静沈着な分析官AIです。専門用語を少し交えつつ、的確でクールな口調で、戦略的なアドバイスや世界観に関する考察でコメントに応答してください。",
    "ゼンレスゾーンゼロ": "あなたは『ゼンレスゾーンゼロ』のストリートカルチャーに詳しいエージェントAIです。ヒップホップのスラングやノリの良い言葉を使い、スタイリッシュで都会的な雰囲気のコメントを返してください。",
    "Fortnite": "あなたはFortniteの建築マスター兼バトル戦術家のAIです。建築バトルや武器のメタ情報に詳しく、視聴者と一緒にビクロイを目指すような、エネルギッシュで競争的なコメントを返してください。",
    "Dead by Daylight": "あなたはDead by DaylightのベテランサバイバーのようなAIです。少し怖がりながらも、キラーの対策やパーク構成、脱出のコツなどを、仲間と協力するような親しみやすい口調でコメントしてください。",
    "ヒロアカウルトラランブル": "あなたは『僕のヒーローアカデミア ULTRA RUMBLE』のヒーローのようなAIです。勇ましく熱血な口調で、個性や戦術についてアドバイスし、視聴者と一緒にヴィランを倒す雰囲気でコメントしてください。",
    "バイオハザード": "あなたはバイオハザードシリーズの熟練サバイバーのようなAIです。サバイバルホラーの雰囲気を大切にしつつ、冷静で慎重なアドバイスや武器・アイテムの使い方などを提供してください。",
}

# 背景画像のファイル名をテーマに紐づけ
THEME_IMAGES = {
    "デフォルト": "default_bg.png",
    "原神": "genshin_bg.png",
    "鳴潮": "wuthering_bg.png",
    "ゼンレスゾーンゼロ": "zenless_bg.png",
    "Fortnite": "fortnite_bg.png",
    "Dead by Daylight": "dbd_bg.png",
    "ヒロアカウルトラランブル": "heroaca_bg.png",
    "バイオハザード": "biohazard_bg.png",
}

# BGMファイルをテーマに紐づけ（ファイルがない場合はNone）
THEME_BGMS = {
    "デフォルト": None,
    "原神": None,
    "鳴潮": None,
    "ゼンレスゾーンゼロ": None,
    "Fortnite": None,
    "Dead by Daylight": None,
    "ヒロアカウルトラランブル": None,
    "バイオハザード": None,
}


# --- グローバル状態 ---
# 各セッションで共有する状態はst.session_stateに保存
def initialize_session_state():
    state_defaults = {
        "bot_running": False,
        "stop_event": threading.Event(),
        "latest_comment": None,
        "chat_id": None,
        "live_video_id": None,
        "ai_enabled": True,
        "persona": "デフォルト",
        "auto_greeting": True,
        "sent_greeting": False,
        "sent_closing": False,
        "background": "デフォルト",
        "bgm_volume": 0.5,
        "manual_stream_url": "",
        "manual_message": "",
        "start_greeting_text": "配信が始まりました！みんなで楽しみましょう！",
        "end_greeting_text": "配信を観てくれてありがとうございました！またね！",
    }
    for key, value in state_defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


initialize_session_state()


# --- YouTube API関連関数 ---
@st.cache_resource(show_spinner=False)
def get_youtube_api():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


# 認証処理とトークン管理
def get_authenticated_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


# ライブ配信IDを自動検出
def get_live_video_id(channel_id: str) -> Optional[str]:
    youtube = get_youtube_api()
    search_response = (
        youtube.search()
        .list(
            part="id",
            channelId=channel_id,
            eventType="live",
            type="video",
            maxResults=1,
        )
        .execute()
    )
    items = search_response.get("items", [])
    if items:
        return items[0]["id"]["videoId"]
    return None


# 指定動画URLからIDを取得
def extract_video_id_from_url(url: str) -> Optional[str]:
    if "watch?v=" in url:
        return url.split("watch?v=")[-1].split("&")[0]
    if "youtu.be/" in url:
        return url.split("youtu.be/")[-1].split("?")[0]
    return None


# 動画IDからチャットIDを取得
def get_live_chat_id(video_id: str) -> Optional[str]:
    youtube = get_youtube_api()
    response = youtube.videos().list(part="liveStreamingDetails", id=video_id).execute()
    items = response.get("items", [])
    if items:
        return items[0]["liveStreamingDetails"]["activeLiveChatId"]
    return None


# 最新コメントを取得
def poll_chat_messages(chat_id: str, page_token: Optional[str] = None):
    youtube = get_authenticated_service()
    response = (
        youtube.liveChatMessages()
        .list(
            liveChatId=chat_id,
            part="snippet,authorDetails",
            pageToken=page_token,
            maxResults=200,
        )
        .execute()
    )
    return response


# コメントにAIで返答
def generate_ai_reply(comment: str, persona: str) -> str:
    prompt = (
        PERSONAS.get(persona, "") + f"\n\nコメント: {comment}\n\n返答 (50文字以内):"
    )
    try:
        response = gemini.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                candidate_count=1,
                max_output_tokens=100,
                temperature=0.7,
            ),
        )
        ai_reply = response.candidates[0].content.parts[0].text.strip()
        return ai_reply[:50]
    except Exception as e:
        logging.error(f"AI生成失敗: {e}")
        return "…"


# チャットにメッセージ送信
def send_message(chat_id: str, message: str):
    youtube = get_authenticated_service()
    youtube.liveChatMessages().insert(
        part="snippet",
        body={
            "snippet": {
                "liveChatId": chat_id,
                "type": "textMessageEvent",
                "textMessageDetails": {"messageText": message},
            }
        },
    ).execute()


# ボットのメインループ
def bot_loop():
    st.session_state.sent_greeting = False
    st.session_state.sent_closing = False
    while not st.session_state.stop_event.is_set():
        if st.session_state.manual_stream_url:
            video_id = extract_video_id_from_url(st.session_state.manual_stream_url)
            if video_id != st.session_state.live_video_id:
                st.session_state.live_video_id = video_id
                st.session_state.chat_id = get_live_chat_id(video_id)
        else:
            current_live = get_live_video_id(CHANNEL_ID)
            if current_live != st.session_state.live_video_id:
                st.session_state.live_video_id = current_live
                st.session_state.chat_id = get_live_chat_id(current_live)

        chat_id = st.session_state.chat_id

        # 挨拶メッセージの自動送信
        if st.session_state.auto_greeting:
            if chat_id and not st.session_state.sent_greeting:
                send_message(chat_id, st.session_state.start_greeting_text)
                st.session_state.sent_greeting = True
            if (
                not chat_id
                and st.session_state.sent_greeting
                and not st.session_state.sent_closing
            ):
                send_message(
                    st.session_state.chat_id, st.session_state.end_greeting_text
                )
                st.session_state.sent_closing = True

        if chat_id:
            response = poll_chat_messages(chat_id)
            messages = response.get("items", [])
            for item in reversed(messages):
                text = item["snippet"]["textMessageDetails"]["messageText"]
                author = item["authorDetails"]["displayName"]
                published = item["snippet"]["publishedAt"]
                message_entry = {
                    "text": text,
                    "author": author,
                    "published": published,
                    "is_bot": False,
                }
                st.session_state.latest_comment = message_entry

                if (
                    st.session_state.ai_enabled
                    and not item["authorDetails"]["isChatOwner"]
                    and not item["authorDetails"]["isChatModerator"]
                    and not item["authorDetails"]["isChatSponsor"]
                ):
                    ai_response = generate_ai_reply(text, st.session_state.persona)
                    send_message(chat_id, ai_response)

        time.sleep(5)


# --- UI表示 ---
def display_background(theme: str):
    image_file = THEME_IMAGES.get(theme, "default_bg.png")
    image_path = os.path.join(os.path.dirname(__file__), image_file)
    if os.path.exists(image_path):
        st.markdown(
            f"""
            <style>
            .stApp {{
                background-image: url("data:image/png;base64,{base64_image(image_path)}");
                background-size: cover;
            }}
            </style>
            """,
            unsafe_allow_html=True,
        )


# 画像をbase64エンコード
def base64_image(file_path: str) -> str:
    import base64

    with open(file_path, "rb") as f:
        data = f.read()
    return base64.b64encode(data).decode()


# BGM埋め込み
def play_bgm(bgm_path: Optional[str], volume: float):
    if not bgm_path or not os.path.exists(bgm_path):
        return
    st.audio(bgm_path, format="audio/mp3", start_time=0)
    st.write(
        f"""
        <script>
        const audioElems = document.getElementsByTagName('audio');
        for (const audio of audioElems) {{
            audio.volume = {volume};
        }}
        </script>
        """,
        unsafe_allow_html=True,
    )


# サイドバーUI
def sidebar_controls():
    with st.sidebar:
        st.title("コントロールパネル")
        # Bot起動/停止
        if st.session_state.bot_running:
            if st.button("Bot停止"):
                st.session_state.stop_event.set()
                st.session_state.bot_running = False
                st.success("Botを停止しました")
        else:
            if st.button("Bot開始"):
                st.session_state.stop_event.clear()
                bot_thread = threading.Thread(target=bot_loop, daemon=True)
                bot_thread.start()
                st.session_state.bot_running = True
                st.success("Botを開始しました")

        # 手動接続URL
        st.text_input(
            "手動配信URL",
            key="manual_stream_url",
            placeholder="https://www.youtube.com/watch?v=...",
        )
        if st.button("手動接続"):
            video_id = extract_video_id_from_url(st.session_state.manual_stream_url)
            if video_id:
                st.session_state.live_video_id = video_id
                st.session_state.chat_id = get_live_chat_id(video_id)
                st.success(f"手動で動画に接続しました: {video_id}")
            else:
                st.error("動画IDが抽出できませんでした")

        # AI設定
        st.checkbox(
            "AI自動応答を有効にする",
            value=st.session_state.ai_enabled,
            key="ai_enabled",
        )
        st.selectbox("AIペルソナを選択", list(PERSONAS.keys()), key="persona")
        # 挨拶設定
        st.checkbox(
            "挨拶の自動送信を有効にする",
            value=st.session_state.auto_greeting,
            key="auto_greeting",
        )
        st.text_input(
            "開始挨拶メッセージ",
            value=st.session_state.start_greeting_text,
            key="start_greeting_text",
        )
        st.text_input(
            "終了挨拶メッセージ",
            value=st.session_state.end_greeting_text,
            key="end_greeting_text",
        )
        if st.button("開始挨拶を送信"):
            if st.session_state.chat_id:
                send_message(
                    st.session_state.chat_id, st.session_state.start_greeting_text
                )
                st.success("開始挨拶を送信しました")
            else:
                st.warning("ライブ配信に接続していません")
        if st.button("終了挨拶を送信"):
            if st.session_state.chat_id:
                send_message(
                    st.session_state.chat_id, st.session_state.end_greeting_text
                )
                st.success("終了挨拶を送信しました")
            else:
                st.warning("ライブ配信に接続していません")

        # テーマ設定
        st.selectbox("テーマ背景を選択", list(THEME_IMAGES.keys()), key="background")
        if THEME_BGMS.get(st.session_state.background):
            st.slider(
                "BGM音量", 0.0, 1.0, st.session_state.bgm_volume, 0.05, key="bgm_volume"
            )


# メインレイアウト表示
def main_layout():
    sidebar_controls()
    display_background(st.session_state.background)
    bgm_file = THEME_BGMS.get(st.session_state.background)
    play_bgm(bgm_file, st.session_state.bgm_volume)

    # ライブ映像とログ
    col1, col2 = st.columns(2)
    with col1:
        st.header("ライブ映像")
        if st.session_state.live_video_id:
            st.video(
                f"https://www.youtube.com/embed/{st.session_state.live_video_id}?autoplay=1"
            )
        else:
            st.write("現在接続中のライブ配信はありません")
    with col2:
        st.header("チャットログ")
        if st.session_state.latest_comment:
            st.text(
                f"[{st.session_state.latest_comment['published']}] {st.session_state.latest_comment['author']}: {st.session_state.latest_comment['text']}"
            )
        else:
            st.write("チャットはまだありません")


# --- 実行 ---
if __name__ == "__main__":
    main_layout()
