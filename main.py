# =========================
# Longvinter Steam Tracker Bot
# =========================

import discord
from discord import app_commands
from discord.ext import commands, tasks

import aiohttp
import sqlite3
import os
import asyncio
import re
import xml.etree.ElementTree as ET

from contextlib import contextmanager
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

# =========================
# 설정
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")
STEAM_API_KEY = os.getenv("STEAM_API_KEY")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

# =========================
# Volume DB 자동 복원
# Volume에 DB가 없을 때 GitHub에 있는 초기 DB를 복사
# =========================

_SEED_DB_PATH = "bot_data.db"  # GitHub에 올라간 초기 DB 경로

def restore_db_if_missing():
    if DB_PATH == _SEED_DB_PATH:
        return  # Volume 미사용 환경이면 스킵

    import shutil

    seed_exists = os.path.exists(_SEED_DB_PATH)
    volume_exists = os.path.exists(DB_PATH)

    if not seed_exists:
        print(f"[DB] 초기 DB 없음. 새 DB로 시작합니다.")
        return

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    if not volume_exists:
        shutil.copy2(_SEED_DB_PATH, DB_PATH)
        print(f"[DB] 초기 DB 복원 완료: {_SEED_DB_PATH} → {DB_PATH}")
        return

    # Volume DB가 있어도 seed DB보다 작으면 (빈 DB면) 덮어쓰기
    seed_size = os.path.getsize(_SEED_DB_PATH)
    volume_size = os.path.getsize(DB_PATH)

    if volume_size < seed_size:
        shutil.copy2(_SEED_DB_PATH, DB_PATH)
        print(f"[DB] Volume DB가 초기 DB보다 작아 덮어씀 ({volume_size} < {seed_size})")
    else:
        print(f"[DB] Volume DB 사용 중: {DB_PATH} ({volume_size} bytes)")

restore_db_if_missing()

SAVE_DIR = os.getenv(
    "SAVE_DIR",
    r"C:\Users\peal\AppData\Local\Longvinter\Saved\SaveGames"
)

WATCH_FILES = [
    "[KR]Uuvana1-seenplayers.sav",
    "[KR]Uuvana2-seenplayers.sav",
    "[KR]Uuvana3-seenplayers.sav",
    "[KR]UuvanaHARDCORE-seenplayers.sav"
]

# =========================
# DB
# =========================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        timeout=60,
        check_same_thread=False
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")  # 60초 대기
    return conn


@contextmanager
def db_connection(auto_commit: bool = True):
    import time
    max_retries = 5
    for attempt in range(max_retries):
        conn = get_db()
        try:
            yield conn
            if auto_commit:
                conn.commit()
            return
        except sqlite3.OperationalError as e:
            conn.rollback()
            conn.close()
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
        except Exception:
            conn.rollback()
            conn.close()
            raise
        finally:
            try:
                conn.close()
            except:
                pass


def ensure_column(cursor: sqlite3.Cursor, table: str, column: str, column_type: str):
    allowed_tables = {"users", "channels", "nickname_history"}
    allowed_types = {"TEXT", "INTEGER", "INTEGER DEFAULT 1", "INTEGER DEFAULT 0"}

    if table not in allowed_tables:
        raise ValueError(f"허용되지 않은 테이블: {table}")
    if column_type not in allowed_types:
        raise ValueError(f"허용되지 않은 타입: {column_type}")

    cursor.execute(f"PRAGMA table_info({table})")
    columns = [x[1] for x in cursor.fetchall()]

    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def init_db():
    with db_connection() as conn:
        cursor = conn.cursor()

        # users
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                steam_id TEXT PRIMARY KEY,
                name_key TEXT UNIQUE
            )
        """)
        ensure_column(cursor, "users", "history", "TEXT")
        ensure_column(cursor, "users", "current_name", "TEXT")
        ensure_column(cursor, "users", "eos_id", "TEXT")
        ensure_column(cursor, "users", "is_monitored", "INTEGER DEFAULT 0")
        ensure_column(cursor, "users", "updated_at", "TEXT")

        # channels
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                guild_id TEXT PRIMARY KEY,
                admin_id INTEGER,
                notify_id INTEGER
            )
        """)

        # nickname_history
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nickname_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                steam_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                changed_at TEXT NOT NULL
            )
        """)

        # 인덱스: nickname_history 조회 성능 향상
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_nickname_history_steam_id
            ON nickname_history (steam_id, id)
        """)

        # 기존 history 컬럼 → nickname_history 테이블 마이그레이션
        cursor.execute("SELECT steam_id, history, current_name FROM users")
        rows = cursor.fetchall()

        for sid, history_text, current_name in rows:
            if not history_text:
                continue

            nicknames = [x.strip() for x in history_text.split("|") if x.strip()]

            for nick in nicknames:
                cursor.execute("""
                    INSERT INTO nickname_history (steam_id, nickname, changed_at)
                    SELECT ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM nickname_history
                        WHERE steam_id = ? AND nickname = ?
                    )
                """, (sid, nick, datetime.now().isoformat() + "_MIGRATED", sid, nick))

            if not current_name and nicknames:
                cursor.execute(
                    "UPDATE users SET current_name = ? WHERE steam_id = ?",
                    (nicknames[-1], sid)
                )

            cursor.execute(
                "UPDATE users SET is_monitored = COALESCE(is_monitored, 0) WHERE steam_id = ?",
                (sid,)
            )


