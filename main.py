# 수정된 전체 코드 (핵심 통합 버전)

import discord
from discord import app_commands
from discord.ext import commands, tasks

import requests
import sqlite3
import os
import asyncio
import re
import xml.etree.ElementTree as ET
import time

from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# =========================
# 설정
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")
STEAM_API_KEY = os.getenv("STEAM_API_KEY")
DB_PATH = os.getenv("DB_PATH", "bot_data.db")

SAVE_DIR = r"C:\Users\peal\AppData\Local\Longvinter\Saved\SaveGames"

WATCH_FILES = [
    "[KR]Uuvana1-seenplayers.sav",
    "[KR]Uuvana2-seenplayers.sav",
    "[KR]Uuvana3-seenplayers.sav",
    "[KR]UuvanaHARDCORE-seenplayers.sav"
]

# =========================
# DB
# =========================

def get_db():

    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    return conn


def init_db():

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                steam_id TEXT PRIMARY KEY,
                name_key TEXT UNIQUE,
                current_name TEXT,
                eos_id TEXT,
                is_monitored INTEGER DEFAULT 0,
                updated_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                guild_id TEXT PRIMARY KEY,
                admin_id INTEGER,
                notify_id INTEGER
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nickname_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                steam_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                changed_at TEXT NOT NULL
            )
        """)

        conn.commit()

    finally:
        conn.close()


init_db()

# =========================
# Steam API
# =========================

async def get_steam_users_info(steam_ids):

    if not steam_ids:
        return []

    ids_str = ",".join(steam_ids)

    url = (
        "https://api.steampowered.com/"
        "ISteamUser/GetPlayerSummaries/v0002/"
        f"?key={STEAM_API_KEY}&steamids={ids_str}"
    )

    try:

        res = await asyncio.to_thread(
            requests.get,
            url,
            timeout=10
        )

        if res.status_code == 200:

            return res.json().get(
                "response",
                {}
            ).get(
                "players",
                []
            )

    except Exception as e:
        print(f"[Steam API 오류] {e}")

    return []


async def get_nickname_from_xml(steam_id):

    url = f"https://steamcommunity.com/profiles/{steam_id}/?xml=1"

    try:

        res = await asyncio.to_thread(
            requests.get,
            url,
            timeout=8
        )

        if res.status_code == 200:

            root = ET.fromstring(res.content)

            node = root.find("steamID")

            if node is not None and node.text:
                return node.text.strip()

    except Exception:
        pass

    return None

# =========================
# SAV 파싱
# =========================

def parse_sav_file(file_bytes):

    results = []

    try:

        text_data = file_bytes.decode(
            "latin-1",
            errors="ignore"
        )

        matches = re.findall(
            r"([0-9a-f]{32}).{0,300}?(7656119\d{10})",
            text_data,
            re.IGNORECASE | re.DOTALL
        )

        unique = set()

        for eos_id, steam_id in matches:

            if steam_id in unique:
                continue

            unique.add(steam_id)

            results.append({
                "steam_id": steam_id,
                "eos_id": eos_id.lower()
            })

    except Exception as e:
        print(f"[SAV 파싱 오류] {e}")

    return results

# =========================
# 히스토리
# =========================

def get_history_list(cursor, steam_id):

    cursor.execute("""
        SELECT nickname
        FROM nickname_history
        WHERE steam_id = ?
        ORDER BY id ASC
    """, (steam_id,))

    return [row[0] for row in cursor.fetchall()]


def add_history_if_needed(cursor, steam_id, nickname):

    cursor.execute("""
        SELECT nickname
        FROM nickname_history
        WHERE steam_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (steam_id,))

    row = cursor.fetchone()

    if row and row[0] == nickname:
        return False

    cursor.execute("""
        INSERT INTO nickname_history (
            steam_id,
            nickname,
            changed_at
        )
        VALUES (?, ?, ?)
    """, (
        steam_id,
        nickname,
        datetime.now().isoformat()
    ))

    return True

# =========================
# EMBED
# =========================

def create_status_embed(
    display_name,
    sid,
    history,
    mode="notify",
    player=None
):

    colors = {
        "add": discord.Color.green(),
        "notify": discord.Color.gold(),
        "history": discord.Color.blue(),
        "exist": discord.Color.red()
    }

    titles = {
        "add": "✨ 감시 등록 완료",
        "notify": "🔔 닉네임 변경 감지",
        "history": "📋 닉네임 변경 내역",
        "exist": "❌ 이미 등록됨"
    }

    embed = discord.Embed(
        title=titles.get(mode, "알림"),
        color=colors.get(mode)
    )

    if player:
        embed.set_thumbnail(url=player.get("avatarfull"))

    embed.add_field(
        name="등록 별명",
        value=display_name,
        inline=False
    )

    embed.add_field(
        name="현재 닉네임",
        value=history[-1] if history else "없음",
        inline=False
    )

    history_text = " → ".join(history)

    if len(history_text) > 1000:
        history_text = history_text[-1000:]

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
# SAV WATCHER
# =========================

