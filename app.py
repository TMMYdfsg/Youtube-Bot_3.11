# -*- coding: utf-8 -*-
"""
YouTubeBOT（統合・完全改良版 / Ultra UI）
- Streamlit 管理画面（ガラス質感 + グラデーション + チャットバブル）
- YouTube Live 自動/手動接続、チャット監視、投稿（接続ボタンの反応性改善）
- Google Gemini による50文字以内の自動応答（ペルソナ切替）
- personas.json ホットリロード & フォールバック
- ゲームごとの画像・BGM自動切替（/images, /audio）
- ローカルファイルは data:URI に自動変換
- KeyError（chat_lock など）を回避する堅牢なセッション初期化

必要ファイル:
- client_secret.json / token.json
- personas.json
- /images/*.jpg, /audio/*.mp3（ユーザー指定のファイル名対応）
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
    genai = None

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
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    mime, _ = mimetypes.guess_type(str(p))
    if not mime:
        ext = p.suffix.lower()
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
        }.get(ext, "application/octet-stream")
    b = p.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(b).decode('ascii')}"


# ============================================================
# ペルソナ
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
                            start="皆さん、こんにちは！配信へようこそ！",
                            end="今日もありがとうございました！",
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
# YouTube 認証/サービス
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
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


@st.cache_resource(show_spinner=False)
def get_youtube_service(_creds: Credentials):
    return build("youtube", "v3", credentials=_creds, cache_discovery=False)


# 反応しない対策：必要時にサービスを自動初期化


def ensure_youtube_service() -> bool:
    ss = st.session_state
    if hasattr(ss, "yt_service") and ss.yt_service is not None:
        return True
    try:
        with st.spinner("YouTube サービスを初期化中..."):
            creds = get_credentials()
            ss.yt_service = get_youtube_service(creds)
        st.success("YouTube サービス初期化OK")
        return True
    except Exception as e:
        st.error(f"YouTube サービス初期化に失敗: {e}")
        return False


# ============================================================
# YouTube API 小物
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
    s = (url_or_id or "").strip()
    if not s:
        return None
    if len(s) == 11 and re.match(r"^[A-Za-z0-9_-]{11}$", s):
        return s
    m = YOUTUBE_ID_RE.search(s)
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
        return genai.GenerativeModel("gemini-1.5-flash")
    except Exception as e:
        st.error(f"Gemini 初期化エラー: {e}")
        return None


def build_persona_prompt(persona: Persona, character: Character) -> str:
    replies = character.greetings.replies or []
    style = " / ".join(replies[:6]) if replies else "丁寧"
    return (
        f"あなたは『{persona.name}』のキャラクター『{character.name}』として返信します。"
        f" 50文字以内の短い応答を1つだけ返してください。絵文字は控えめに。"
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
            return False
        now = time.time()
        if now - self.last_reply_at.get(author_channel_id, 0) < self.rate_limit_sec:
            return False
        self.last_reply_at[author_channel_id] = now
        return True

    def run(self):
        polling_interval = 3.0
        while not self.stop_event.is_set():
            try:
                resp = (
                    self.youtube.liveChatMessages()
                    .list(
                        liveChatId=self.live_chat_id,
                        part="snippet,authorDetails",
                        pageToken=self.next_page_token,
                    )
                    .execute()
                )
                self.next_page_token = resp.get("nextPageToken")
                polling_interval = max(
                    1.0, resp.get("pollingIntervalMillis", 3000) / 1000.0
                )

                for item in resp.get("items", []):
                    snip = item.get("snippet", {})
                    auth = item.get("authorDetails", {})
                    text = snip.get("textMessageDetails", {}).get("messageText")
                    if text is None:
                        continue
                    ts = snip.get("publishedAt")
                    author_name = auth.get("displayName", "?")
                    author_channel_id = auth.get("channelId")
                    is_owner = auth.get("isChatOwner", False) or auth.get(
                        "isChatModerator", False
                    )

                    self.on_message(
                        {
                            "time": ts,
                            "author": author_name,
                            "text": text,
                            "owner": is_owner,
                            "bot": False,
                        }
                    )

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
# セッション & チャット管理
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
        "my_channel_id": None,
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
        ss["chat_lock"] = threading.Lock()
    with ss.chat_lock:
        ss.chat_log.append(row)


# ============================================================
# UI（スタイル + コンポーネント）
# ============================================================


def inject_global_css():
    st.markdown(
        """
        <style>
        /* Hide default header/footer */
        header[data-testid="stHeader"], footer {visibility: hidden; height: 0;}

        /* Glassy container */
        .block-container { backdrop-filter: blur(6px); }

        /* Buttons */
        .stButton>button {
            background: linear-gradient(135deg, #7C3AED 0%, #06B6D4 100%)!important;
            color: white!important;
            border: none!important;
            border-radius: 14px!important;
            padding: 0.6rem 1.0rem!important;
            box-shadow: 0 8px 24px rgba(124,58,237,0.35);
            transition: transform .08s ease, box-shadow .2s ease;
        }
        .stButton>button:hover { transform: translateY(-1px); box-shadow: 0 12px 28px rgba(6,182,212,0.35); }

        /* Metrics (pill style) */
        [data-testid="stMetric"] { background: rgba(255,255,255,0.08); padding: 10px 12px; border-radius: 14px; }

        /* Chat bubble */
        .bubble { padding:10px 12px; border-radius:14px; margin-bottom:10px; animation: pop .15s ease-out; }
        .bubble.bot { background: rgba(255,255,255,0.08); }
        .bubble.user{ background: rgba(0,0,0,0.15); }
        @keyframes pop { from { transform: scale(.98); opacity:.0;} to {transform: scale(1); opacity:1;} }

        /* Hero banner */
        .hero { position: relative; border-radius: 16px; overflow: hidden; }
        .hero::after{ content:""; position:absolute; inset:0; background: linear-gradient(180deg, rgba(0,0,0,.35), rgba(0,0,0,.65)); }
        .hero h1 { position:absolute; left:16px; bottom:12px; color:#fff; z-index:2; margin:0; }
        .hero small { position:absolute; left:16px; bottom:48px; color:#e5e7eb; z-index:2; }
        </style>
        """,
        unsafe_allow_html=True,
    )


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
            background-size: cover; background-position: center center; background-attachment: fixed;
        }}
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
        <script>const audio=document.getElementById('bgm'); if(audio) audio.volume={vol};</script>
        """,
        height=0,
    )


def hero_banner(game_title: str, cover_src: Optional[str]):
    if not cover_src:
        return
    url = cover_src
    if not is_url(cover_src):
        data = file_to_data_url(cover_src)
        if data:
            url = data
        else:
            return
    st_html(
        f"""
        <div class="hero" style="height:200px;">
            <img src="{url}" style="width:100%; height:100%; object-fit:cover; display:block;"/>
            <small>Now Playing</small>
            <h1>🎮 {game_title}</h1>
        </div>
        """,
        height=210,
    )


def render_chat_log():
    with st.container(height=440):
        for row in st.session_state.chat_log[-600:]:
            ts = row.get("time")
            author = row.get("author")
            text = row.get("text")
            who_cls = "bot" if row.get("bot") else "user"
            icon = "🤖" if row.get("bot") else "🟢"
            st.markdown(
                f"<div class='bubble {who_cls}'>"
                f"{icon} <b>{author}</b> <code>[{ts}]</code><br>{text}</div>",
                unsafe_allow_html=True,
            )


# ============================================================
# ゲームメディア定義
# ============================================================
GAME_MEDIA = {
    "Dead by Daylight": {
        "image": "images/Dead by Daylight.jpg",
        "audio": "audio/Dead by Daylight.mp3",
    },
    "Fortnite": {"image": "images/Fortnite.jpg", "audio": "audio/Fortnite.mp3"},
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
    "原神": {"image": "images/原神.jpg", "audio": "audio/原神.mp3"},
    "鳴潮": {"image": "images/鳴潮.jpg", "audio": "audio/鳴潮.mp3"},
}

# ============================================================
# サイドバー
# ============================================================


def sidebar_controls(personas: List[Persona]):
    ss = st.session_state
    with st.sidebar:
        st.subheader("⚙️ コントロール")

        # 認証/サービス
        auth_col1, auth_col2 = st.columns([1, 1])
        if auth_col1.button("🔐 Google 認証", use_container_width=True):
            ensure_youtube_service()
        if auth_col2.button("♻️ サービス再生成", use_container_width=True):
            st.cache_resource.clear()
            ensure_youtube_service()

        # 接続
        st.divider()
        st.markdown("**🔴 配信に接続**")
        ss.yt_channel_id = st.text_input(
            "チャンネルID（ライブ自動検出）", value=ss.yt_channel_id
        )
        colA, colB = st.columns([1, 1])
        with colA:
            if st.button("📡 ライブ検出して接続", use_container_width=True):
                if ensure_youtube_service():
                    with st.spinner("ライブを検索中..."):
                        vid = search_live_video_id_by_channel(
                            ss.yt_service, ss.yt_channel_id
                        )
                    if not vid:
                        st.warning(
                            "ライブ配信が見つかりませんでした。手動接続をご利用ください。"
                        )
                    else:
                        connect_to_video_id(vid)
        with colB:
            manual = st.text_input("ライブURL または videoId")
            if st.button("🔗 手動接続", use_container_width=True):
                if ensure_youtube_service():
                    vid = extract_video_id(manual)
                    if not vid:
                        st.warning("URL/ID を正しく入力してください")
                    else:
                        connect_to_video_id(vid)

        # AI / ペルソナ
        st.divider()
        st.markdown("**🤖 AI 応答**")
        ss.ai_enabled = st.toggle("AI応答を有効化", value=ss.ai_enabled)
        ppath = Path(ss.personas_path)
        colR1, colR2 = st.columns([1, 1])
        if colR1.button("🔄 ペルソナ再読込", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        ss.personas_path = st.text_input("personas.json パス", value=str(ppath))

        persona_names = [p.name for p in personas] or ["デフォルト"]
        sel_persona = st.selectbox(
            "ペルソナ",
            persona_names,
            index=max(
                0,
                persona_names.index(ss.get("selected_persona_name", persona_names[0])),
            ),
        )
        if sel_persona != ss.get("selected_persona_name"):
            ss.selected_persona_name = sel_persona
        persona_obj = next(
            (p for p in personas if p.name == sel_persona),
            (personas[0] if personas else Persona("デフォルト", [])),
        )

        char_names = [c.name for c in persona_obj.characters] or ["キャラ"]
        sel_char = st.selectbox(
            "キャラクター",
            char_names,
            index=max(
                0, char_names.index(ss.get("selected_character_name", char_names[0]))
            ),
        )
        if sel_char != ss.get("selected_character_name"):
            ss.selected_character_name = sel_char

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
        st.text_area(
            "開始挨拶（接続時に送信可）",
            value=(ch.greetings.start if ch else "配信開始のご挨拶です！"),
            key=start_key,
            height=80,
        )
        st.text_area(
            "終了挨拶（切断時に送信可）",
            value=(ch.greetings.end if ch else "本日はありがとうございました！"),
            key=end_key,
            height=80,
        )
        ss.auto_greet = st.toggle("接続/切断で自動挨拶", value=ss.auto_greet)

        # ゲーム演出
        st.divider()
        st.markdown("**🎮 ゲーム演出**")
        games = ["なし"] + list(GAME_MEDIA.keys())
        game_choice = st.selectbox(
            "ゲームを選択", games, index=games.index(ss.get("selected_game", "なし"))
        )
        if game_choice != ss.get("selected_game"):
            ss.selected_game = game_choice
        if game_choice != "なし":
            media = GAME_MEDIA[game_choice]
            ss.bg_url = media["image"]
            ss.bgm_url = media["audio"]
        else:
            ss.bg_url = st.text_input("背景画像パス/URL", value=ss.bg_url)
            ss.bgm_url = st.text_input("BGM パス/URL (mp3/m4a/ogg)", value=ss.bgm_url)
        ss.bgm_volume = st.slider("BGM 音量", 0.0, 1.0, float(ss.bgm_volume), 0.01)

        # 監視
        st.divider()
        ctrl1, ctrl2 = st.columns([1, 1])
        if ctrl1.button(
            "▶️ 監視開始",
            use_container_width=True,
            disabled=not ss.get("yt_live_chat_id"),
        ):
            start_watch(st.session_state.get("_personas", []))
        if ctrl2.button("⏹️ 停止", use_container_width=True):
            stop_watch(send_goodbye=ss.auto_greet)

        st.divider()
        st.caption("© YouTubeBOT / Streamlit")


# ============================================================
# 接続/監視
# ============================================================


def connect_to_video_id(video_id: str):
    ss = st.session_state
    if not ensure_youtube_service():
        return
    with st.spinner("ライブチャットIDを取得中..."):
        live_chat_id = get_live_chat_id(ss.yt_service, video_id)
    if not live_chat_id:
        st.warning("この動画にはアクティブなライブチャットがありません。")
        return
    ss.yt_video_id = video_id
    ss.yt_live_chat_id = live_chat_id
    ss.yt_connected = True
    st.success("YouTube に接続しました ✅")

    if ss.auto_greet and ss.ai_enabled is not None:
        persona, ch = current_persona_and_character()
        start_key = (
            f"start_greet__{persona.name}__{ch.name}"
            if (persona and ch)
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

    model = setup_gemini(ss.gemini_api_key) if ss.ai_enabled else None
    ss._personas = personas
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
            if (persona and ch)
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


# ============================================================
# メイン
# ============================================================


def main():
    st.set_page_config(page_title="YouTubeBOT", page_icon="📺", layout="wide")
    inject_global_css()
    init_session_state()

    # personas.json ホットリロード
    ppath = Path(st.session_state.personas_path)
    raw = load_personas(str(ppath), ppath.stat().st_mtime if ppath.exists() else 0.0)
    personas = normalize_personas(raw)

    if personas:
        st.session_state.setdefault("selected_persona_name", personas[0].name)
        st.session_state.setdefault(
            "selected_character_name",
            personas[0].characters[0].name if personas[0].characters else "キャラ",
        )

    # 背景/BGM
    render_background_css(st.session_state.bg_url)
    render_bgm_player(st.session_state.bgm_url, float(st.session_state.bgm_volume))

    # ヒーローバナー（選択ゲームのカバー）
    game = st.session_state.get("selected_game", "なし")
    cover = GAME_MEDIA.get(game, {}).get("image") if game != "なし" else None
    if cover:
        hero_banner(game, cover)

    # サイドバー
    sidebar_controls(personas)

    # メイン
    left, right = st.columns([7, 5])
    with left:
        st.subheader("📺 配信ビュー")
        vid = st.session_state.get("yt_video_id")
        if vid:
            st_html(
                f"""
                <div style='position:relative;padding-bottom:56.25%;height:0;overflow:hidden;border-radius:14px;'>
                    <iframe src="https://www.youtube.com/embed/{vid}" frameborder="0" allow="autoplay; encrypted-media" allowfullscreen style='position:absolute;top:0;left:0;width:100%;height:100%'></iframe>
                </div>
                """,
                height=360,
            )
            st.markdown(f"[🔗 YouTube で開く](https://www.youtube.com/watch?v={vid})")
        else:
            st.info("未接続です。チャンネル自動検出または手動接続を行ってください。")

        st.markdown("### 💬 チャット送信")
        msg = st.text_input("メッセージ", key="ui_send_text")
        colS1, colS2, colS3 = st.columns([1, 1, 1])
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
                if (p and c)
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
        if colS3.button(
            "🙇 定型: 終了挨拶",
            use_container_width=True,
            disabled=not st.session_state.get("yt_live_chat_id"),
        ):
            p, c = current_persona_and_character()
            key = (
                f"end_greet__{p.name}__{c.name}" if (p and c) else "end_greet__default"
            )
            text = st.session_state.get(key) or (
                c.greetings.end if c else "ご視聴ありがとうございました！"
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
            "監視",
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
