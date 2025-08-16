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
    "ヒロアカウルトラランブル": "あなたは『僕のヒーローアカデミア』の世界でヒーローを目指す卵のようなAIです。『Plus Ultra!』の精神で、キャラクターの個性やチーム連携について、熱くヒーローらしい正義感あふれるコメントをしてください。",
    "バイオハザード7": "あなたはバイオハザード7の恐怖を生き抜いた生存者のようなAIです。少しおびえながらも、アイテムの場所や敵の倒し方について、他の生存者に助言を与えるような緊迫感のあるコメントをしてください。",
}

# --- Session Stateの初期化 ---
if "chat_log" not in st.session_state:
    st.session_state.chat_log = []
    st.session_state.running = False
    st.session_state.stop_event = threading.Event()
    st.session_state.selected_persona = "デフォルト"
    st.session_state.last_reply_time = 0  # AIの最終返信時刻
    st.session_state.live_chat_id = None
    st.session_state.current_video_id = None
    st.session_state.manual_chat_id = None
    st.session_state.manual_video_id = None
    st.session_state.ai_enabled = True
    st.session_state.auto_greeting_enabled = True
    st.session_state.previous_chat_id = None
    st.session_state.start_greeting = "📢 配信が始まりました！楽しんでいってね！"
    st.session_state.end_greeting = (
        "📢 配信が終了しました。ご視聴ありがとうございました！"
    )
    st.session_state.bg_theme = "デフォルト"
    st.session_state.bgm_volume = 0.5
    # BGM用URL（SoundHelixのサンプル楽曲を使用）
    st.session_state.bgm_files = {
        "デフォルト": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
        "原神": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
        "鳴潮": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-3.mp3",
        "ゼンレスゾーンゼロ": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-4.mp3",
        "Fortnite": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-5.mp3",
        "Dead by Daylight": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-6.mp3",
        "ヒロアカウルトラランブル": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-7.mp3",
        "バイオハザード7": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-8.mp3",
    }
    # 背景画像ファイル名（予め用意されたPNGファイル）
    st.session_state.bg_images = {
        "デフォルト": "default_bg.png",
        "原神": "genshin_bg.png",
        "鳴潮": "wuthering_bg.png",
        "ゼンレスゾーンゼロ": "zenless_bg.png",
        "Fortnite": "fortnite_bg.png",
        "Dead by Daylight": "dbd_bg.png",
        "ヒロアカウルトラランブル": "heroaca_bg.png",
        "バイオハザード7": "biohazard_bg.png",
    }


# --- YouTube & AI コア機能 ---
@st.cache_resource
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


@st.cache_resource
def get_youtube_reader():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def get_live_chat_details(reader):
    """
    現在配信中のライブのチャットIDと動画IDを返します。
    ライブがない場合は (None, None) を返します。
    """
    try:
        resp = (
            reader.search()
            .list(part="id", channelId=CHANNEL_ID, eventType="live", type="video")
            .execute()
        )
        items = resp.get("items", [])
        if not items:
            return None, None
        vid = items[0]["id"]["videoId"]
        details = reader.videos().list(id=vid, part="liveStreamingDetails").execute()
        chat_id = details["items"][0]["liveStreamingDetails"].get("activeLiveChatId")
        return chat_id, vid
    except Exception as e:
        logging.error(f"ライブ詳細取得エラー: {e}")
        return None, None


def parse_video_id(url: str) -> Optional[str]:
    """指定されたYouTube URLから動画IDを抽出します。失敗した場合はNoneを返します。"""
    import re

    try:
        pattern = r"(?:v=|\/)([0-9A-Za-z_-]{11})"
        match = re.search(pattern, url)
        return match.group(1) if match else None
    except Exception:
        return None


def get_chat_id_from_video(reader, video_id: str) -> Optional[str]:
    """動画IDからアクティブなライブチャットIDを取得します。"""
    try:
        details = (
            reader.videos().list(id=video_id, part="liveStreamingDetails").execute()
        )
        return details["items"][0]["liveStreamingDetails"].get("activeLiveChatId")
    except Exception as e:
        logging.error(f"動画IDからチャットID取得エラー: {e}")
        return None


def send_chat_message(service, chat_id: str, text: str) -> None:
    """指定されたチャットIDにテキストメッセージを送信します。"""
    try:
        service.liveChatMessages().insert(
            part="snippet",
            body={
                "snippet": {
                    "liveChatId": chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {"messageText": text},
                }
            },
        ).execute()
    except Exception as e:
        logging.error(f"メッセージ送信エラー: {e}")