class SavWatcher(FileSystemEventHandler):

    def __init__(self, bot):

        self.bot = bot
        self.last_run = {}

    def on_modified(self, event):

        filename = os.path.basename(event.src_path)

        if filename not in WATCH_FILES:
            return

        now = time.time()

        last = self.last_run.get(filename, 0)

        if now - last < 3:
            return

        self.last_run[filename] = now

        asyncio.run_coroutine_threadsafe(
            self.process_file(event.src_path),
            self.bot.loop
        )

    async def process_file(self, path):

        try:

            with open(path, "rb") as f:
                file_bytes = f.read()

            parsed = await asyncio.to_thread(
                parse_sav_file,
                file_bytes
            )

            if not parsed:
                return

            steam_ids = [
                p["steam_id"]
                for p in parsed
            ]

            players = await get_steam_users_info(steam_ids)

            p_dict = {
                p["steamid"]: p
                for p in players
            }

            conn = get_db()

            try:

                cursor = conn.cursor()

                cursor.execute("""
                    SELECT notify_id
                    FROM channels
                """)

                notify_channels = [
                    x[0]
                    for x in cursor.fetchall()
                    if x[0]
                ]

                for item in parsed:

                    sid = item["steam_id"]
                    eos = item["eos_id"]

                    player = p_dict.get(sid)

                    curr = (
                        player.get("personaname")
                        if player and player.get("personaname")
                        else None
                    )

                    if not curr:
                        continue

                    curr = curr.strip()

                    cursor.execute("""
                        SELECT
                            current_name,
                            is_monitored,
                            name_key
                        FROM users
                        WHERE steam_id = ?
                    """, (sid,))

                    row = cursor.fetchone()

                    if not row:

                        save_name = f"user_{sid[-6:]}"

                        cursor.execute("""
                            INSERT INTO users (
                                steam_id,
                                name_key,
                                current_name,
                                eos_id,
                                is_monitored,
                                updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (
                            sid,
                            save_name,
                            curr,
                            eos,
                            0,
                            datetime.now().isoformat()
                        ))

                        add_history_if_needed(
                            cursor,
                            sid,
                            curr
                        )

                        print(f"[자동 등록] {curr} ({sid})")

                    else:

                        old_name, monitored, name_key = row

                        if old_name != curr:

                            cursor.execute("""
                                UPDATE users
                                SET
                                    current_name = ?,
                                    eos_id = ?,
                                    updated_at = ?
                                WHERE steam_id = ?
                            """, (
                                curr,
                                eos,
                                datetime.now().isoformat(),
                                sid
                            ))

                            added = add_history_if_needed(
                                cursor,
                                sid,
                                curr
                            )

                            print(
                                f"[닉변 감지] "
                                f"{old_name} → {curr}"
                            )

                            if monitored == 1 and added:

                                history = get_history_list(
                                    cursor,
                                    sid
                                )

                                embed = create_status_embed(
                                    name_key,
                                    sid,
                                    history,
                                    "notify",
                                    player
                                )

                                for ch_id in notify_channels:

                                    try:

                                        ch = self.bot.get_channel(ch_id)

                                        if not ch:
                                            ch = await self.bot.fetch_channel(ch_id)

                                        if ch:
                                            await ch.send(embed=embed)

                                    except Exception as e:
                                        print(f"[알림 오류] {e}")

                conn.commit()

            finally:
                conn.close()

        except Exception as e:
            print(f"[SAV 감시 오류] {e}")

# =========================
# BOT
# =========================

class MyBot(commands.Bot):

    def __init__(self):

        super().__init__(
            command_prefix="!",
            intents=discord.Intents.all()
        )

    async def setup_hook(self):

        synced = await self.tree.sync()

        print(f"슬래시 명령어 동기화 완료: {len(synced)}개")

    async def on_ready(self):

        print(f"Logged in as {self.user}")

        if not hasattr(self, "observer"):

            event_handler = SavWatcher(self)

            self.observer = Observer()

            self.observer.schedule(
                event_handler,
                SAVE_DIR,
                recursive=False
            )

            self.observer.start()

            print("[SAV 실시간 감시 시작]")

        if not self.check_steam_nicknames.is_running():
            self.check_steam_nicknames.start()

    @tasks.loop(minutes=10)
    async def check_steam_nicknames(self):

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    steam_id,
                    current_name
                FROM users
            """)

            rows = cursor.fetchall()

        finally:
            conn.close()

        if not rows:
            return

        ids = [r[0] for r in rows]

        players = await get_steam_users_info(ids)

        p_dict = {
            p["steamid"]: p
            for p in players
        }

        conn = get_db()

        try:

            cursor = conn.cursor()

            for sid, old_name in rows:

                player = p_dict.get(sid)

                if not player:
                    continue

                curr = player.get("personaname")

                if not curr:
                    continue

                curr = curr.strip()

                if curr == old_name:
                    continue

                cursor.execute("""
                    UPDATE users
                    SET current_name = ?,
                        updated_at = ?
                    WHERE steam_id = ?
                """, (
                    curr,
                    datetime.now().isoformat(),
                    sid
                ))

                add_history_if_needed(
                    cursor,
                    sid,
                    curr
                )

                print(f"[주기 닉변] {old_name} → {curr}")

            conn.commit()

        finally:
            conn.close()

