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

    db_dir = os.path.dirname(DB_PATH)

    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)

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

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_history_steam
            ON nickname_history(steam_id)
        """)

        cursor.execute("PRAGMA table_info(users)")

        cols = [c[1] for c in cursor.fetchall()]

        alter_queries = []

        if "current_name" not in cols:
            alter_queries.append(
                "ALTER TABLE users ADD COLUMN current_name TEXT"
            )

        if "eos_id" not in cols:
            alter_queries.append(
                "ALTER TABLE users ADD COLUMN eos_id TEXT"
            )

        if "updated_at" not in cols:
            alter_queries.append(
                "ALTER TABLE users ADD COLUMN updated_at TEXT"
            )

        if "is_monitored" not in cols:
            alter_queries.append(
                "ALTER TABLE users ADD COLUMN is_monitored INTEGER DEFAULT 0"
            )

        for q in alter_queries:

            try:
                cursor.execute(q)

            except Exception as e:
                print(f"[ALTER 오류] {e}")

        conn.commit()

    finally:
        conn.close()

def repair_current_names():

    conn = get_db()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            UPDATE users
            SET current_name = name_key
            WHERE current_name IS NULL
            OR current_name = ''
        """)

        conn.commit()

    except Exception as e:
        print(f"[DB 복구 오류] {e}")

    finally:
        conn.close()

def get_history_list(cursor, steam_id):

    cursor.execute("""
        SELECT nickname
        FROM nickname_history
        WHERE steam_id = ?
        ORDER BY id ASC
    """, (steam_id,))

    return [row[0] for row in cursor.fetchall()]

init_db()
repair_current_names()
enable_all_monitored()

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

    except Exception as e:
        print(f"[XML 오류] {e}")

    return None

# =========================
# SAV 파싱
# =========================

def parse_sav_file(file_bytes):

    results = []

    try:

        text_data = file_bytes.decode(
            "utf-8",
            errors="ignore"
        )

        steam_ids = set(
            re.findall(r"7656119\d{10}", text_data)
        )

        eos_ids = set(
            re.findall(
                r"[0-9a-f]{32}",
                text_data,
                re.IGNORECASE
            )
        )

        for sid in steam_ids:

            eos = None

            if eos_ids:
                eos = list(eos_ids)[0]

            results.append({
                "steam_id": sid,
                "eos_id": eos
            })

    except Exception as e:
        print(f"[SAV 파싱 오류] {e}")

    return results

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
        color=colors.get(mode, discord.Color.light_grey())
    )

    if player:
        embed.set_thumbnail(url=player.get("avatarfull"))

    embed.add_field(
        name="등록 별명",
        value=display_name or "없음",
        inline=True
    )

    embed.add_field(
        name="현재 닉네임",
        value=history[-1] if history else "없음",
        inline=True
    )

    history_text = " → ".join(history)

    if len(history_text) > 1000:
        history_text = "... " + history_text[-950:]

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

    embed.set_footer(
        text=f"{sid} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
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

    async def on_ready(self):

        print(f"Logged in as {self.user}")

        if not self.check_steam_nicknames.is_running():
            self.check_steam_nicknames.start()

    @tasks.loop(minutes=5)
    async def check_steam_nicknames(self):

        conn = get_db()

        try:

            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    steam_id,
                    name_key,
                    current_name,
                    is_monitored
                FROM users
            """)

            rows = cursor.fetchall()

            cursor.execute("""
                SELECT notify_id
                FROM channels
            """)

            notify_channels = [
                r[0]
                for r in cursor.fetchall()
                if r[0]
            ]

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

        for sid, name_key, current_name, monitored in rows:

            player = p_dict.get(sid)

            curr_nick = (
                player.get("personaname")
                if player and player.get("personaname")
                else await get_nickname_from_xml(sid)
            )

            if not curr_nick:
                continue

            curr_nick = str(curr_nick).strip()

            conn = get_db()

            try:

                cursor = conn.cursor()

                history = get_history_list(cursor, sid)

                if history and history[-1] == curr_nick:
                    continue

                cursor.execute("""
                    UPDATE users
                    SET
                        current_name = ?,
                        updated_at = ?
                    WHERE steam_id = ?
                """, (
                    curr_nick,
                    datetime.now().isoformat(),
                    sid
                ))

                cursor.execute("""
                    INSERT INTO nickname_history (
                        steam_id,
                        nickname,
                        changed_at
                    )
                    VALUES (?, ?, ?)
                """, (
                    sid,
                    curr_nick,
                    datetime.now().isoformat()
                ))

                conn.commit()

                history.append(curr_nick)

            finally:
                conn.close()

            if monitored != 1:
                continue

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
                    print(f"[채널 전송 오류] {e}")

bot = MyBot()

# =========================
# 채널 제한
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

@bot.tree.command(name="추가", description="유저 감시 등록")
async def add_user(
    i: discord.Interaction,
    steam_id: str,
    별명: str = None
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
                current_name,
                is_monitored
            FROM users
            WHERE steam_id = ?
        """, (steam_id,))

        row = cursor.fetchone()

        if row:

            name_key, current_name, monitored = row

            return await i.followup.send(
                f"❌ 이미 등록된 SteamID\n\n"
                f"등록 별명: {name_key}\n"
                f"현재 닉네임: {current_name}\n"
                f"SteamID: {steam_id}"
            )

        players = await get_steam_users_info([steam_id])

        player = players[0] if players else None

        curr = (
            player.get("personaname")
            if player and player.get("personaname")
            else await get_nickname_from_xml(steam_id)
        )

        if not curr:

            return await i.followup.send(
                "❌ 유효하지 않은 SteamID"
            )

        save_name = (
            별명.strip()
            if 별명
            else f"user_{steam_id[-6:]}"
        )

        cursor.execute("""
            SELECT steam_id
            FROM users
            WHERE name_key = ?
        """, (save_name,))

        existing_name = cursor.fetchone()

        if existing_name:

            return await i.followup.send(
                f"❌ 이미 사용 중인 별명입니다.\n\n"
                f"등록 별명: {save_name}\n"
                f"SteamID: {existing_name[0]}"
            )

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
            steam_id,
            save_name,
            curr,
            1,
            datetime.now().isoformat()
        ))

        cursor.execute("""
            INSERT INTO nickname_history (
                steam_id,
                nickname,
                changed_at
            )
            VALUES (?, ?, ?)
        """, (
            steam_id,
            curr,
            datetime.now().isoformat()
        ))

        conn.commit()

        await i.followup.send(
            f"✅ 감시 등록 완료\n\n"
            f"등록 별명: {save_name}\n"
            f"현재 닉네임: {curr}\n"
            f"SteamID: {steam_id}"
        )

    finally:
        conn.close()