def generate_ai_reply(msg: str, persona_key: str) -> str:
    """Geminiモデルを使ってコメントへの返信を生成する。"""
    persona_prompt = PERSONAS.get(persona_key, PERSONAS["デフォルト"])
    prompt = (
        "あなたは以下のキャラクターになりきって、視聴者のコメントに返信してください。\n\n"
        "# キャラクター設定\n"
        f"{persona_prompt}\n\n"
        "# 視聴者のコメント\n"
        f"「{msg}」\n\n"
        "# あなたの返信（50字程度の自然な会話で）:"
    )
    res = gemini.generate_content(prompt)
    return res.text.strip()


def monitor_thread(reader, service, stop_event: threading.Event) -> None:
    """
    バックグラウンドでライブチャットを監視し、チャットログを更新してAI応答や挨拶を行うスレッド。
    manual_chat_idが設定されていればそれを優先して監視する。
    """
    seen: set[str] = set()
    while not stop_event.is_set():
        # 手動接続があればそれを使用し、なければ自動検出
        if st.session_state.manual_chat_id:
            chat_id = st.session_state.manual_chat_id
            video_id = st.session_state.manual_video_id
        else:
            chat_id, video_id = get_live_chat_details(reader)

        st.session_state.live_chat_id = chat_id
        st.session_state.current_video_id = video_id

        # 自動挨拶の処理
        prev = st.session_state.previous_chat_id
        if st.session_state.auto_greeting_enabled:
            if prev is None and chat_id:
                try:
                    send_chat_message(service, chat_id, st.session_state.start_greeting)
                except Exception as e:
                    logging.error(f"開始挨拶送信エラー: {e}")
            elif prev and not chat_id:
                try:
                    send_chat_message(service, prev, st.session_state.end_greeting)
                except Exception as e:
                    logging.error(f"終了挨拶送信エラー: {e}")
        st.session_state.previous_chat_id = chat_id

        if not chat_id:
            logging.info("ライブ配信が見つかりません。20秒後に再試行します。")
            time.sleep(20)
            continue

        try:
            res = (
                reader.liveChatMessages()
                .list(liveChatId=chat_id, part="snippet,authorDetails")
                .execute()
            )
            for item in res.get("items", []):
                cid = item["id"]
                if cid in seen:
                    continue
                seen.add(cid)

                user = item["authorDetails"]["displayName"]
                text = item["snippet"]["displayMessage"]
                timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                st.session_state.chat_log.append(
                    {"author": user, "msg": text, "time": timestamp}
                )

                # AI応答
                cooldown = 15
                should_reply = (
                    user != "AI Bot"
                    and (time.time() - st.session_state.last_reply_time > cooldown)
                    and st.session_state.ai_enabled
                )
                if should_reply:
                    time.sleep(random.uniform(2, 4))
                    reply = generate_ai_reply(text, st.session_state.selected_persona)
                    try:
                        send_chat_message(service, chat_id, reply)
                    except Exception as e:
                        logging.error(f"AI応答送信エラー: {e}")
                    st.session_state.last_reply_time = time.time()
                    st.session_state.chat_log.append(
                        {"author": "AI Bot", "msg": reply, "time": timestamp}
                    )

            time.sleep(10)
        except Exception as e:
            logging.error(f"監視ループでエラー: {e}")
            time.sleep(20)


# --- UI ---
st.title("🤖 YouTube Gemini Bot")
col_left, col_right = st.columns([3, 1])

with col_left:
    # ステータス表示
    if st.session_state.live_chat_id:
        st.success(f"✅ 接続中のライブチャットID: {st.session_state.live_chat_id}")
    else:
        st.info("🔍 現在接続中のライブチャットはありません")

    # ライブ映像の埋め込み
    if st.session_state.current_video_id:
        video_src = f"https://www.youtube.com/embed/{st.session_state.current_video_id}?autoplay=0"
        st.components.v1.html(
            f'<iframe width="100%" height="360" src="{video_src}" frameborder="0" allowfullscreen></iframe>',
            height=360,
        )

    # Bot開始・停止
    if not st.session_state.running:
        if st.button("🟢 Bot開始"):
            reader = get_youtube_reader()
            service = get_authenticated_service()
            st.session_state.stop_event.clear()
            threading.Thread(
                target=monitor_thread,
                args=(reader, service, st.session_state.stop_event),
                daemon=True,
            ).start()
            st.session_state.running = True
            st.rerun()
    else:
        if st.button("🔴 Bot停止"):
            st.session_state.stop_event.set()
            st.session_state.running = False
            # 手動接続状態も解除
            st.session_state.manual_chat_id = None
            st.session_state.manual_video_id = None
            st.rerun()

    st.markdown("---")
    # チャットログ表示（最新50件）
    for entry in reversed(st.session_state.chat_log[-50:]):
        st.write(f"[{entry['time']}] **{entry['author']}**: {entry['msg']}")