bot = MyBot()

# =========================
# 채널 체크
# =========================

async def check_admin_channel(interaction):

    if interaction.guild is None:

        await interaction.response.send_message(
            "❌ 서버에서만 사용 가능합니다",
            ephemeral=True
        )

        return False

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT admin_id
            FROM channels
            WHERE guild_id = ?
        """, (str(interaction.guild_id),))

        row = cursor.fetchone()

    finally:
        conn.close()

    if not row or not row[0]:

        await interaction.response.send_message(
            "❌ 먼저 /채널설정 으로 관리 채널을 설정하세요",
            ephemeral=True
        )

        return False

    if interaction.channel_id != row[0]:

        await interaction.response.send_message(
            "❌ 관리 채널에서만 사용 가능합니다",
            ephemeral=True
        )

        return False

    return True

# =========================
# /추가
# =========================

@bot.tree.command(name="추가", description="알림 감시 활성화")
async def add_user(
    i: discord.Interaction,
    target: str,
    별명: str = None
):

    if not await check_admin_channel(i):
        return

    await i.response.defer()

    conn = get_db()

    try:

        cursor = conn.cursor()

        if 별명:

            cursor.execute("""
                SELECT steam_id
                FROM users
                WHERE name_key = ?
            """, (별명,))

            dup_name = cursor.fetchone()

if dup_name:

    return await i.followup.send(
        f"❌ 이미 사용중인 별명입니다\n\n"
        f"등록 별명: {별명}\n"
        f"SteamID: {dup_name[0]}"
    )

        cursor.execute("""
            SELECT
                name_key,
                current_name,
                steam_id,
                is_monitored
            FROM users
            WHERE steam_id = ?
            OR name_key = ?
        """, (
            target,
            target
        ))

        row = cursor.fetchone()

        if not row:

            return await i.followup.send(
                "❌ SAV에서 아직 발견되지 않은 유저입니다"
            )

        name_key, current_name, sid, monitored = row

        if monitored == 1:

            return await i.followup.send(
                f"❌ 이미 감시중인 SteamID입니다

"
                f"등록 별명: {name_key}
"
                f"최근 닉네임: {current_name}
"
                f"SteamID: {sid}"
            )

        new_name = (
            별명.strip()
            if 별명
            else name_key or f"user_{sid[-6:]}"
        )

        cursor.execute("""
            UPDATE users
            SET
                is_monitored = 1,
                name_key = ?
            WHERE steam_id = ?
        """, (
            new_name,
            sid
        ))

        conn.commit()

        history = get_history_list(cursor, sid)

    finally:
        conn.close()

    players = await get_steam_users_info([sid])

    player = players[0] if players else None

    embed = create_status_embed(
        new_name,
        sid,
        history,
        "add",
        player
    )

    embed.add_field(
        name="감시 상태",
        value="🔔 알림 활성화",
        inline=False
    )

    await i.followup.send(embed=embed)

# =========================
# /삭제
# =========================

@bot.tree.command(name="삭제", description="알림 감시 해제")
async def delete_user(
    i: discord.Interaction,
    target: str
):

    if not await check_admin_channel(i):
        return

    await i.response.defer()

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                steam_id,
                current_name,
                name_key,
                is_monitored
            FROM users
            WHERE steam_id = ?
            OR name_key = ?
        """, (
            target,
            target
        ))

        row = cursor.fetchone()

        if not row:

            return await i.followup.send(
                "❌ 유저 없음"
            )

        sid, current_name, name_key, monitored = row

        if monitored == 0:

            return await i.followup.send(
                "❌ 이미 감시 해제 상태"
            )

        cursor.execute("""
            UPDATE users
            SET is_monitored = 0
            WHERE steam_id = ?
        """, (sid,))

        conn.commit()

    finally:
        conn.close()

    await i.followup.send(
        f"✅ 감시 해제 완료

"
        f"등록 별명: {name_key}
"
        f"최근 닉네임: {current_name}
"
        f"SteamID: {sid}"
    )

