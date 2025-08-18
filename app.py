# -*- coding: utf-8 -*-
"""
YouTubeBOT（統合・単一ファイル版・完全統合）
- Streamlit 管理画面
- YouTube Live 自動/手動接続、チャット監視、投稿
- Google Gemini による50文字以内の自動応答（ペルソナ切替）
- personas.json をホットリロード（保存→即反映）
- BGM / テーマ背景 / ステータス表示
- ゲームごとの画像・BGM選択（/images, /audio）
- OAuth: client_secret.json + token.json を使用

補足:
- 画像/音声はローカル相対パスでもOK（自動で data URI に変換して埋め込み表示）
- personas.json が不正でも落ちない（フォールバック）
- セッションキー未初期化時の KeyError（chat_lock など）を回避
"""

from __future__ import annotations
import os
import re
import json
import time
import base64
import mimetypes
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
from streamlit.components.v1 import html as st_html

# --- Google / YouTube ---
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- Gemini ---
try:
    import google.generativeai as genai
except Exception:
    genai = None  # ランタイムに無い場合もエラーにしない

# ============================================================
# 定数・ユーティリティ
# ============================================================
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
JST = timezone(timedelta(hours=9), name="JST")
YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/live/|/shorts/)([A-Za-z0-9_-]{11})")
PERSONAS_DEFAULT_PATH = "personas.json"


# ============================================================
# 画像/音声 ヘルパ
# ============================================================
def is_url(path_or_url: str) -> bool:
    u = (path_or_url or "").strip().lower()
    return u.startswith("http://") or u.startswith("https://") or u.startswith("data:")


def file_to_data_url(path: str) -> Optional[str]:
    """ローカルファイルを data:URI に変換。存在しない場合 None。"""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    mime, _ = mimetypes.guess_type(str(p))
    if not mime:
        # 拡張子不明でも画像/音声で仮定
        if p.suffix.lower() in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        elif p.suffix.lower() == ".png":
            mime = "image/png"
        elif p.suffix.lower() in (".mp3",):
            mime = "audio/mpeg"
        elif p.suffix.lower() in (".m4a",):
            mime = "audio/mp4"
        elif p.suffix.lower() in (".ogg",):
            mime = "audio/ogg"
        else:
            mime = "application/octet-stream"
    b = p.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(b).decode('ascii')}"


# ============================================================
# ペルソナ ローディング（JSON ホットリロード）
# ============================================================
@st.cache_data(show_spinner=False)
def load_personas(json_path: str, _mtime: float) -> Dict[str, Any]:
    p = Path(json_path)
    if not p.exists():
        st.warning(f"personas.json が見つかりません: {json_path}")
        return {"personas": []}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        st.error("personas.json の読み込みに失敗しました（JSON形式が不正です）。")
        return {"personas": []}


@dataclass
class CharacterGreetings:
    start: str = ""
    end: str = ""
    replies: List[str] = None


@dataclass
class Character:
    name: str
    greetings: CharacterGreetings


@dataclass
class Persona:
    name: str
    characters: List[Character]


def normalize_personas(raw: Dict[str, Any]) -> List[Persona]:
    # 期待構造: {"personas":[{"name":"原神","characters":[{"name":"パイモン","greetings":{"start":"...","end":"...","replies":[...]}}]}]}
    personas: List[Persona] = []
    items = raw.get("personas") or raw.get("data") or raw.get("list") or []
    for p in items:
        pname = p.get("name") or p.get("title") or "デフォルト"
        chars_raw = p.get("characters") or p.get("list") or []
        chars: List[Character] = []
        for c in chars_raw:
            cname = c.get("name") or c.get("title") or "キャラ"
            g = c.get("greetings") or c.get("characterGreetings") or {}
            start = (
                g.get("start")
                or "皆さん、こんにちは！配信へようこそ！一緒に楽しんでいきましょう！"
            )
            end = (
                g.get("end")
                or "今日もありがとうございました！また次回の配信でお会いしましょう！お疲れ様でした！"
            )
            replies = g.get("replies") or []
            if replies is None:
                replies = []
            chars.append(
                Character(
                    name=cname,
                    greetings=CharacterGreetings(start=start, end=end, replies=replies),
                )
            )
        personas.append(Persona(name=pname, characters=chars))
    if not personas:
        personas = [
            Persona(
                name="デフォルト",
                characters=[
                    Character(
                        name="配信者",
                        greetings=CharacterGreetings(
                            start="皆さん、こんにちは！配信へようこそ！一緒に楽しんでいきましょう！",
                            end="今日もありがとうございました！また次回の配信でお会いしましょう！お疲れ様でした！",
                            replies=[
                                "すごい！",
                                "なるほど！",
                                "面白いですね！",
                                "応援してます！",
                            ],
                        ),
                    )
                ],
            )
        ]
    return personas


