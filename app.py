# -*- coding: utf-8 -*-
"""
YouTube Live Bot (Streamlit) – Mobile First + Persona Editor (Full)
- スマホ向けミニマル&おしゃれUI（単一カラム / ガラス質感 / ヒーローバナー / チャットバブル）
- YouTube連携：認証→ライブ自動検出→手動接続→監視→送信→自動挨拶
- Gemini連携：AI自動返信（50文字以内）/ ON-OFF / ペルソナ切替
- ゲーム演出：背景画像& BGM 自動切替（/images, /audio）＋音量調整
- ペルソナ管理：既定 personas.json を読み込み、**追加・編集・削除** をWeb UIで実行＆保存
- 安定化：@st.cache_resource / @st.cache_data、threading.Event、chat_lock、例外時の復旧
"""

from __future__ import annotations
import os
import re
import io
import json
import time
import copy
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

# ------------------------------------------------------------
# 汎用安全 index
# ------------------------------------------------------------


def safe_idx(options: List[str], selected: Optional[str], default: int = 0) -> int:
    if not options:
        return 0
    if selected is None:
        return default
    try:
        return options.index(selected)
    except Exception:
        return default


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
# personas.json 読み書き
# ============================================================
@st.cache_data(show_spinner=False)
def load_personas_raw(json_path: str, _mtime: float) -> Dict[str, Any]:
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


def atomic_write_json(path: Path, data: Dict[str, Any]):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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


def personas_to_raw(personas: List[Persona]) -> Dict[str, Any]:
    data = {"personas": []}
    for p in personas:
        entry = {"name": p.name, "characters": []}
        for c in p.characters:
            entry["characters"].append(
                {
                    "name": c.name,
                    "greetings": {
                        "start": c.greetings.start,
                        "end": c.greetings.end,
                        "replies": c.greetings.replies or [],
                    },
                }
            )
        data["personas"].append(entry)
    return data


# ============================================================
# YouTube 認証/サービス
# ============================================================


def ensure_client_secret_ui() -> bool:
    secret_path = Path("client_secret.json")
    if secret_path.exists():
        return True
    st.warning("client_secret.json が見つかりません。下でアップロードしてください。")
    up = st.file_uploader(
        "client_secret.json をアップロード", type=["json"], key="up_client_secret"
    )
    if up is not None:
        secret_path.write_bytes(up.read())
        st.success("client_secret.json を保存しました。もう一度 認証 を実行できます。")
        return True
    return False