init_db()

# =========================
# UTIL
# =========================

def chunked(lst: list, size: int):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def unique_name_key(cursor: sqlite3.Cursor, base: str) -> str:
    candidate = base
    count = 1
    while True:
        cursor.execute("SELECT 1 FROM users WHERE name_key = ?", (candidate,))
        if not cursor.fetchone():
            return candidate
        candidate = f"{base}_{count}"
        count += 1

# =========================
# Steam API (aiohttp 세션 재사용)
# =========================

_http_session: aiohttp.ClientSession | None = None


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def get_steam_users_info(steam_ids: list[str]) -> list[dict]:
    if not steam_ids:
        return []

    all_players = []
    session = await get_http_session()

    for chunk in chunked(steam_ids, 100):
        ids_str = ",".join(chunk)
        url = (
            "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
            f"?key={STEAM_API_KEY}&steamids={ids_str}"
        )
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as res:
                if res.status == 200:
                    data = await res.json()
                    players = data.get("response", {}).get("players", [])
                    all_players.extend(players)
        except Exception as e:
            print(f"[Steam API 오류] {e}")

    return all_players


async def get_nickname_from_xml(steam_id: str) -> str | None:
    url = f"https://steamcommunity.com/profiles/{steam_id}/?xml=1"
    session = await get_http_session()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as res:
            if res.status == 200:
                content = await res.read()
                root = ET.fromstring(content)
                node = root.find("steamID")
                if node is not None and node.text:
                    return node.text.strip()
    except Exception:
        pass
    return None


async def get_current_nickname(sid: str, player: dict | None = None) -> str | None:
    if player:
        name = player.get("personaname", "").strip()
        if name:
            return name
    return await get_nickname_from_xml(sid)

# =========================
# SAV 파싱
# =========================

_SAV_STEAM_ID_PATTERN = re.compile(rb"7656119\d{10}")


def parse_sav_file(file_bytes: bytes) -> list[dict]:
    try:
        steam_ids = set(
            m.group().decode("ascii")
            for m in _SAV_STEAM_ID_PATTERN.finditer(file_bytes)
        )
        return [{"steam_id": sid} for sid in steam_ids]
    except Exception as e:
        print(f"[SAV 파싱 오류] {e}")
        return []

# =========================
# 닉네임 히스토리
# =========================

def get_history_list(cursor: sqlite3.Cursor, steam_id: str) -> list[str]:
    cursor.execute("""
        SELECT nickname FROM nickname_history
        WHERE steam_id = ?
        ORDER BY id ASC
    """, (steam_id,))
    return [row[0] for row in cursor.fetchall()]


def add_history_if_needed(cursor: sqlite3.Cursor, steam_id: str, nickname: str) -> bool:
    cursor.execute("""
        SELECT nickname FROM nickname_history
        WHERE steam_id = ?
        ORDER BY id DESC LIMIT 1
    """, (steam_id,))
    row = cursor.fetchone()

    if row and row[0] == nickname:
        return False

    cursor.execute("""
        INSERT INTO nickname_history (steam_id, nickname, changed_at)
        VALUES (?, ?, ?)
    """, (steam_id, nickname, datetime.now().isoformat()))
    return True

# =========================
# EMBED
# =========================

def create_status_embed(
    display_name: str,
    sid: str,
    history: list[str],
    mode: str = "notify",
    player: dict | None = None
) -> discord.Embed:

    colors = {
        "add":     discord.Color.green(),
        "notify":  discord.Color.gold(),
        "history": discord.Color.blue(),
        "exist":   discord.Color.red()
    }
    titles = {
        "add":     "✨ 감시 등록 완료",
        "notify":  "🔔 닉네임 변경 감지",
        "history": "📋 닉네임 변경 내역",
        "exist":   "❌ 이미 등록됨"
    }

    embed = discord.Embed(
        title=titles.get(mode, "알림"),
        color=colors.get(mode, discord.Color.default())
    )

    if player and player.get("avatarfull"):
        embed.set_thumbnail(url=player["avatarfull"])

    embed.add_field(name="등록 별명", value=display_name, inline=False)
    embed.add_field(
        name="현재 닉네임",
        value=history[-1] if history else "없음",
        inline=False
    )

    history_text = " → ".join(history) if history else "없음"
    if len(history_text) > 1000:
        history_text = "…" + history_text[-997:]

    embed.add_field(
        name=f"닉네임 기록 ({len(history)}개)",
        value=history_text,
        inline=False
    )
    embed.add_field(
        name="Steam 프로필",
        value=f"https://steamcommunity.com/profiles/{sid}",
        inline=False
    )

    return embed

# =========================
# SAV 스캔: 신규 유저 DB 등록만 담당
# (닉네임 추적은 nickname_track_loop에서 별도 처리)
# =========================