# ============================================================
# 資格情報 / YouTube サービス
# ============================================================


def get_credentials() -> Credentials:
    creds: Optional[Credentials] = None
    token_path = Path("token.json")
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secret_path = Path("client_secret.json")
            if not secret_path.exists():
                st.error(
                    "client_secret.json が見つかりません。Google Cloud でOAuthクライアントを作成してください。"
                )
                raise FileNotFoundError("client_secret.json not found")
            flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), SCOPES)
            creds = flow.run_local_server(port=0)
        # 保存
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


@st.cache_resource(show_spinner=False)
def get_youtube_service(_creds: Credentials):
    # UnhashableParamError 対策：引数名を _creds に
    return build("youtube", "v3", credentials=_creds, cache_discovery=False)


# ============================================================
# YouTube API ラッパ
# ============================================================


def search_live_video_id_by_channel(youtube, channel_id: str) -> Optional[str]:
    try:
        resp = (
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
        items = resp.get("items", [])
        if not items:
            return None
        return items[0]["id"].get("videoId")
    except HttpError as e:
        st.error(f"YouTube API error (search): {e}")
        return None


def extract_video_id(url_or_id: str) -> Optional[str]:
    url_or_id = (url_or_id or "").strip()
    if not url_or_id:
        return None
    if len(url_or_id) == 11 and re.match(r"^[A-Za-z0-9_-]{11}$", url_or_id):
        return url_or_id
    m = YOUTUBE_ID_RE.search(url_or_id)
    if m:
        return m.group(1)
    return None


def get_live_chat_id(youtube, video_id: str) -> Optional[str]:
    try:
        resp = youtube.videos().list(part="liveStreamingDetails", id=video_id).execute()
        items = resp.get("items", [])
        if not items:
            return None
        return items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")
    except HttpError as e:
        st.error(f"YouTube API error (videos.list): {e}")
        return None


def send_chat_message(youtube, live_chat_id: str, text: str) -> bool:
    try:
        body = {
            "snippet": {
                "type": "textMessageEvent",
                "liveChatId": live_chat_id,
                "textMessageDetails": {"messageText": text},
            }
        }
        youtube.liveChatMessages().insert(part="snippet", body=body).execute()
        return True
    except HttpError as e:
        st.error(f"YouTube API error (liveChatMessages.insert): {e}")
        return False


# ============================================================
# Gemini 応答
# ============================================================


def setup_gemini(api_key: str) -> Optional[Any]:
    if not genai:
        st.warning(
            "google-generativeai がインストールされていません。AI応答は無効になります。"
        )
        return None
    if not api_key:
        st.warning("GEMINI_API_KEY が未設定です。AI応答は無効になります。")
        return None
    genai.configure(api_key=api_key)
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        return model
    except Exception as e:
        st.error(f"Gemini 初期化エラー: {e}")
        return None


def build_persona_prompt(persona: Persona, character: Character) -> str:
    # 口調・挨拶・短文指示
    replies = character.greetings.replies or []
    style = " / ".join(replies[:6]) if replies else "丁寧"
    return (
        f"あなたは『{persona.name}』の世界観のキャラクター『{character.name}』として返信します。"
        f" 口調・語尾はキャラクターに合わせ、50文字以内の短い応答を1つだけ返してください。"
        f" 絵文字や顔文字は控えめに。文末に不要な記号は付けない。"
        f" 参考フレーズ:{style}"
    )


def generate_ai_reply(
    model, persona: Persona, character: Character, user_text: str
) -> str:
    if not model:
        return ""
    sys_prompt = build_persona_prompt(persona, character)
    try:
        out = model.generate_content(
            [{"role": "user", "parts": [sys_prompt + "\nユーザー: " + user_text]}]
        )
        text = (out.text or "").strip()
        # 50文字にトリム
        return text[:50]
    except Exception as e:
        st.warning(f"AI応答生成エラー: {e}")
        return ""


# ============================================================
# チャット監視スレッド
# ============================================================


class ChatWatcher:
    def __init__(
        self,
        youtube,
        live_chat_id: str,
        my_channel_id: Optional[str],
        on_message,
        stop_event: threading.Event,
        ai_model=None,
        persona: Optional[Persona] = None,
        character: Optional[Character] = None,
        auto_reply: bool = False,
        rate_limit_sec: int = 15,
    ):
        self.youtube = youtube
        self.live_chat_id = live_chat_id
        self.my_channel_id = my_channel_id
        self.on_message = on_message
        self.stop_event = stop_event
        self.next_page_token = None
        self.ai_model = ai_model
        self.persona = persona
        self.character = character
        self.auto_reply = auto_reply
        self.rate_limit_sec = rate_limit_sec
        self.last_reply_at: Dict[str, float] = {}

    def _should_reply(self, author_channel_id: str) -> bool:
        if not self.auto_reply or not self.ai_model:
            return False
        if self.my_channel_id and author_channel_id == self.my_channel_id:
            return False  # 自分には反応しない
        now = time.time()
        t = self.last_reply_at.get(author_channel_id, 0)
        if now - t < self.rate_limit_sec:
            return False
        self.last_reply_at[author_channel_id] = now
        return True

    def run(self):
        polling_interval = 3.0
        while not self.stop_event.is_set():
            try:
                req = self.youtube.liveChatMessages().list(
                    liveChatId=self.live_chat_id,
                    part="snippet,authorDetails",
                    pageToken=self.next_page_token,
                )
                resp = req.execute()
                self.next_page_token = resp.get("nextPageToken")
                polling_interval_ms = resp.get("pollingIntervalMillis", 3000)
                polling_interval = max(1.0, polling_interval_ms / 1000.0)
                for item in resp.get("items", []):
                    snip = item.get("snippet", {})
                    auth = item.get("authorDetails", {})
                    text = snip.get("textMessageDetails", {}).get("messageText")
                    if text is None:
                        # ほかのタイプはスキップ
                        continue
                    ts = snip.get("publishedAt")
                    author_name = auth.get("displayName", "?")
                    author_channel_id = auth.get("channelId")
                    is_owner = auth.get("isChatOwner", False) or auth.get(
                        "isChatModerator", False
                    )

                    # UI へ反映
                    self.on_message(
                        {
                            "time": ts,
                            "author": author_name,
                            "text": text,
                            "owner": is_owner,
                            "bot": False,
                        }
                    )

                    # AI 自動応答
                    if self._should_reply(author_channel_id):
                        reply = generate_ai_reply(
                            self.ai_model, self.persona, self.character, text
                        )
                        if reply:
                            ok = send_chat_message(
                                self.youtube, self.live_chat_id, reply
                            )
                            self.on_message(
                                {
                                    "time": datetime.now(JST).isoformat(),
                                    "author": "Bot",
                                    "text": reply,
                                    "owner": True,
                                    "bot": True,
                                    "sent": ok,
                                }
                            )
            except HttpError as e:
                self.on_message(
                    {
                        "time": datetime.now(JST).isoformat(),
                        "author": "System",
                        "text": f"YouTube API error: {e}",
                        "owner": True,
                        "bot": True,
                    }
                )
                time.sleep(5)
            except Exception as e:
                self.on_message(
                    {
                        "time": datetime.now(JST).isoformat(),
                        "author": "System",
                        "text": f"Watcher error: {e}",
                        "owner": True,
                        "bot": True,
                    }
                )
                time.sleep(5)
            finally:
                time.sleep(polling_interval)


# ============================================================
# UI 初期化 & チャット管理
# ============================================================


def init_session_state():
    ss = st.session_state
    defaults = {
        "personas_path": st.secrets.get("PERSONAS_PATH", PERSONAS_DEFAULT_PATH),
        "yt_connected": False,
        "yt_video_id": "",
        "yt_live_chat_id": "",
        "yt_channel_id": st.secrets.get("CHANNEL_ID", ""),
        "chat_log": [],
        "chat_lock": threading.Lock(),
        "stop_event": threading.Event(),
        "watcher_thread": None,
        "auto_greet": True,
        "ai_enabled": True,
        "bg_url": st.secrets.get("THEME_BG_URL", ""),
        "bgm_url": st.secrets.get("BGM_URL", ""),
        "bgm_volume": 0.2,
        "gemini_api_key": st.secrets.get(
            "GEMINI_API_KEY", os.getenv("GEMINI_API_KEY", "")
        ),
        "my_channel_id": None,  # 自分のチャンネルID（必要なら取得）
        "selected_persona_name": None,
        "selected_character_name": None,
        "selected_game": "なし",
    }
    for k, v in defaults.items():
        if k not in ss:
            ss[k] = v


def append_chat(row: Dict[str, Any]):
    ss = st.session_state
    if "chat_lock" not in ss:
        ss["chat_lock"] = threading.Lock()  # フォールバック初期化
    with ss.chat_lock:
        ss.chat_log.append(row)


# ============================================================
# UI コンポーネント
# ============================================================


def render_background_css(src: str):
    if not src:
        return
    url = src
    if not is_url(src):
        data = file_to_data_url(src)
        if data:
            url = data
        else:
            st.warning(f"背景画像が見つかりません: {src}")
            return
    st.markdown(
        f"""
        <style>
        .stApp {{
            background-image: url('{url}');
            background-size: cover;
            background-position: center center;
            background-attachment: fixed;
        }}
        /* 透過カード調整 */
        .block-container {{
            background: rgba(0,0,0,0.35);
            border-radius: 16px;
            padding: 1rem 1.2rem;
        }}
        .stMarkdown, .stText, .stDataFrame, .stChatMessage {{ color: #fff; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_bgm_player(src: str, volume: float):
    if not src:
        return
    vol = max(0.0, min(1.0, float(volume)))
    url = src
    if not is_url(src):
        data = file_to_data_url(src)
        if data:
            url = data
        else:
            st.warning(f"BGMファイルが見つかりません: {src}")
            return
    st_html(
        f"""
        <audio id="bgm" src="{url}" autoplay loop></audio>
        <script>
        const audio = document.getElementById('bgm');
        if (audio) {{ audio.volume = {vol}; }}
        </script>
        """,
        height=0,
    )


def render_chat_log():
    with st.container(height=420):
        for row in st.session_state.chat_log[-500:]:
            who = "🟢" if not row.get("bot") else "🤖"
            ts = row.get("time")
            author = row.get("author")
            text = row.get("text")
            if row.get("bot"):
                st.markdown(
                    f"<div style='background:#333;padding:6px;border-radius:8px;'>{who} <b>{author}</b> <code>[{ts}]</code><br>{text}</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<div style='padding:6px;border-radius:8px;'>{who} <b>{author}</b> <code>[{ts}]</code><br>{text}</div>",
                    unsafe_allow_html=True,
                )
            st.divider()


# ============================================================
# ゲームメディア定義
# ============================================================
GAME_MEDIA = {
    "Dead by Daylight": {
        "image": "images/Dead by Daylight.jpg",
        "audio": "audio/Dead by Daylight.mp3",
    },
    "Fortnite": {
        "image": "images/Fortnite.jpg",
        "audio": "audio/Fortnite.mp3",
    },
    "ゼンレスゾーンゼロ": {
        "image": "images/ゼンレスゾーンゼロ.jpg",
        "audio": "audio/ゼンレスゾーンゼロ.mp3",
    },
    "バイオハザード7": {
        "image": "images/バイオハザード7.jpg",
        "audio": "audio/バイオハザード7.mp3",
    },
    "ヒロアカウルトラランブル": {
        "image": "images/ヒロアカウルトラランブル.jpg",
        "audio": "audio/ヒロアカウルトラランブル.mp3",
    },
    "原神": {
        "image": "images/原神.jpg",
        "audio": "audio/原神.mp3",
    },
    "鳴潮": {
        "image": "images/鳴潮.jpg",
        "audio": "audio/鳴潮.mp3",
    },
}

# ============================================================
# サイドバー UI
# ============================================================


def sidebar_controls(personas: List[Persona]):
    ss = st.session_state

    with st.sidebar:
        st.subheader("⚙️ コントロール")

        # Google 認証 & サービス
        auth_col1, auth_col2 = st.columns([1, 1])
        if auth_col1.button("🔐 Google 認証"):
            try:
                creds = get_credentials()
                ss.yt_service = get_youtube_service(creds)
                st.success("Google 認証OK / YouTube API 初期化しました。")
            except Exception as e:
                st.error(f"認証エラー: {e}")
        if auth_col2.button("♻️ サービス再生成"):
            try:
                creds = get_credentials()
                ss.yt_service = get_youtube_service(creds)
                st.success("YouTube サービスを再生成しました。")
            except Exception as e:
                st.error(f"初期化エラー: {e}")

        # 接続
        st.divider()
        st.markdown("**🔴 配信に接続**")
        ss.yt_channel_id = st.text_input(
            "チャンネルID（ライブ自動検出）", value=ss.yt_channel_id
        )
        colA, colB = st.columns([1, 1])
        with colA:
            if st.button(
                "📡 ライブ検出して接続",
                use_container_width=True,
                disabled=("yt_service" not in ss),
            ):
                vid = search_live_video_id_by_channel(ss.yt_service, ss.yt_channel_id)
                if not vid:
                    st.warning("ライブ配信が見つかりません")
                else:
                    connect_to_video_id(vid)
        with colB:
            manual = st.text_input("ライブURL または videoId")
            if st.button(
                "🔗 手動接続",
                use_container_width=True,
                disabled=("yt_service" not in ss),
            ):
                vid = extract_video_id(manual)
                if not vid:
                    st.warning("URL/ID を正しく入力してください")
                else:
                    connect_to_video_id(vid)

        # AI / ペルソナ
        st.divider()
        st.markdown("**🤖 AI 応答**")
        ss.ai_enabled = st.toggle("AI応答を有効化", value=ss.ai_enabled)
        # personas.json リロード
        ppath = Path(ss.personas_path)
        colR1, colR2 = st.columns([1, 1])
        if colR1.button("🔄 ペルソナ再読込", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        ss.personas_path = st.text_input("personas.json パス", value=str(ppath))

        # Persona / Character 選択
        persona_names = [p.name for p in personas]
        if not persona_names:
            st.error("ペルソナがありません")
            sel_persona_idx = 0
        else:
            sel_persona_idx = max(
                0,
                (
                    persona_names.index(
                        ss.get("selected_persona_name") or persona_names[0]
                    )
                    if ss.get("selected_persona_name") in persona_names
                    else 0
                ),
            )
        persona_obj: Persona = (
            personas[sel_persona_idx] if personas else Persona("デフォルト", [])
        )
        sel_persona = st.selectbox(
            "ペルソナ", persona_names, index=sel_persona_idx, key="ui_persona"
        )
        if sel_persona != ss.get("selected_persona_name"):
            ss.selected_persona_name = sel_persona

        char_names = [c.name for c in persona_obj.characters] or ["キャラ"]
        if not char_names:
            char_idx = 0
        else:
            prev = ss.get("selected_character_name")
            char_idx = max(0, char_names.index(prev) if prev in char_names else 0)
        sel_char = st.selectbox(
            "キャラクター", char_names, index=char_idx, key="ui_character"
        )
        if sel_char != ss.get("selected_character_name"):
            ss.selected_character_name = sel_char

        # 挨拶（キャラ名でキーを分ける → エラー回避）
        ch = next((c for c in persona_obj.characters if c.name == sel_char), None)
        if ch is None and persona_obj.characters:
            ch = persona_obj.characters[0]
        start_key = (
            f"start_greet__{persona_obj.name}__{ch.name}"
            if ch
            else "start_greet__default"
        )
        end_key = (
            f"end_greet__{persona_obj.name}__{ch.name}" if ch else "end_greet__default"
        )
        default_start = ch.greetings.start if ch else "配信開始のご挨拶です！"
        default_end = ch.greetings.end if ch else "本日はありがとうございました！"
        st.text_area(
            "開始挨拶（接続時に送信可）", value=default_start, key=start_key, height=80
        )
        st.text_area(
            "終了挨拶（切断時に送信可）", value=default_end, key=end_key, height=80
        )
        ss.auto_greet = st.toggle("接続/切断で自動挨拶", value=ss.auto_greet)

        # ゲーム選択 → 背景 & BGM 切替
        st.divider()
        st.markdown("**🎮 ゲーム演出**")
        game_choice = st.selectbox(
            "ゲームを選択",
            ["なし"] + list(GAME_MEDIA.keys()),
            index=(["なし"] + list(GAME_MEDIA.keys())).index(
                ss.get("selected_game", "なし")
            ),
        )
        if game_choice != ss.get("selected_game"):
            ss.selected_game = game_choice
        if game_choice != "なし":
            media = GAME_MEDIA[game_choice]
            # 背景・BGM を自動設定（ローカル → data URI 埋め込み）
            ss.bg_url = media["image"]
            ss.bgm_url = media["audio"]
        else:
            # 手動入力モード（従来通り）
            ss.bg_url = st.text_input("背景画像パス/URL", value=ss.bg_url)
            ss.bgm_url = st.text_input("BGM パス/URL (mp3/m4a/ogg)", value=ss.bgm_url)
        ss.bgm_volume = st.slider("BGM 音量", 0.0, 1.0, float(ss.bgm_volume), 0.01)

        # 動作
        st.divider()
        ctrl1, ctrl2 = st.columns([1, 1])
        if ctrl1.button(
            "▶️ 監視開始",
            use_container_width=True,
            disabled=not ss.get("yt_live_chat_id"),
        ):
            start_watch(personas)
        if ctrl2.button("⏹️ 停止", use_container_width=True):
            stop_watch(send_goodbye=ss.auto_greet)

        st.divider()
        st.caption("© YouTubeBOT / Streamlit")


# ============================================================
# 接続処理 / 監視開始/停止
# ============================================================


def connect_to_video_id(video_id: str):
    ss = st.session_state
    if "yt_service" not in ss:
        st.warning("先に『Google 認証』を実行してください")
        return
    live_chat_id = get_live_chat_id(ss.yt_service, video_id)
    if not live_chat_id:
        st.warning("この動画にはアクティブなライブチャットがありません。")
        return
    ss.yt_video_id = video_id
    ss.yt_live_chat_id = live_chat_id
    ss.yt_connected = True

    # 自動挨拶（開始）
    if ss.auto_greet and ss.ai_enabled is not None:
        persona, ch = current_persona_and_character()
        start_key = (
            f"start_greet__{persona.name}__{ch.name}"
            if persona and ch
            else "start_greet__default"
        )
        start_msg = st.session_state.get(start_key) or (
            ch.greetings.start if ch else "配信へようこそ！"
        )
        send_chat_message(ss.yt_service, ss.yt_live_chat_id, start_msg)
        append_chat(
            {
                "time": datetime.now(JST).isoformat(),
                "author": "Bot",
                "text": start_msg,
                "owner": True,
                "bot": True,
                "sent": True,
            }
        )


def current_persona_and_character() -> Tuple[Optional[Persona], Optional[Character]]:
    personas = st.session_state.get("_personas", [])
    pn = st.session_state.get("selected_persona_name")
    cn = st.session_state.get("selected_character_name")
    p = next((x for x in personas if x.name == pn), (personas[0] if personas else None))
    c = next(
        (y for y in (p.characters if p else []) if y.name == cn),
        ((p.characters[0] if p and p.characters else None)),
    )
    return p, c


def start_watch(personas: List[Persona]):
    ss = st.session_state
    if not ss.get("yt_connected"):
        st.warning("先に配信へ接続してください")
        return
    if ss.get("watcher_thread") and ss.get("watcher_thread").is_alive():
        st.info("すでに監視中です")
        return

    # Gemini
    model = setup_gemini(ss.gemini_api_key) if ss.ai_enabled else None
    ss._personas = personas  # 後から参照
    persona, character = current_persona_and_character()

    ss.stop_event.clear()

    watcher = ChatWatcher(
        youtube=ss.yt_service,
        live_chat_id=ss.yt_live_chat_id,
        my_channel_id=ss.my_channel_id,
        on_message=append_chat,
        stop_event=ss.stop_event,
        ai_model=model,
        persona=persona,
        character=character,
        auto_reply=bool(ss.ai_enabled),
        rate_limit_sec=15,
    )
    th = threading.Thread(target=watcher.run, daemon=True)
    th.start()
    ss.watcher_thread = th
    st.success("チャット監視を開始しました")


def stop_watch(send_goodbye: bool = False):
    ss = st.session_state
    if ss.get("watcher_thread") and ss.get("watcher_thread").is_alive():
        ss.stop_event.set()
        ss.watcher_thread.join(timeout=3)
        ss.watcher_thread = None
        st.info("監視を停止しました")
    if send_goodbye and ss.get("yt_connected"):
        persona, ch = current_persona_and_character()
        end_key = (
            f"end_greet__{persona.name}__{ch.name}"
            if persona and ch
            else "end_greet__default"
        )
        end_msg = st.session_state.get(end_key) or (
            ch.greetings.end if ch else "ご視聴ありがとうございました！"
        )
        if ss.get("yt_live_chat_id"):
            send_chat_message(ss.yt_service, ss.yt_live_chat_id, end_msg)
            append_chat(
                {
                    "time": datetime.now(JST).isoformat(),
                    "author": "Bot",
                    "text": end_msg,
                    "owner": True,
                    "bot": True,
                    "sent": True,
                }
            )
    # 接続状態は維持


# ============================================================
# メインページ
# ============================================================


def main():
    st.set_page_config(page_title="YouTubeBOT", page_icon="📺", layout="wide")
    init_session_state()

    # personas.json 読み込み（ホットリロード: mtime をキー化）
    ppath = Path(st.session_state.personas_path)
    raw = load_personas(str(ppath), ppath.stat().st_mtime if ppath.exists() else 0.0)
    personas = normalize_personas(raw)

    # 既定選択
    if personas:
        st.session_state.setdefault("selected_persona_name", personas[0].name)
        st.session_state.setdefault(
            "selected_character_name",
            personas[0].characters[0].name if personas[0].characters else "キャラ",
        )

    # 背景CSS / BGM
    render_background_css(st.session_state.bg_url)
    render_bgm_player(st.session_state.bgm_url, float(st.session_state.bgm_volume))

    # サイドバー
    sidebar_controls(personas)

    # メインレイアウト
    left, right = st.columns([7, 5])

    with left:
        st.subheader("📺 配信ビュー")
        vid = st.session_state.get("yt_video_id")
        if vid:
            st_html(
                f"""
                <div style='position:relative;padding-bottom:56.25%;height:0;overflow:hidden;border-radius:12px;'>
                    <iframe src="https://www.youtube.com/embed/{vid}" frameborder="0" allow="autoplay; encrypted-media" allowfullscreen style='position:absolute;top:0;left:0;width:100%;height:100%'></iframe>
                </div>
                """,
                height=360,
            )
        else:
            st.info("未接続です。チャンネル自動検出または手動接続を行ってください。")

        st.markdown("### 💬 チャット送信")
        msg = st.text_input("メッセージ", key="ui_send_text")
        colS1, colS2 = st.columns([1, 1])
        if colS1.button(
            "📤 送信",
            use_container_width=True,
            disabled=not st.session_state.get("yt_live_chat_id"),
        ):
            ok = send_chat_message(
                st.session_state.yt_service, st.session_state.yt_live_chat_id, msg
            )
            append_chat(
                {
                    "time": datetime.now(JST).isoformat(),
                    "author": "Bot",
                    "text": msg,
                    "owner": True,
                    "bot": True,
                    "sent": ok,
                }
            )
        if colS2.button(
            "🙏 定型: 開始挨拶",
            use_container_width=True,
            disabled=not st.session_state.get("yt_live_chat_id"),
        ):
            p, c = current_persona_and_character()
            key = (
                f"start_greet__{p.name}__{c.name}"
                if p and c
                else "start_greet__default"
            )
            text = st.session_state.get(key) or (
                c.greetings.start if c else "配信へようこそ！"
            )
            ok = send_chat_message(
                st.session_state.yt_service, st.session_state.yt_live_chat_id, text
            )
            append_chat(
                {
                    "time": datetime.now(JST).isoformat(),
                    "author": "Bot",
                    "text": text,
                    "owner": True,
                    "bot": True,
                    "sent": ok,
                }
            )

    with right:
        st.subheader("🧭 ステータス")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("接続", "✅" if st.session_state.get("yt_connected") else "❌")
        c2.metric("AI", "ON" if st.session_state.get("ai_enabled") else "OFF")
        c3.metric(
            "監視スレッド",
            (
                "RUN"
                if (
                    st.session_state.get("watcher_thread")
                    and st.session_state.get("watcher_thread").is_alive()
                )
                else "STOP"
            ),
        )
        c4.metric("ゲーム", st.session_state.get("selected_game", "なし"))

        st.code(
            json.dumps(
                {
                    "video_id": st.session_state.get("yt_video_id"),
                    "live_chat_id": st.session_state.get("yt_live_chat_id"),
                    "channel_id": st.session_state.get("yt_channel_id"),
                    "persona": st.session_state.get("selected_persona_name"),
                    "character": st.session_state.get("selected_character_name"),
                    "game": st.session_state.get("selected_game"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        st.markdown("### 📜 チャットログ")
        render_chat_log()


# ============================================================
# エントリーポイント
# ============================================================
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        st.exception(e)