def get_credentials() -> Credentials:
    creds: Optional[Credentials] = None
    token_path = Path("token.json")
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not ensure_client_secret_ui():
                raise FileNotFoundError("client_secret.json not found")
            flow = InstalledAppFlow.from_client_secrets_file(
                "client_secret.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


@st.cache_resource(show_spinner=False)
def get_youtube_service(_creds: Credentials):
    return build("youtube", "v3", credentials=_creds, cache_discovery=False)


def ensure_youtube_service() -> bool:
    ss = st.session_state
    if getattr(ss, "yt_service", None) is not None:
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
        # Persona Editor work buffer
        "personas_edit": None,
        "persona_editor_open": False,
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
        @media (max-width: 640px){ .block-container{ padding-top: .75rem; padding-bottom: 4rem; } }
        header[data-testid="stHeader"], footer {visibility: hidden; height: 0;}
        .block-container { backdrop-filter: blur(6px); }
        .stButton>button {
            background: linear-gradient(135deg, #7C3AED 0%, #06B6D4 100%)!important;
            color: white!important; border: none!important; border-radius: 14px!important;
            padding: 0.65rem 1.0rem!important; box-shadow: 0 8px 24px rgba(124,58,237,0.35);
            transition: transform .08s ease, box-shadow .2s ease;
        }
        .stButton>button:hover { transform: translateY(-1px); box-shadow: 0 12px 28px rgba(6,182,212,0.35); }
        .bubble { padding:10px 12px; border-radius:14px; margin:10px 0; animation: pop .15s ease-out; }
        .bubble.bot { background: rgba(255,255,255,0.08); }
        .bubble.user{ background: rgba(0,0,0,0.15); }
        @keyframes pop { from { transform: scale(.98); opacity:.0;} to {transform: scale(1); opacity:1;} }
        .hero { position: relative; border-radius: 16px; overflow: hidden; }
        .hero::after{ content:""; position:absolute; inset:0; background: linear-gradient(180deg, rgba(0,0,0,.35), rgba(0,0,0,.65)); }
        .hero h1 { position:absolute; left:16px; bottom:12px; color:#fff; z-index:2; margin:0; }
        .hero small { position:absolute; left:16px; bottom:48px; color:#e5e7eb; z-index:2; }
        .pill { display:inline-block; padding:6px 10px; margin:4px 6px 0 0; border-radius:12px; background: rgba(255,255,255,0.08); }
        .card { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); padding:14px; border-radius:16px; }
        .muted { color:#9ca3af }
        .danger { color:#ef4444; }
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
        .stApp {{ background-image: url('{url}'); background-size: cover; background-position: center center; background-attachment: fixed; }}
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
        <audio id=\"bgm\" src=\"{url}\" autoplay loop></audio>
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
        <div class=\"hero\" style=\"height:200px;\">
            <img src=\"{url}\" style=\"width:100%; height:100%; object-fit:cover; display:block;\"/>
            <small>Now Playing</small>
            <h1>🎮 {game_title}</h1>
        </div>
        """,
        height=210,
    )


def render_chat_log():
    with st.container(height=460):
        for row in st.session_state.chat_log[-800:]:
            ts = row.get("time")
            author = row.get("author")
            text = row.get("text")
            who_cls = "bot" if row.get("bot") else "user"
            icon = "🤖" if row.get("bot") else "🟢"
            st.markdown(
                f"<div class='bubble {who_cls}'>{icon} <b>{author}</b> <code>[{ts}]</code><br>{text}</div>",
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
# ペルソナ編集 UI
# ============================================================


def ensure_edit_buffer(raw: Dict[str, Any]):
    ss = st.session_state
    if ss.personas_edit is None:
        ss.personas_edit = copy.deepcopy(raw)
        if not isinstance(ss.personas_edit.get("personas"), list):
            ss.personas_edit = {"personas": []}


def persona_editor_ui(
    raw_loaded: Dict[str, Any], json_path: Path
) -> Optional[Dict[str, Any]]:
    ss = st.session_state
    ensure_edit_buffer(raw_loaded)
    data = ss.personas_edit

    st.subheader("🧩 ペルソナ編集（追加・編集・削除）")
    st.caption(
        "既定の personas.json を直接編集して保存します。スマホでも操作しやすい最小UIです。"
    )

    # 追加（ペルソナ）
    with st.container():
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        new_p_name = st.text_input("新規ペルソナ名", key="pe_new_pname")
        if (
            st.button(
                "➕ ペルソナを追加", use_container_width=True, key="btn_add_persona"
            )
            and new_p_name.strip()
        ):
            data.setdefault("personas", []).append(
                {"name": new_p_name.strip(), "characters": []}
            )
            st.success(f"ペルソナ『{new_p_name}』を追加しました")
        st.markdown("</div>", unsafe_allow_html=True)

    # 一覧 & 編集
    personas_list = data.get("personas", [])
    if not personas_list:
        st.info("ペルソナがありません。上で追加してください。")
    for pi, p in enumerate(personas_list):
        with st.expander(f"📦 {p.get('name','(無名)')}", expanded=False):
            # ペルソナ名
            p_name_key = f"pe_pname_{pi}"
            p["name"] = st.text_input(
                "ペルソナ名", value=p.get("name", ""), key=p_name_key
            )

            # キャラ追加
            st.markdown("<div class='card'>", unsafe_allow_html=True)
            c_new_name = st.text_input("新規キャラ名", key=f"pe_new_cname_{pi}")
            c_new_start = st.text_area(
                "開始挨拶",
                key=f"pe_new_cstart_{pi}",
                height=70,
                value="皆さん、こんにちは！配信へようこそ！",
            )
            c_new_end = st.text_area(
                "終了挨拶",
                key=f"pe_new_cend_{pi}",
                height=70,
                value="今日もありがとうございました！",
            )
            c_new_repl = st.text_input(
                "口調ヒント（カンマ区切り）",
                key=f"pe_new_crepl_{pi}",
                value="すごい！, なるほど！, いいね！",
            )
            if (
                st.button(
                    "➕ キャラ追加", use_container_width=True, key=f"btn_add_char_{pi}"
                )
                and c_new_name.strip()
            ):
                replies = [x.strip() for x in c_new_repl.split(",") if x.strip()]
                p.setdefault("characters", []).append(
                    {
                        "name": c_new_name.strip(),
                        "greetings": {
                            "start": c_new_start,
                            "end": c_new_end,
                            "replies": replies,
                        },
                    }
                )
                st.success(f"キャラ『{c_new_name}』を追加しました")
            st.markdown("</div>", unsafe_allow_html=True)

            # 既存キャラ編集
            for ci, c in enumerate(p.get("characters", [])):
                st.markdown("<div class='card'>", unsafe_allow_html=True)
                c["name"] = st.text_input(
                    "キャラ名", value=c.get("name", ""), key=f"pe_cname_{pi}_{ci}"
                )
                g = c.setdefault("greetings", {})
                g["start"] = st.text_area(
                    "開始挨拶",
                    value=g.get("start", ""),
                    key=f"pe_cstart_{pi}_{ci}",
                    height=70,
                )
                g["end"] = st.text_area(
                    "終了挨拶",
                    value=g.get("end", ""),
                    key=f"pe_cend_{pi}_{ci}",
                    height=70,
                )
                repl_str = ", ".join(g.get("replies", []) or [])
                repl_in = st.text_input(
                    "口調ヒント（カンマ区切り）",
                    value=repl_str,
                    key=f"pe_crepl_{pi}_{ci}",
                )
                g["replies"] = [x.strip() for x in repl_in.split(",") if x.strip()]
                cols = st.columns(2)
                with cols[0]:
                    if st.button("🗑️ このキャラを削除", key=f"btn_del_char_{pi}_{ci}"):
                        p.get("characters", []).pop(ci)
                        st.experimental_rerun()
                with cols[1]:
                    st.caption("")
                st.markdown("</div>", unsafe_allow_html=True)

            # ペルソナ削除
            if st.button("🗑️ このペルソナを削除", key=f"btn_del_persona_{pi}"):
                personas_list.pop(pi)
                st.experimental_rerun()

    # 保存 / リセット / エクスポート / インポート
    cols = st.columns(2)
    with cols[0]:
        if st.button(
            "💾 personas.json に保存", use_container_width=True, key="btn_save_personas"
        ):
            try:
                atomic_write_json(json_path, data)
                st.cache_data.clear()
                st.success("保存しました。UIを更新します…")
                st.rerun()
            except Exception as e:
                st.error(f"保存に失敗しました: {e}")
    with cols[1]:
        if st.button(
            "↩️ ディスクの内容でリセット",
            use_container_width=True,
            key="btn_reset_personas",
        ):
            st.session_state.personas_edit = None
            st.cache_data.clear()
            st.rerun()

    # エクスポート
    raw_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "⬇️ personas.json をダウンロード",
        data=raw_bytes,
        file_name="personas.json",
        mime="application/json",
        use_container_width=True,
    )

    # インポート
    up = st.file_uploader(
        "⬆️ personas.json をインポート（置き換え）", type=["json"], key="pe_import"
    )
    if up is not None:
        try:
            new_raw = json.load(io.TextIOWrapper(up, encoding="utf-8"))
            if not isinstance(new_raw.get("personas"), list):
                st.error("不正な形式です。'personas' が配列ではありません。")
            else:
                ss.personas_edit = new_raw
                st.success("編集バッファに読み込みました。保存ボタンで確定します。")
        except Exception as e:
            st.error(f"読み込みに失敗しました: {e}")

    return data


# ============================================================
# メインコントロール（サイドバー廃止・縦並び）
# ============================================================


def controls_ui(personas: List[Persona], raw_loaded: Dict[str, Any]):
    ss = st.session_state

    # 1) 認証・サービス
    st.subheader("1️⃣ 認証・サービス")
    if st.button("🔐 Google 認証 / 初期化", use_container_width=True):
        ensure_youtube_service()
    if st.button("♻️ サービス再生成", use_container_width=True):
        st.cache_resource.clear()
        ensure_youtube_service()

    # 2) 配信に接続
    st.subheader("2️⃣ 配信に接続")
    ss.yt_channel_id = st.text_input(
        "チャンネルID（ライブ自動検出）", value=ss.yt_channel_id
    )
    if st.button("📡 ライブ検出して接続", use_container_width=True):
        if ensure_youtube_service():
            with st.spinner("ライブを検索中..."):
                vid = search_live_video_id_by_channel(ss.yt_service, ss.yt_channel_id)
            if not vid:
                st.warning(
                    "ライブ配信が見つかりませんでした。手動接続をご利用ください。"
                )
            else:
                connect_to_video_id(vid)
    manual = st.text_input("ライブURL または videoId")
    if st.button("🔗 手動接続", use_container_width=True):
        if ensure_youtube_service():
            vid = extract_video_id(manual)
            if not vid:
                st.warning("URL/ID を正しく入力してください")
            else:
                connect_to_video_id(vid)

    # 3) AI / ペルソナ
    st.subheader("3️⃣ AI / ペルソナ")
    ss.ai_enabled = st.toggle("AI応答を有効化", value=ss.ai_enabled)
    ppath = Path(ss.personas_path)
    if st.button("🔄 personas.json を再読込", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    ss.personas_path = st.text_input("personas.json パス", value=str(ppath))

    persona_names = [p.name for p in personas] or ["デフォルト"]
    current_p = ss.get("selected_persona_name") or persona_names[0]
    sel_persona = st.selectbox(
        "ペルソナ", persona_names, index=safe_idx(persona_names, current_p)
    )
    if sel_persona != ss.get("selected_persona_name"):
        ss.selected_persona_name = sel_persona
    persona_obj = next(
        (p for p in personas if p.name == sel_persona),
        (personas[0] if personas else Persona("デフォルト", [])),
    )

    char_names = [c.name for c in persona_obj.characters] or ["キャラ"]
    current_c = ss.get("selected_character_name") or char_names[0]
    sel_char = st.selectbox(
        "キャラクター", char_names, index=safe_idx(char_names, current_c)
    )
    if sel_char != ss.get("selected_character_name"):
        ss.selected_character_name = sel_char

    ch = next((c for c in persona_obj.characters if c.name == sel_char), None)
    if ch is None and persona_obj.characters:
        ch = persona_obj.characters[0]
    start_key = (
        f"start_greet__{persona_obj.name}__{ch.name}" if ch else "start_greet__default"
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

    # 3.5) ペルソナ編集（トグル）
    if st.toggle(
        "🧩 ペルソナ編集を開く", key="toggle_open_editor", value=ss.persona_editor_open
    ):
        ss.persona_editor_open = True
        persona_editor_ui(raw_loaded, Path(ss.personas_path))
    else:
        ss.persona_editor_open = False

    # 4) ゲーム演出
    st.subheader("4️⃣ ゲーム演出")
    games = ["なし"] + list(GAME_MEDIA.keys())
    current_g = ss.get("selected_game") or "なし"
    game_choice = st.selectbox("ゲームを選択", games, index=safe_idx(games, current_g))
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

    # 5) 監視
    st.subheader("5️⃣ 監視")
    if st.button(
        "▶️ 監視開始", use_container_width=True, disabled=not ss.get("yt_live_chat_id")
    ):
        start_watch(personas)
    if st.button("⏹️ 停止", use_container_width=True):
        stop_watch(send_goodbye=ss.auto_greet)


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
    st.set_page_config(page_title="YouTubeBOT", page_icon="📺", layout="centered")
    inject_global_css()
    init_session_state()

    # personas.json ホットリロード
    ppath = Path(st.session_state.personas_path)
    raw_loaded = load_personas_raw(
        str(ppath), ppath.stat().st_mtime if ppath.exists() else 0.0
    )
    personas = normalize_personas(raw_loaded)

    if personas:
        st.session_state.setdefault("selected_persona_name", personas[0].name)
        first_char = (
            personas[0].characters[0].name if personas[0].characters else "キャラ"
        )
        st.session_state.setdefault("selected_character_name", first_char)

    # 背景/BGM + ヒーローバナー
    render_background_css(st.session_state.bg_url)
    render_bgm_player(st.session_state.bgm_url, float(st.session_state.bgm_volume))
    game = st.session_state.get("selected_game", "なし")
    cover = GAME_MEDIA.get(game, {}).get("image") if game != "なし" else None
    if cover:
        hero_banner(game, cover)

    # コントロール（縦）
    controls_ui(personas, raw_loaded)

    # ステータス
    st.subheader("🧭 ステータス")
    st.markdown(
        f"<span class='pill'>接続: {'✅' if st.session_state.get('yt_connected') else '❌'}</span>"
        f"<span class='pill'>AI: {'ON' if st.session_state.get('ai_enabled') else 'OFF'}</span>"
        f"<span class='pill'>監視: {'RUN' if (st.session_state.get('watcher_thread') and st.session_state.get('watcher_thread').is_alive()) else 'STOP'}</span>"
        f"<span class='pill'>ゲーム: {st.session_state.get('selected_game','なし')}</span>",
        unsafe_allow_html=True,
    )

    # 配信ビュー
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

    # 送信 & ログ
    st.subheader("💬 チャット送信")
    msg = st.text_input("メッセージ", key="ui_send_text")
    if st.button(
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
    if st.button(
        "🙏 定型: 開始挨拶",
        use_container_width=True,
        disabled=not st.session_state.get("yt_live_chat_id"),
    ):
        p, c = current_persona_and_character()
        key = (
            f"start_greet__{p.name}__{c.name}" if (p and c) else "start_greet__default"
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
    if st.button(
        "🙇 定型: 終了挨拶",
        use_container_width=True,
        disabled=not st.session_state.get("yt_live_chat_id"),
    ):
        p, c = current_persona_and_character()
        key = f"end_greet__{p.name}__{c.name}" if (p and c) else "end_greet__default"
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

    st.subheader("📜 チャットログ")
    render_chat_log()


# ============================================================
# エントリーポイント
# ============================================================
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        st.exception(e)