async def scan_sav_and_register() -> list[str]:
    """
    SAV 파일을 스캔해 DB에 없는 신규 Steam ID를 등록합니다.
    - is_monitored=0 (알림 비활성) 상태로 등록
    - 닉네임은 등록 시점에 Steam API로 조회해 저장
    - 이미 DB에 있는 유저는 건드리지 않음
    반환값: 새로 등록된 steam_id 목록
    """
    new_ids: list[str] = []
    found_ids: set[str] = set()

    # SAV 파일에서 모든 Steam ID 수집
    for filename in WATCH_FILES:
        path = os.path.join(SAVE_DIR, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                file_bytes = f.read()
            parsed = parse_sav_file(file_bytes)
            for item in parsed:
                found_ids.add(item["steam_id"])
        except Exception as e:
            print(f"[SAV 스캔 오류] {filename}: {e}")

    if not found_ids:
        return new_ids

    # DB에 없는 신규 ID만 필터링
    with db_connection(auto_commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT steam_id FROM users")
        existing_ids = {row[0] for row in cursor.fetchall()}

    truly_new = list(found_ids - existing_ids)
    if not truly_new:
        return new_ids

    print(f"[SAV 신규 발견] {len(truly_new)}명")

    # 신규 유저 닉네임 조회 (100명씩 배치)
    players = await get_steam_users_info(truly_new)
    p_dict = {p["steamid"]: p for p in players}

    with db_connection() as conn:
        cursor = conn.cursor()
        for sid in truly_new:
            player = p_dict.get(sid)
            current_name = await get_current_nickname(sid, player)

            if current_name is None:
                # 닉네임 조회 실패 시 임시 키로만 등록
                current_name = ""

            base_key = f"user_{sid[-6:]}"
            name_key = unique_name_key(cursor, base_key)

            cursor.execute("""
                INSERT OR IGNORE INTO users
                    (steam_id, name_key, current_name, is_monitored, updated_at)
                VALUES (?, ?, ?, 0, ?)
            """, (sid, name_key, current_name, datetime.now().isoformat()))

            if current_name:
                add_history_if_needed(cursor, sid, current_name)

            new_ids.append(sid)
            print(f"[신규 등록] {current_name or '(이름없음)'} ({sid}) → {name_key}")

    return new_ids


# =========================
# 전체 유저 닉네임 추적
# - 모든 유저(is_monitored 무관)의 닉네임 변경을 히스토리에 저장
# - is_monitored=1 유저만 알림 대상으로 반환
# =========================

async def track_all_nicknames() -> list[tuple]:
    """
    DB의 모든 유저 닉네임을 Steam API로 갱신합니다.
    닉네임이 변경되고 is_monitored=1인 유저만 알림 대상으로 반환합니다.

    반환값: [(name_key, sid, history, player), ...]
    """
    pending_notify: list[tuple] = []

    # 전체 유저 조회
    try:
        with db_connection(auto_commit=False) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT steam_id, name_key, current_name, is_monitored
                FROM users
            """)
            all_users = cursor.fetchall()
    except Exception as e:
        print(f"[track_all_nicknames DB 조회 오류] {e}")
        return pending_notify

    if not all_users:
        return pending_notify

    steam_ids = [row[0] for row in all_users]
    players = await get_steam_users_info(steam_ids)
    p_dict = {p["steamid"]: p for p in players}

    try:
        with db_connection() as conn:
            cursor = conn.cursor()

            for sid, name_key, old_name, is_monitored in all_users:
                player = p_dict.get(sid)
                curr = await get_current_nickname(sid, player)

                if curr is None:
                    # 조회 실패: 스킵 (히스토리도 건드리지 않음)
                    continue

                if curr == old_name:
                    # 변경 없음
                    continue

                # 닉네임 변경 감지 → DB 갱신
                cursor.execute("""
                    UPDATE users SET current_name = ?, updated_at = ?
                    WHERE steam_id = ?
                """, (curr, datetime.now().isoformat(), sid))

                changed = add_history_if_needed(cursor, sid, curr)

                # is_monitored=1인 유저만 알림 목록에 추가
                if changed and is_monitored == 1:
                    history = get_history_list(cursor, sid)
                    pending_notify.append((name_key, sid, history, player))
                    print(f"[닉변 감지 → 알림 예정] {name_key}: {old_name} → {curr}")
                elif changed:
                    print(f"[닉변 기록] {name_key}: {old_name} → {curr} (알림 비활성)")

    except Exception as e:
        print(f"[track_all_nicknames 갱신 오류] {e}")

    return pending_notify

# =========================
# BOT
# =========================

class MyBot(commands.Bot):

    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=discord.Intents.all()
        )
        self.scan_lock = asyncio.Lock()
        self.track_lock = asyncio.Lock()

    async def setup_hook(self):
        synced = await self.tree.sync()
        print(f"슬래시 명령어 동기화 완료: {len(synced)}개")

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        if not self.sav_scan_loop.is_running():
            self.sav_scan_loop.start()
        if not self.nickname_track_loop.is_running():
            self.nickname_track_loop.start()

    async def close(self):
        global _http_session
        if _http_session and not _http_session.closed:
            await _http_session.close()
        await super().close()

    # ── SAV 스캔 루프: 30초마다 신규 유저 등록 ──
    # 가벼운 작업 (DB에 없는 ID만 Steam API 호출)
    @tasks.loop(seconds=30)
    async def sav_scan_loop(self):
        async with self.scan_lock:
            try:
                new_ids = await scan_sav_and_register()
                if new_ids:
                    print(f"[SAV 스캔 완료] 신규 등록: {len(new_ids)}명")
            except Exception as e:
                print(f"[sav_scan_loop 예외] {e}")

    @sav_scan_loop.error
    async def sav_scan_loop_error(self, error):
        print(f"[sav_scan_loop 예외] {error}")

    # ── 닉네임 추적 루프: 5분마다 전체 유저 닉변 체크 ──
    # 무거운 작업 (전체 유저 Steam API 호출)
    @tasks.loop(minutes=5)
    async def nickname_track_loop(self):
        async with self.track_lock:
            try:
                pending_notify = await track_all_nicknames()

                if not pending_notify:
                    return

                # 알림 채널 조회
                with db_connection(auto_commit=False) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT notify_id FROM channels")
                    notify_channels = [x[0] for x in cursor.fetchall() if x[0]]

                # 알림 전송
                for name_key, sid, history, player in pending_notify:
                    embed = create_status_embed(name_key, sid, history, "notify", player)

                    for ch_id in notify_channels:
                        try:
                            ch = self.get_channel(ch_id)
                            if not ch:
                                ch = await self.fetch_channel(ch_id)
                            if ch:
                                await ch.send(embed=embed)
                        except Exception as e:
                            print(f"[알림 오류] ch={ch_id} {e}")

            except Exception as e:
                print(f"[nickname_track_loop 예외] {e}")

    @nickname_track_loop.error
    async def nickname_track_loop_error(self, error):
        print(f"[nickname_track_loop 예외] {error}")


bot = MyBot()

# =========================
# 채널 체크
# =========================

async def check_admin_channel(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ 서버에서만 사용 가능합니다", ephemeral=True
        )
        return False

    try:
        with db_connection(auto_commit=False) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT admin_id FROM channels WHERE guild_id = ?",
                (str(interaction.guild_id),)
            )
            row = cursor.fetchone()
    except Exception as e:
        print(f"[check_admin_channel DB 오류] {e}")
        await interaction.response.send_message("❌ DB 오류가 발생했습니다", ephemeral=True)
        return False

    if not row or not row[0]:
        await interaction.response.send_message(
            "❌ 먼저 /채널설정 으로 관리 채널을 설정하세요", ephemeral=True
        )
        return False

    if interaction.channel_id != row[0]:
        await interaction.response.send_message(
            "❌ 관리 채널에서만 사용 가능합니다", ephemeral=True
        )
        return False

    return True


async def check_admin_permission(interaction: discord.Interaction) -> bool:
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ 관리자 권한이 필요합니다", ephemeral=True
        )
        return False
    return True

# =========================
# 유저 조회
# =========================

def find_user(cursor: sqlite3.Cursor, target: str) -> tuple | None:
    if target.isdigit():
        cursor.execute("""
            SELECT steam_id, current_name, name_key, is_monitored
            FROM users WHERE steam_id = ?
        """, (target,))
    else:
        cursor.execute("""
            SELECT steam_id, current_name, name_key, is_monitored
            FROM users WHERE name_key = ?
        """, (target,))
    return cursor.fetchone()

# =========================
# /추가
# =========================

@bot.tree.command(name="추가", description="알림 감시 활성화")
async def add_user(i: discord.Interaction, target: str, 별명: str = None):
    if not await check_admin_channel(i):
        return

    await i.response.defer()

    try:
        with db_connection() as conn:
            cursor = conn.cursor()

            # 별명 중복 체크
            if 별명:
                cursor.execute("""
                    SELECT steam_id, current_name, name_key
                    FROM users WHERE name_key = ?
                """, (별명.strip(),))
                dup = cursor.fetchone()
                if dup:
                    return await i.followup.send(
                        f"❌ 이미 사용중인 별명입니다\n\n"
                        f"등록 별명: {dup[2]}\n"
                        f"최근 닉네임: {dup[1]}\n"
                        f"SteamID: {dup[0]}"
                    )

            row = find_user(cursor, target)

            if not row:
                # DB에 없으면 SteamID로 직접 조회 후 등록
                if not target.isdigit():
                    return await i.followup.send("❌ 존재하지 않는 유저입니다")

                sid = target
                players = await get_steam_users_info([sid])
                player = players[0] if players else None

                if not player:
                    return await i.followup.send("❌ Steam 유저 조회 실패")

                current_name = await get_current_nickname(sid, player)
                if current_name is None:
                    return await i.followup.send("❌ 닉네임 조회 실패, 잠시 후 다시 시도해주세요")

                base_key = 별명.strip() if 별명 else f"user_{sid[-6:]}"
                name_key = unique_name_key(cursor, base_key)

                cursor.execute("""
                    INSERT INTO users (steam_id, name_key, current_name, is_monitored, updated_at)
                    VALUES (?, ?, ?, 1, ?)
                """, (sid, name_key, current_name, datetime.now().isoformat()))

                add_history_if_needed(cursor, sid, current_name)

                sid_out, current_name_out, name_key_out = sid, current_name, name_key

            else:
                sid_out, current_name_out, name_key_out, monitored_out = row
                player = None

                if monitored_out == 1:
                    return await i.followup.send(
                        f"❌ 이미 감시중인 SteamID입니다\n\n"
                        f"등록 별명: {name_key_out}\n"
                        f"최근 닉네임: {current_name_out}\n"
                        f"SteamID: {sid_out}"
                    )

                # 별명 변경이 있으면 반영
                new_name_key = 별명.strip() if 별명 else name_key_out

                cursor.execute("""
                    UPDATE users SET is_monitored = 1, name_key = ?
                    WHERE steam_id = ?
                """, (new_name_key, sid_out))

                name_key_out = new_name_key

            history = get_history_list(cursor, sid_out)
            sid_out = sid_out if 'sid_out' in dir() else sid

    except Exception as e:
        print(f"[/추가 오류] {e}")
        return await i.followup.send("❌ 처리 중 오류가 발생했습니다")

    # embed용 player 재조회 (기존 유저 경로)
    if player is None:
        players = await get_steam_users_info([sid_out])
        player = players[0] if players else None

    embed = create_status_embed(name_key_out, sid_out, history, "add", player)
    embed.add_field(name="감시 상태", value="🔔 알림 활성화", inline=False)
    await i.followup.send(embed=embed)

# =========================
# /삭제
# =========================

@bot.tree.command(name="삭제", description="알림 감시 해제")
async def delete_user(i: discord.Interaction, target: str):
    if not await check_admin_permission(i):
        return
    if not await check_admin_channel(i):
        return

    await i.response.defer()

    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            row = find_user(cursor, target)

            if not row:
                return await i.followup.send("❌ 유저 없음")

            sid, current_name, name_key, monitored = row

            if monitored == 0:
                return await i.followup.send("❌ 이미 감시 해제 상태")

            cursor.execute(
                "UPDATE users SET is_monitored = 0 WHERE steam_id = ?",
                (sid,)
            )

    except Exception as e:
        print(f"[/삭제 오류] {e}")
        return await i.followup.send("❌ 처리 중 오류가 발생했습니다")

    await i.followup.send(
        f"✅ 감시 해제 완료\n\n"
        f"등록 별명: {name_key}\n"
        f"최근 닉네임: {current_name}\n"
        f"SteamID: {sid}"
    )

# =========================
# /내역
# =========================

@bot.tree.command(name="내역", description="닉변 내역 확인")
async def user_history(i: discord.Interaction, target: str):
    if not await check_admin_channel(i):
        return

    await i.response.defer()

    try:
        with db_connection(auto_commit=False) as conn:
            cursor = conn.cursor()

            if target.isdigit():
                cursor.execute("""
                    SELECT name_key, steam_id, current_name, updated_at, is_monitored
                    FROM users WHERE steam_id = ?
                """, (target,))
            else:
                cursor.execute("""
                    SELECT name_key, steam_id, current_name, updated_at, is_monitored
                    FROM users WHERE name_key = ?
                """, (target,))

            row = cursor.fetchone()

            if not row:
                return await i.followup.send("❌ 유저 없음")

            name_key, sid, current_name, updated_at, monitored = row
            history = get_history_list(cursor, sid)

    except Exception as e:
        print(f"[/내역 오류] {e}")
        return await i.followup.send("❌ 처리 중 오류가 발생했습니다")

    players = await get_steam_users_info([sid])
    player = players[0] if players else None

    embed = create_status_embed(name_key, sid, history, "history", player)
    embed.add_field(name="감시 상태", value="🔔 감시중" if monitored else "🔇 미감시", inline=False)
    embed.add_field(name="최근 갱신", value=updated_at or "없음", inline=False)
    embed.add_field(name="닉변 횟수", value=str(max(0, len(history) - 1)), inline=False)

    await i.followup.send(embed=embed)

# =========================
# /현황
# =========================

@bot.tree.command(name="현황", description="현재 감시중인 유저 목록")
async def status_list(i: discord.Interaction):
    if not await check_admin_channel(i):
        return

    await i.response.defer()

    try:
        with db_connection(auto_commit=False) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name_key, steam_id, current_name
                FROM users WHERE is_monitored = 1
                ORDER BY name_key COLLATE NOCASE
            """)
            rows = cursor.fetchall()

    except Exception as e:
        print(f"[/현황 오류] {e}")
        return await i.followup.send("❌ 처리 중 오류가 발생했습니다")

    if not rows:
        return await i.followup.send("📭 현재 감시중인 유저 없음")

    messages = []
    current_msg = "📡 현재 감시중인 유저 목록\n```text\n별명 / 현재닉 / SteamID\n"

    for name, sid, current_name in rows:
        line = f"{name} / {current_name or '없음'} / {sid}\n"

        if len(current_msg + line + "```") > 1900:
            current_msg += "```"
            messages.append(current_msg)
            current_msg = "```text\n" + line
        else:
            current_msg += line

    current_msg += "```"
    messages.append(current_msg)

    for msg in messages:
        try:
            await i.followup.send(msg)
        except Exception as e:
            print(f"[현황 전송 오류] {e}")


# =========================
# /전체현황
# =========================

@bot.tree.command(name="전체현황", description="DB에 저장된 모든 유저 수 및 통계")
async def full_status(i: discord.Interaction):
    if not await check_admin_channel(i):
        return

    await i.response.defer()

    try:
        with db_connection(auto_commit=False) as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM users")
            total = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM users WHERE is_monitored = 1")
            monitored = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM nickname_history")
            history_total = cursor.fetchone()[0]

            cursor.execute("""
                SELECT u.name_key, u.steam_id, COUNT(nh.id) as cnt
                FROM users u
                LEFT JOIN nickname_history nh ON u.steam_id = nh.steam_id
                GROUP BY u.steam_id
                ORDER BY cnt DESC
                LIMIT 5
            """)
            top_changers = cursor.fetchall()

    except Exception as e:
        print(f"[/전체현황 오류] {e}")
        return await i.followup.send("❌ 처리 중 오류가 발생했습니다")

    embed = discord.Embed(title="📊 전체 유저 통계", color=discord.Color.blurple())
    embed.add_field(name="전체 추적 유저", value=f"{total}명", inline=True)
    embed.add_field(name="알림 활성 유저", value=f"{monitored}명", inline=True)
    embed.add_field(name="총 닉변 기록 수", value=f"{history_total}건", inline=True)

    if top_changers:
        top_text = "\n".join(
            f"{idx+1}. {name} ({sid[-6:]}) — {cnt}회"
            for idx, (name, sid, cnt) in enumerate(top_changers)
        )
        embed.add_field(name="닉변 TOP 5", value=top_text, inline=False)

    await i.followup.send(embed=embed)


# =========================
# /채널설정
# =========================

_ROLE_TO_COLUMN = {
    "admin":  "admin_id",
    "notify": "notify_id"
}

@bot.tree.command(name="채널설정", description="관리/알림 채널 설정")
@app_commands.choices(
    역할=[
        app_commands.Choice(name="관리", value="admin"),
        app_commands.Choice(name="알림", value="notify")
    ]
)
async def set_channel(i: discord.Interaction, 역할: str):
    if not i.user.guild_permissions.administrator:
        return await i.response.send_message("❌ 관리자 권한 필요", ephemeral=True)

    await i.response.defer()

    col = _ROLE_TO_COLUMN.get(역할)
    if col is None:
        return await i.followup.send("❌ 올바르지 않은 역할입니다")

    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                INSERT INTO channels (guild_id, {col})
                VALUES (?, ?)
                ON CONFLICT(guild_id)
                DO UPDATE SET {col}=excluded.{col}
            """, (str(i.guild_id), i.channel_id))

    except Exception as e:
        print(f"[/채널설정 오류] {e}")
        return await i.followup.send("❌ 처리 중 오류가 발생했습니다")

    await i.followup.send(f"✅ {역할} 채널 설정 완료")

# =========================
# 실행
# =========================

# =========================
# /동기화
# =========================

@bot.tree.command(name="동기화", description=".sav 파일을 업로드해 Steam ID를 DB에 일괄 등록")
async def sync_sav(i: discord.Interaction):
    if not await check_admin_permission(i):
        return
    if not await check_admin_channel(i):
        return

    # 파일 첨부 안내 메시지 전송
    await i.response.send_message(
        "📂 **seenplayers.sav 파일을 첨부해서 보내주세요.**\n"
        "여러 파일을 한 번에 올려도 됩니다. (예: Uuvana1~3, UuvanaHARDCORE)\n"
        "⏳ 60초 안에 업로드해주세요.",
        ephemeral=False
    )

    # 파일 업로드 대기
    def check(m: discord.Message):
        return (
            m.channel.id == i.channel_id
            and m.author.id == i.user.id
            and len(m.attachments) > 0
            and any(a.filename.endswith(".sav") for a in m.attachments)
        )

    try:
        msg: discord.Message = await bot.wait_for("message", check=check, timeout=60.0)
    except asyncio.TimeoutError:
        return await i.followup.send("⏰ 시간 초과. `/동기화`를 다시 실행해주세요.", ephemeral=True)

    sav_attachments = [a for a in msg.attachments if a.filename.endswith(".sav")]

    await i.followup.send(
        f"⚙️ {len(sav_attachments)}개 파일 처리 중... (Steam ID 추출 → DB 등록)\n"
        f"유저 수에 따라 시간이 걸릴 수 있어요."
    )

    # 파일별 Steam ID 수집
    session = await get_http_session()
    found_ids: set[str] = set()

    for attachment in sav_attachments:
        try:
            async with session.get(attachment.url, timeout=aiohttp.ClientTimeout(total=30)) as res:
                if res.status == 200:
                    file_bytes = await res.read()
                    parsed = parse_sav_file(file_bytes)
                    for item in parsed:
                        found_ids.add(item["steam_id"])
                    print(f"[동기화] {attachment.filename}: {len(parsed)}개 ID 추출")
        except Exception as e:
            print(f"[동기화 파일 오류] {attachment.filename}: {e}")

    if not found_ids:
        return await i.followup.send("❌ .sav 파일에서 Steam ID를 찾을 수 없었어요.")

    # DB에 없는 신규 ID 필터링
    with db_connection(auto_commit=False) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT steam_id FROM users")
        existing_ids = {row[0] for row in cursor.fetchall()}

    truly_new = list(found_ids - existing_ids)
    already_count = len(found_ids) - len(truly_new)

    if not truly_new:
        return await i.followup.send(
            f"✅ 동기화 완료!\n"
            f"📊 .sav 총 Steam ID: {len(found_ids):,}개\n"
            f"🔁 이미 DB에 있는 유저: {already_count:,}명\n"
            f"🆕 신규 등록: 0명 (모두 이미 등록됨)"
        )

    await i.followup.send(
        f"🔍 신규 유저 {len(truly_new):,}명 발견! Steam 닉네임 조회 중...\n"
        f"(100명당 약 1~2초 소요, 총 예상 시간: 약 {max(1, len(truly_new) // 100 * 2)}초)"
    )

    # Steam API로 닉네임 일괄 조회
    players = await get_steam_users_info(truly_new)
    p_dict = {p["steamid"]: p for p in players}

    # DB 등록
    registered = 0
    failed_nick = 0

    with db_connection() as conn:
        cursor = conn.cursor()
        for sid in truly_new:
            try:
                player = p_dict.get(sid)
                current_name = await get_current_nickname(sid, player)

                if current_name is None:
                    current_name = ""
                    failed_nick += 1

                base_key = f"user_{sid[-6:]}"
                name_key = unique_name_key(cursor, base_key)

                cursor.execute("""
                    INSERT OR IGNORE INTO users
                        (steam_id, name_key, current_name, is_monitored, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                """, (sid, name_key, current_name, datetime.now().isoformat()))

                if current_name:
                    add_history_if_needed(cursor, sid, current_name)

                registered += 1

            except Exception as e:
                print(f"[동기화 등록 오류] {sid}: {e}")

    # 최종 결과 리포트
    embed = discord.Embed(
        title="✅ .sav 동기화 완료",
        color=discord.Color.green()
    )
    embed.add_field(name="📂 처리한 파일 수", value=f"{len(sav_attachments)}개", inline=True)
    embed.add_field(name="👥 .sav 총 Steam ID", value=f"{len(found_ids):,}개", inline=True)
    embed.add_field(name="🔁 이미 등록된 유저", value=f"{already_count:,}명", inline=True)
    embed.add_field(name="🆕 신규 등록 완료", value=f"{registered:,}명", inline=True)
    embed.add_field(name="⚠️ 닉네임 조회 실패", value=f"{failed_nick:,}명 (ID는 등록됨)", inline=True)
    embed.set_footer(text="신규 등록된 유저는 is_monitored=0 (알림 비활성) 상태입니다. /추가로 감시 활성화하세요.")

    await i.followup.send(embed=embed)


# =========================
# /도움말
# =========================

@bot.tree.command(name="도움말", description="봇 명령어 안내")
async def help_command(i: discord.Interaction):
    embed = discord.Embed(
        title="📖 롱빈터 스팀 트래커 봇 도움말",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="👤 유저 관리",
        value=(
            "`/추가 [SteamID] (별명)` — 유저를 감시 목록에 등록, 닉변 알림 활성화\n"
            "`/삭제 [SteamID 또는 별명]` — 감시 해제 (DB에서 삭제되진 않음)\n"
            "`/내역 [SteamID 또는 별명]` — 닉네임 변경 내역 확인"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 현황 확인",
        value=(
            "`/현황` — 현재 알림 감시 중인 유저 목록\n"
            "`/전체현황` — DB에 저장된 전체 유저 수 및 통계 (닉변 TOP 5 등)"
        ),
        inline=False
    )

    embed.add_field(
        name="🔄 동기화",
        value=(
            "`/동기화` — .sav 파일을 업로드하면 Steam ID를 DB에 일괄 등록\n"
            "　　　　　등록된 유저는 알림 비활성(is_monitored=0) 상태\n"
            "　　　　　알림을 받으려면 `/추가`로 감시 활성화 필요"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ 설정",
        value=(
            "`/채널설정 [관리/알림]` — 명령어를 사용할 관리 채널 / 닉변 알림을 받을 채널 설정\n"
            "　　　　　　　관리자 권한 필요"
        ),
        inline=False
    )

    embed.add_field(
        name="ℹ️ 참고",
        value=(
            "• `/추가`로 등록된 유저만 닉변 알림이 옵니다\n"
            "• `/동기화`로 등록된 유저는 닉변 기록은 되지만 알림은 오지 않습니다\n"
            "• 닉네임은 5분마다 자동으로 갱신됩니다"
        ),
        inline=False
    )

    await i.response.send_message(embed=embed)


# =========================
# /일괄감시
# =========================

_MONITORED_IDS = [
    '76561199181578912', '76561198740509806', '76561199330057128', '76561199186211430',
    '76561199853543628', '76561198755562543', '76561199321806704', '76561198759452739',
    '76561198728034961', '76561198417186711', '76561199787976761', '76561198202789714',
    '76561199060383719', '76561199536361170', '76561198409905276', '76561199697265605',
    '76561199375063854', '76561198723840522', '76561198762817202', '76561198736227455',
    '76561198789169680', '76561199478666526', '76561199227711388', '76561198734632465',
    '76561198728060007', '76561198743190821', '76561199635197728', '76561199509854920',
    '76561198744378550', '76561199798585655', '76561199813609408', '76561198732890917',
    '76561198739106627', '76561199863075794', '76561199842133343', '76561198729097198',
    '76561198726000436', '76561199837889892', '76561198723778596', '76561198994520187',
    '76561198739618053', '76561198436064537', '76561198736695342', '76561198754256790',
    '76561198122468894', '76561198080567276', '76561198158887106', '76561199685602861',
    '76561198732766237', '76561198781725345', '76561198752960783', '76561199661929717',
    '76561199720331326', '76561199672756293', '76561198800587075', '76561199822446724',
    '76561199720827840', '76561198741294065', '76561198723288425', '76561198759160362',
    '76561198735717668', '76561198743558406', '76561198159398388', '76561198125596817',
    '76561199242850311', '76561199800283943', '76561198163404947', '76561198723437729',
    '76561198133784033', '76561198755793283', '76561199666151778', '76561199804946165',
    '76561198256967816', '76561198725548482', '76561198753887930', '76561199737154565',
    '76561198780238129', '76561199789851883', '76561198729442558', '76561198794916193',
    '76561199553226980', '76561198722687362', '76561198762409988', '76561198715664356',
    '76561198392823357', '76561198733939555', '76561199730954273', '76561198753369220',
    '76561198837379345', '76561198273681878', '76561198790297530', '76561198714515838',
    '76561199886736237', '76561199517720657', '76561199848120167', '76561199642459483',
    '76561198754249926', '76561198021289636', '76561199235917996', '76561199557057775',
    '76561198786875379', '76561199706917303', '76561198710240714', '76561199239798388',
    '76561198790555960', '76561198866998382', '76561198712722749', '76561198767724006',
    '76561199834723349', '76561199636670761', '76561198732820479', '76561198792685415',
    '76561199860327467', '76561198709036550', '76561199569887820', '76561198714496967',
    '76561198716183660', '76561199848865849', '76561199148700328', '76561199386785483',
    '76561198750304238', '76561199726869973', '76561198811298397', '76561199257135435',
    '76561199881107761', '76561199889894640', '76561198757203859',
]


@bot.tree.command(name="일괄감시", description="기존 127명을 감시 대상으로 일괄 활성화 (1회용)")
async def bulk_monitor(i: discord.Interaction):
    if not await check_admin_permission(i):
        return

    await i.response.defer()

    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            updated = 0
            not_found = 0

            for sid in _MONITORED_IDS:
                cursor.execute(
                    "UPDATE users SET is_monitored = 1 WHERE steam_id = ?", (sid,)
                )
                if cursor.rowcount > 0:
                    updated += 1
                else:
                    not_found += 1

    except Exception as e:
        print(f"[/일괄감시 오류] {e}")
        return await i.followup.send("❌ 처리 중 오류가 발생했습니다")

    embed = discord.Embed(title="✅ 일괄 감시 활성화 완료", color=discord.Color.green())
    embed.add_field(name="🔔 감시 활성화", value=f"{updated}명", inline=True)
    embed.add_field(name="⚠️ DB에 없는 유저", value=f"{not_found}명", inline=True)
    embed.set_footer(text="이 명령어는 1회용입니다. 완료 후 코드에서 제거하세요.")
    await i.followup.send(embed=embed)



# =========================
# HTTP API 서버 (로컬 클라이언트용)
# =========================

api = FastAPI()

DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
ALLOWED_GUILD_IDS     = set(os.getenv("ALLOWED_GUILD_IDS", "").split(","))
REDIRECT_URI          = "http://localhost:7777/callback"

# 세션 토큰 저장소 {session_token: discord_user_id}
_sessions: dict = {}


def _make_session_token() -> str:
    import secrets
    return secrets.token_urlsafe(32)


# ── OAuth2 로그인 URL 반환 ──
@api.get("/auth/login")
async def auth_login():
    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
    })
    return JSONResponse(content={"url": f"https://discord.com/oauth2/authorize?{params}"})


# ── OAuth2 콜백 (로컬 서버가 받아서 봇 서버로 전달) ──
@api.get("/auth/callback")
async def auth_callback(code: str):
    session = await get_http_session()

    # 액세스 토큰 교환
    try:
        async with session.post(
            "https://discord.com/api/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as res:
            token_data = await res.json()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    access_token = token_data.get("access_token")
    if not access_token:
        return JSONResponse(status_code=401, content={"error": "토큰 발급 실패"})

    # 유저 정보 조회
    try:
        async with session.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as res:
            user_data = await res.json()

        async with session.get(
            "https://discord.com/api/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as res:
            guilds_data = await res.json()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    # 허용 서버 멤버 확인
    user_guild_ids = {str(g["id"]) for g in guilds_data}
    if not (user_guild_ids & ALLOWED_GUILD_IDS):
        return JSONResponse(status_code=403, content={"error": "허용된 서버 멤버가 아닙니다"})

    # 세션 발급
    session_token = _make_session_token()
    _sessions[session_token] = user_data.get("id")
    username = user_data.get("username", "unknown")
    print(f"[인증 성공] {username} ({user_data.get('id')})")

    return JSONResponse(content={
        "session_token": session_token,
        "username": username,
    })


# ── 세션 검증 ──
def _verify_session(session_token: str) -> bool:
    return session_token in _sessions


# ── 세션 검증 엔드포인트 ──
@api.get("/auth/verify")
async def auth_verify(session_token: str = ""):
    if _verify_session(session_token):
        return JSONResponse(content={"valid": True})
    return JSONResponse(status_code=401, content={"valid": False})


# ── 유저 조회 ──
@api.get("/user")
async def get_user(steam_id: str, session_token: str = ""):
    if not _verify_session(session_token):
        return JSONResponse(status_code=401, content={"error": "인증이 필요합니다"})

    try:
        # API는 읽기 전용 별도 연결 사용 (DB 락 방지)
        conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT steam_id, name_key, current_name, is_monitored
            FROM users WHERE steam_id = ?
        """, (steam_id,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return JSONResponse(content={"found": False, "steam_id": steam_id})

        cursor.execute("""
            SELECT nickname FROM nickname_history
            WHERE steam_id = ?
            ORDER BY id ASC
        """, (steam_id,))
        history = [r[0] for r in cursor.fetchall()]
        conn.close()

        return JSONResponse(content={
            "found": True,
            "steam_id": row["steam_id"],
            "name_key": row["name_key"],
            "current_name": row["current_name"],
            "is_monitored": bool(row["is_monitored"]),
            "history": history
        })

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


async def run_api():
    config = uvicorn.Config(
        api,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        log_level="info",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # Railway 시그널 충돌 방지
    await server.serve()


async def main():
    await asyncio.gather(
        run_api(),
        bot.start(TOKEN),
    )

if __name__ == "__main__":
    asyncio.run(main())
