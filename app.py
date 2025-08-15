import streamlit as st
import threading
import time
import datetime
import os
import logging
import random
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

# ★★★ AIペルソナ機能を追加 ★★★
PERSONAS = {
    "デフォルト": "あなたはライブ配信を盛り上げる、親切でフレンドリーなアシスタントAIです。",
    "原神": "あなたは原神の世界「テイワット」から来た知識豊富な冒険者パイモンのようなAIです。元気で少し食いしん坊な口調で、初心者へのアドバイスやゲーム内のネタを交えながらコメントに答えてください。",
    "鳴潮": "あなたは未来的な世界観を持つ「鳴潮」の冷静沈着な分析官AIです。専門用語を少し交えつつ、的確でクールな口調で、戦略的なアドバイスや世界観に関する考察でコメントに応答してください。",
    "ゼンレスゾーンゼロ": "あなたは『ゼンレスゾーンゼロ』のストリートカルチャーに詳しいエージェントAIです。ヒップホップのスラングやノリの良い言葉を使い、スタイリッシュで都会的な雰囲気のコメントを返してください。",
    "Fortnite": "あなたはFortniteの建築マスター兼バトル戦術家のAIです。建築バトルや武器のメタ情報に詳しく、視聴者と一緒にビクロイを目指すような、エネルギッシュで競争的なコメントを返してください。",
    "Dead by Daylight": "あなたはDead by DaylightのベテランサバイバーのようなAIです。少し怖がりながらも、キラーの対策やパーク構成、脱出のコツなどを、仲間と協力するような親しみやすい口調でコメントしてください。",
    "ヒロアカウルトラランブル": "あなたは『僕のヒーローアカデミア』の世界でヒーローを目指す卵のようなAIです。「Plus Ultra!」の精神で、キャラクターの個性（技）の使い方やチームでの連携について、熱く、ヒーローらしい正義感あふれるコメントをしてください。",
    "バイオハザード7": "あなたはバイオハザード7の恐怖を生き抜いた生存者のようなAIです。少しおびえながらも、アイテムの場所や敵の倒し方について、他の生存者（視聴者）に助言を与えるような緊迫感のあるコメントをしてください。",
}

# --- Session Stateの初期化 ---
if "chat_log" not in st.session_state:
    st.session_state.chat_log = []
    st.session_state.running = False
    st.session_state.stop_event = threading.Event()
    st.session_state.selected_persona = "デフォルト"
    st.session_state.last_reply_time = 0  # AIの最終返信時刻
    st.session_state.live_chat_id = None


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


def get_live_chat_id(reader):
    try:
        resp = (
            reader.search()
            .list(part="id", channelId=CHANNEL_ID, eventType="live", type="video")
            .execute()
        )
        if not (items := resp.get("items", [])):
            return None
        vid = items[0]["id"]["videoId"]
        details = reader.videos().list(id=vid, part="liveStreamingDetails").execute()
        return details["items"][0]["liveStreamingDetails"].get("activeLiveChatId")
    except Exception as e:
        logging.error(f"ライブ詳細取得エラー: {e}")
        return None


def send_chat_message(service, chat_id, text):
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


def generate_ai_reply(msg, persona_key):
    persona_prompt = PERSONAS.get(persona_key, PERSONAS["デフォルト"])
    prompt = f"あなたは以下のキャラクターになりきって、視聴者のコメントに返信してください。\n\n# キャラクター設定\n{persona_prompt}\n\n# 視聴者のコメント\n「{msg}」\n\n# あなたの返信（50字程度の自然な会話で）:"
    res = gemini.generate_content(prompt)
    return res.text.strip()


# --- バックグラウンド監視スレッド ---
def monitor_thread(reader, service, stop_event):
    seen = set()
    while not stop_event.is_set():
        # ★★★ ライブ配信の常時自動検知 ★★★
        chat_id = get_live_chat_id(reader)
        st.session_state.live_chat_id = chat_id  # UI表示用に状態を更新

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

                # --- AI自動応答 ---
                # ★★★ 連投防止機能 ★★★
                # 自分の投稿には反応せず、一定時間待ってから応答する
                cooldown_seconds = 15
                can_reply = (user != "AI Bot") and (
                    time.time() - st.session_state.last_reply_time > cooldown_seconds
                )

                if can_reply:
                    # ★★★ 応答遅延を追加 ★★★
                    time.sleep(random.uniform(2, 4))

                    reply = generate_ai_reply(text, st.session_state.selected_persona)
                    send_chat_message(service, chat_id, reply)
                    st.session_state.last_reply_time = time.time()  # 最終返信時刻を更新
                    st.session_state.chat_log.append(
                        {"author": "AI Bot", "msg": reply, "time": timestamp}
                    )

            time.sleep(10)
        except Exception as e:
            logging.error(f"監視ループでエラー: {e}")
            time.sleep(20)


# --- UI ---
st.title("🤖 YouTube Gemini Bot")
col1, col2 = st.columns([3, 1])

with col1:
    if not st.session_state.running:
        if st.button("🟢 開始"):
            # ★★★ 安定性の向上のため、スレッド起動方法を修正 ★★★
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
        if st.button("🔴 停止"):
            st.session_state.stop_event.set()
            st.session_state.running = False
            st.rerun()

    st.markdown("---")
    # UIを自動更新させるため、チャットログ表示は毎回UIを描画する
    for entry in reversed(st.session_state.chat_log[-50:]):
        st.write(f"[{entry['time']}] **{entry['author']}**: {entry['msg']}")

with col2:
    # ★★★ AIペルソナ選択機能を追加 ★★★
    st.selectbox("AIペルソナを選択:", PERSONAS.keys(), key="selected_persona")
    st.markdown("---")

    user_msg = st.text_input("手動送信")
    if st.button("送信", key="send") and user_msg:
        # 手動送信時も、取得済みのchat_idを利用する
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

    st.write("※ 自動応答はYouTubeライブ内のチャットにも投稿されます")

# --- UI自動更新 ---
if st.session_state.running:
    time.sleep(5)
    st.rerun()
