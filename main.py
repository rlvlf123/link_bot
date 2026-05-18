python
# 수정된 전체 코드 (오류 수정 완료 버전)

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
                f"❌ 이미 감시중인 SteamID입니다\n\n"
                f"등록 별명: {name_key}\n"
                f"최근 닉네임: {current_name}\n"
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
# 실행
# =========================

if __name__ == "__main__":
    bot.run(TOKEN)