with col_right:
    # AIペルソナ選択
    st.selectbox("AIペルソナを選択:", list(PERSONAS.keys()), key="selected_persona")
    # AI応答ON/OFF
    st.checkbox(
        "AI自動応答を有効にする", value=st.session_state.ai_enabled, key="ai_enabled"
    )
    # 自動挨拶ON/OFF
    st.checkbox(
        "自動挨拶を有効にする",
        value=st.session_state.auto_greeting_enabled,
        key="auto_greeting_enabled",
    )

    st.markdown("---")
    # 手動メッセージ送信
    user_msg = st.text_input("手動送信メッセージ")
    if st.button("💬 送信") and user_msg:
        if st.session_state.live_chat_id:
            service = get_authenticated_service()
            send_chat_message(service, st.session_state.live_chat_id, user_msg)
            st.session_state.chat_log.append(
                {
                    "author": "You",
                    "msg": user_msg,
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                }
            )
            st.rerun()
        else:
            st.warning("ライブ配信に接続していません。")

    # 挨拶メッセージの手動送信
    if st.button("👋 開始挨拶を送信"):
        if st.session_state.live_chat_id:
            service = get_authenticated_service()
            send_chat_message(
                service, st.session_state.live_chat_id, st.session_state.start_greeting
            )
        else:
            st.warning("ライブ配信に接続していません。")
    if st.button("👋 終了挨拶を送信"):
        if st.session_state.live_chat_id:
            service = get_authenticated_service()
            send_chat_message(
                service, st.session_state.live_chat_id, st.session_state.end_greeting
            )
        else:
            st.warning("ライブ配信に接続していません。")

    st.markdown("---")
    # 手動配信接続
    manual_url = st.text_input("YouTubeライブのURLを入力", key="manual_url")
    if st.button("🔗 手動接続"):
        vid = parse_video_id(manual_url)
        if vid:
            reader = get_youtube_reader()
            chat_id = get_chat_id_from_video(reader, vid)
            if chat_id:
                st.session_state.manual_chat_id = chat_id
                st.session_state.manual_video_id = vid
                st.success(
                    "手動でライブ配信に接続しました。Botを開始すると監視が始まります。"
                )
            else:
                st.error(
                    "指定された動画はライブ配信ではないか、チャットIDを取得できませんでした。"
                )
        else:
            st.error("URLから動画IDを抽出できませんでした。URLを確認してください。")

    st.markdown("---")
    # テーマ背景切替
    theme_options = list(st.session_state.bgm_files.keys())
    selected_theme = st.selectbox("テーマ背景を選択", theme_options, key="bg_theme")
    # BGM URL更新
    st.session_state.bgm_url = st.session_state.bgm_files.get(
        selected_theme, st.session_state.bgm_files["デフォルト"]
    )
    # 音量スライダー
    volume = st.slider(
        "BGM音量",
        min_value=0.0,
        max_value=1.0,
        value=st.session_state.bgm_volume,
        step=0.05,
        key="bgm_volume",
    )
    # 背景画像表示
    bg_image_path = st.session_state.bg_images.get(selected_theme)
    if bg_image_path and os.path.exists(bg_image_path):
        st.image(bg_image_path, use_column_width=True)
    # BGMプレイヤー（HTML5オーディオ）
    audio_html = f"""
        <audio controls autoplay loop style="width:100%" volume="{volume}">
            <source src="{st.session_state.bgm_url}" type="audio/mpeg">
        </audio>
    """
    st.components.v1.html(audio_html, height=80)
    st.write(
        "※ 音量はブラウザ側でも調整可能です。背景画像がない場合はデフォルト背景が使用されます。"
    )

# --- UI自動更新 ---
if st.session_state.running:
    # 5秒ごとに画面を再描画して最新のチャットを表示
    time.sleep(5)
    st.rerun()