# =========================
# /내역
# =========================

@bot.tree.command(
    name="내역",
    description="닉변 내역 확인"
)
async def user_history(
    i: discord.Interaction,
    target: str
):

    if not await check_admin_channel(i):
        return

    await i.response.defer()

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                name_key,
                steam_id,
                current_name,
                updated_at,
                is_monitored
            FROM users
            WHERE steam_id = ?
            OR name_key = ?
        """, (
            target,
            target
        ))

        row = cursor.fetchone()

        if not row:

            return await i.followup.send(
                "❌ 유저 없음"
            )

        name_key, sid, current_name, updated_at, monitored = row

        history = get_history_list(cursor, sid)

    finally:
        conn.close()

    players = await get_steam_users_info([sid])

    player = players[0] if players else None

    embed = create_status_embed(
        name_key,
        sid,
        history,
        "history",
        player
    )

    embed.add_field(
        name="감시 상태",
        value="🔔 감시중" if monitored else "🔇 미감시",
        inline=False
    )

    embed.add_field(
        name="최근 갱신",
        value=updated_at or "없음",
        inline=False
    )

    embed.add_field(
        name="닉변 횟수",
        value=str(max(0, len(history)-1)),
        inline=False
    )

    await i.followup.send(embed=embed)

# =========================
# /현황
# =========================

@bot.tree.command(
    name="현황",
    description="현재 감시중인 유저 목록"
)
async def status_list(i: discord.Interaction):

    if not await check_admin_channel(i):
        return

    await i.response.defer()

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                name_key,
                steam_id,
                current_name
            FROM users
            WHERE is_monitored = 1
            ORDER BY updated_at DESC
        """)

        rows = cursor.fetchall()

    finally:
        conn.close()

    if not rows:

        return await i.followup.send(
            "📭 현재 감시중인 유저 없음"
        )

    messages = []

    current_msg = (
        "📡 현재 감시중인 유저 목록
"
        "```text
"
        "별명 / 현재닉 / SteamID
"
    )

    for name, sid, current_name in rows:

        line = f"{name} / {current_name} / {sid}
"

        if len(current_msg + line + "```") > 1900:

            current_msg += "```"
            messages.append(current_msg)

            current_msg = "```text
"
            current_msg += line

        else:
            current_msg += line

    current_msg += "```"
    messages.append(current_msg)

    for idx, msg in enumerate(messages):

        if idx == 0:
            await i.followup.send(msg)
        else:
            await i.channel.send(msg)

# =========================
# /채널설정
# =========================

@bot.tree.command(
    name="채널설정",
    description="관리/알림 채널 설정"
)
@app_commands.choices(
    역할=[
        app_commands.Choice(name="관리", value="admin"),
        app_commands.Choice(name="알림", value="notify")
    ]
)
async def set_channel(
    i: discord.Interaction,
    역할: str
):

    if not i.user.guild_permissions.administrator:

        return await i.response.send_message(
            "❌ 관리자 권한 필요",
            ephemeral=True
        )

    await i.response.defer()

    col = (
        "admin_id"
        if 역할 == "admin"
        else "notify_id"
    )

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute(f"""
            INSERT INTO channels (
                guild_id,
                {col}
            )
            VALUES (?, ?)

            ON CONFLICT(guild_id)
            DO UPDATE SET
                {col}=excluded.{col}
        """, (
            str(i.guild_id),
            i.channel_id
        ))

        conn.commit()

    finally:
        conn.close()

    await i.followup.send(
        f"✅ {역할} 채널 설정 완료"
    )

# =========================
# 초기 전체 SAV 스캔
# =========================

async def initial_scan():

    print("[초기 SAV 전체 스캔 시작]")

    watcher = SavWatcher(bot)

    for filename in WATCH_FILES:

        path = os.path.join(SAVE_DIR, filename)

        if os.path.exists(path):

            await watcher.process_file(path)

    print("[초기 SAV 전체 스캔 완료]")

# =========================
# 실행
# =========================

if __name__ == "__main__":

    async def runner():
        await initial_scan()

    bot.loop.create_task(runner())

    bot.run(TOKEN)
````