# =========================
# /동기화
# =========================

@bot.tree.command(
    name="동기화",
    description=".sav 업로드"
)
async def sync_sav_file(
    i: discord.Interaction,
    file: discord.Attachment
):

    if not await check_admin_channel(i):
        return

    if not file.filename.endswith(".sav"):

        return await i.response.send_message(
            "❌ .sav 파일만 가능",
            ephemeral=True
        )

    await i.response.defer()

    file_bytes = await file.read()

    parsed = await asyncio.to_thread(
        parse_sav_file,
        file_bytes
    )

    if not parsed:

        return await i.followup.send(
            "❌ SteamID 발견 실패"
        )

    steam_ids = [
        p["steam_id"]
        for p in parsed
    ]

    players = await get_steam_users_info(steam_ids)

    p_dict = {
        p["steamid"]: p
        for p in players
    }

    added = 0

    conn = get_db()

    try:

        cursor = conn.cursor()

        for item in parsed:

            sid = item["steam_id"]
            eos = item["eos_id"]

            cursor.execute("""
                SELECT 1
                FROM users
                WHERE steam_id = ?
            """, (sid,))

            if cursor.fetchone():
                continue

            player = p_dict.get(sid)

            curr = (
                player.get("personaname")
                if player
                else f"Unknown_{sid[-4:]}"
            )

            save_name = f"user_{sid[-6:]}"

            temp_name = save_name
            count = 1

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

            cursor.execute("""
                INSERT INTO nickname_history (
                    steam_id,
                    nickname,
                    changed_at
                )
                VALUES (?, ?, ?)
            """, (
                sid,
                curr,
                datetime.now().isoformat()
            ))

            added += 1

        conn.commit()

    finally:
        conn.close()

    await i.followup.send(
        f"✅ 동기화 완료\n새 유저 {added}명 저장"
    )

# =========================
# /현황
# =========================

@bot.tree.command(
    name="현황",
    description="전체 리스트"
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
                current_name,
                is_monitored
            FROM users
            ORDER BY updated_at DESC
        """)

        rows = cursor.fetchall()

    finally:
        conn.close()

    if not rows:
        return await i.followup.send("📊 저장된 유저 없음")

    messages = []

    current_msg = (
        "📊 전체 현황\n"
        "```text\n"
        "상태 / 저장별명 / 현재닉 / SteamID\n"
    )

    for name, sid, current_name, monitored in rows:

        icon = "🔔" if monitored else "🔇"

        line = f"{icon} / {name} / {current_name} / {sid}\n"

        if len(current_msg + line + "```") > 1900:

            current_msg += "```"
            messages.append(current_msg)

            current_msg = (
                "```text\n"
                f"{line}"
            )

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
                steam_id
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

        name, sid = row

        history = get_history_list(cursor, sid)

    finally:
        conn.close()

    players = await get_steam_users_info([sid])

    player = players[0] if players else None

    await i.followup.send(
        embed=create_status_embed(
            name,
            sid,
            history,
            "history",
            player
        )
    )

# =========================
# /삭제
# =========================

@bot.tree.command(
    name="삭제",
    description="유저 삭제"
)
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
                name_key,
                steam_id
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
                "❌ 찾을 수 없는 유저"
            )

        name_key, steam_id = row

        cursor.execute("""
            DELETE FROM users
            WHERE steam_id = ?
        """, (steam_id,))

        cursor.execute("""
            DELETE FROM nickname_history
            WHERE steam_id = ?
        """, (steam_id,))

        conn.commit()

    finally:
        conn.close()

    await i.followup.send(
        f"✅ 삭제 완료\n\n"
        f"삭제된 별명: {name_key}\n"
        f"SteamID: {steam_id}"
    )

# =========================
# /채널설정
# =========================

@bot.tree.command(
    name="채널설정",
    description="알림 채널 설정"
)
@app_commands.choices(
    역할=[
        app_commands.Choice(
            name="관리",
            value="admin"
        ),
        app_commands.Choice(
            name="알림",
            value="notify"
        )
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
