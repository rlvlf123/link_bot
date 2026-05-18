# =========================
# Longvinter Steam Tracker Bot
# =========================

import discord
from discord import app_commands
from discord.ext import commands, tasks

import requests
import sqlite3
import os
import asyncio
import re
import xml.etree.ElementTree as ET

from datetime import datetime

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


def ensure_column(cursor, table, column, column_type):

    cursor.execute(f"PRAGMA table_info({table})")

    columns = [x[1] for x in cursor.fetchall()]

    if column not in columns:

        cursor.execute(
            f"ALTER TABLE {table} "
            f"ADD COLUMN {column} {column_type}"
        )


def init_db():

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                steam_id TEXT PRIMARY KEY,
                name_key TEXT UNIQUE
            )
        """)

        ensure_column(cursor, "users", "current_name", "TEXT")
        ensure_column(cursor, "users", "eos_id", "TEXT")
        ensure_column(cursor, "users", "is_monitored", "INTEGER DEFAULT 0")
        ensure_column(cursor, "users", "updated_at", "TEXT")

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
# UTIL
# =========================

def chunked(lst, size):

    for i in range(0, len(lst), size):
        yield lst[i:i + size]


# =========================
# Steam API
# =========================

async def get_steam_users_info(steam_ids):

    if not steam_ids:
        return []

    all_players = []

    for chunk in chunked(steam_ids, 100):

        ids_str = ",".join(chunk)

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

                players = (
                    res.json()
                    .get("response", {})
                    .get("players", [])
                )

                all_players.extend(players)

        except Exception as e:
            print(f"[Steam API 오류] {e}")

    return all_players


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


async def get_current_nickname(sid, player=None):

    curr = None

    if player:
        curr = player.get("personaname")

    if not curr:
        curr = await get_nickname_from_xml(sid)

    if not curr:
        curr = f"Unknown_{sid[-4:]}"

    return curr.strip()

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

        steam_ids = set(
            re.findall(r"7656119\d{10}", text_data)
        )

        for sid in steam_ids:

            results.append({
                "steam_id": sid
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

    if player and player.get("avatarfull"):
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
# SAV 자동 스캔
# =========================

async def scan_all_sav_files():

    print("[SAV 전체 스캔 시작]")

    for filename in WATCH_FILES:

        path = os.path.join(SAVE_DIR, filename)

        if not os.path.exists(path):
            continue

        try:

            with open(path, "rb") as f:
                file_bytes = f.read()

            parsed = parse_sav_file(file_bytes)

            if not parsed:
                continue

            steam_ids = [
                x["steam_id"]
                for x in parsed
            ]

            players = await get_steam_users_info(steam_ids)

            p_dict = {
                p["steamid"]: p
                for p in players
            }

            conn = get_db()

            try:

                cursor = conn.cursor()

                for item in parsed:

                    sid = item["steam_id"]

                    player = p_dict.get(sid)

                    curr = await get_current_nickname(
                        sid,
                        player
                    )

                    cursor.execute("""
                        SELECT current_name
                        FROM users
                        WHERE steam_id = ?
                    """, (sid,))

                    row = cursor.fetchone()

                    if not row:

                        save_name = f"user_{sid[-6:]}"

                        count = 1
                        temp_name = save_name

                        while True:

                            cursor.execute("""
                                SELECT 1
                                FROM users
                                WHERE name_key = ?
                            """, (temp_name,))

                            if not cursor.fetchone():
                                break

                            temp_name = f"{save_name}_{count}"
                            count += 1

                        save_name = temp_name

                        cursor.execute("""
                            INSERT INTO users (
                                steam_id,
                                name_key,
                                current_name,
                                is_monitored,
                                updated_at
                            )
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            sid,
                            save_name,
                            curr,
                            0,
                            datetime.now().isoformat()
                        ))

                        add_history_if_needed(
                            cursor,
                            sid,
                            curr
                        )

                        print(f"[자동 등록] {curr}")

                    else:

                        old_name = row[0]

                        if old_name != curr:

                            cursor.execute("""
                                UPDATE users
                                SET
                                    current_name = ?,
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

                            print(
                                f"[닉변 감지] "
                                f"{old_name} → {curr}"
                            )

                conn.commit()

            finally:
                conn.close()

        except Exception as e:
            print(f"[SAV 스캔 오류] {e}")

    print("[SAV 전체 스캔 완료]")

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

    async def setup_hook(self):

        synced = await self.tree.sync()

        print(f"슬래시 명령어 동기화 완료: {len(synced)}개")

    async def on_ready(self):

        print(f"Logged in as {self.user}")

        if not self.scan_loop.is_running():
            self.scan_loop.start()

    @tasks.loop(minutes=5)
    async def scan_loop(self):

        async with self.scan_lock:

            await scan_all_sav_files()

            conn = get_db()

            try:

                cursor = conn.cursor()

                cursor.execute("""
                    SELECT
                        steam_id,
                        name_key,
                        current_name
                    FROM users
                    WHERE is_monitored = 1
                """)

                rows = cursor.fetchall()

                cursor.execute("""
                    SELECT notify_id
                    FROM channels
                """)

                notify_channels = [
                    x[0]
                    for x in cursor.fetchall()
                    if x[0]
                ]

            finally:
                conn.close()

            if not rows:
                return

            steam_ids = [
                r[0]
                for r in rows
            ]

            players = await get_steam_users_info(steam_ids)

            p_dict = {
                p["steamid"]: p
                for p in players
            }

            conn = get_db()

            try:

                cursor = conn.cursor()

                for sid, name_key, old_name in rows:

                    player = p_dict.get(sid)

                    curr = await get_current_nickname(
                        sid,
                        player
                    )

                    if curr == old_name:
                        continue

                    cursor.execute("""
                        UPDATE users
                        SET
                            current_name = ?,
                            updated_at = ?
                        WHERE steam_id = ?
                    """, (
                        curr,
                        datetime.now().isoformat(),
                        sid
                    ))

                    changed = add_history_if_needed(
                        cursor,
                        sid,
                        curr
                    )

                    conn.commit()

                    if changed:

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

                                ch = self.get_channel(ch_id)

                                if not ch:
                                    ch = await self.fetch_channel(ch_id)

                                if ch:
                                    await ch.send(embed=embed)

                            except Exception as e:
                                print(f"[알림 오류] {e}")

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
# 유저 조회
# =========================

def find_user(cursor, target):

    if target.isdigit():

        cursor.execute("""
            SELECT
                steam_id,
                current_name,
                name_key,
                is_monitored
            FROM users
            WHERE steam_id = ?
        """, (target,))

    else:

        cursor.execute("""
            SELECT
                steam_id,
                current_name,
                name_key,
                is_monitored
            FROM users
            WHERE name_key = ?
        """, (target,))

    return cursor.fetchone()

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
                SELECT 1
                FROM users
                WHERE name_key = ?
            """, (별명.strip(),))

            if cursor.fetchone():

                return await i.followup.send(
                    "❌ 이미 사용중인 별명입니다"
                )

        row = find_user(cursor, target)

        if not row:

            return await i.followup.send(
                "❌ SAV에서 아직 발견되지 않은 유저입니다"
            )

        sid, current_name, name_key, monitored = row

        if monitored == 1:

            return await i.followup.send(
                "❌ 이미 감시중인 유저입니다"
            )

        new_name = (
            별명.strip()
            if 별명
            else name_key
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

        row = find_user(cursor, target)

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
        f"✅ 감시 해제 완료\n\n"
        f"등록 별명: {name_key}\n"
        f"최근 닉네임: {current_name}\n"
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

        if target.isdigit():

            cursor.execute("""
                SELECT
                    name_key,
                    steam_id,
                    current_name,
                    updated_at,
                    is_monitored
                FROM users
                WHERE steam_id = ?
            """, (target,))

        else:

            cursor.execute("""
                SELECT
                    name_key,
                    steam_id,
                    current_name,
                    updated_at,
                    is_monitored
                FROM users
                WHERE name_key = ?
            """, (target,))

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
            ORDER BY name_key COLLATE NOCASE
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
        "📡 현재 감시중인 유저 목록\n"
        "```text\n"
        "별명 / 현재닉 / SteamID\n"
    )

    for name, sid, current_name in rows:

        line = f"{name} / {current_name} / {sid}\n"

        if len(current_msg + line + "```") > 1900:

            current_msg += "```"
            messages.append(current_msg)

            current_msg = "```text\n"
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
# 실행
# =========================

if __name__ == "__main__":
    bot.run(TOKEN)
