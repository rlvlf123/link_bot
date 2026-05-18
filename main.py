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

# --- [1. 설정 정보] ---
TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
DB_PATH = os.getenv('DB_PATH', 'bot_data.db')

# --- [2. 데이터베이스 초기화] ---
def init_db():

    db_dir = os.path.dirname(DB_PATH)

    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            steam_id TEXT PRIMARY KEY,
            eos_id TEXT,
            name_key TEXT,
            current_name TEXT,
            history TEXT,
            first_seen TEXT,
            last_seen TEXT,
            last_server TEXT,
            is_monitored INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            guild_id TEXT PRIMARY KEY,
            admin_id INTEGER,
            notify_id INTEGER
        )
    ''')

    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect(DB_PATH, timeout=10)

# --- [3. 유틸리티 ] ---

async def get_steam_users_info(steam_ids):

    if not steam_ids:
        return []

    ids_str = ",".join(steam_ids)

    url = (
        f"http://api.steampowered.com/ISteamUser/"
        f"GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={ids_str}"
    )

    try:
        res = await asyncio.to_thread(requests.get, url, timeout=10)

        if res.status_code == 200:
            return res.json().get('response', {}).get('players', [])

    except Exception as e:
        print(f"[스팀 API 에러] {e}")

    return []

async def get_nickname_from_xml(steam_id):

    url = f"https://steamcommunity.com/profiles/{steam_id}/?xml=1"

    try:
        res = await asyncio.to_thread(requests.get, url, timeout=8)

        if res.status_code == 200:

            root = ET.fromstring(res.content)

            node = root.find('steamID')

            if node is not None and node.text:
                return node.text.strip()

    except Exception as e:
        print(f"[XML 파싱 에러] {e}")

    return None

def parse_sav_file(file_bytes, filename):

    found_players = []

    try:
        text_data = file_bytes.decode('utf-8', errors='ignore')

        steam_ids = re.findall(r'7656119\d{10}', text_data)

        eos_ids = re.findall(
            r'\b[A-F0-9]{32}\b',
            text_data
        )

        server_name = "알수없음"

        lower_name = filename.lower()

        if "uuvana1" in lower_name:
            server_name = "우바나1"

        elif "uuvana2" in lower_name:
            server_name = "우바나2"

        elif "uuvana3" in lower_name:
            server_name = "우바나3"

        elif "hardcore" in lower_name:
            server_name = "하드코어"

        for idx, sid in enumerate(steam_ids):

            eos = eos_ids[idx] if idx < len(eos_ids) else None

            found_players.append({
                "steam_id": sid,
                "eos_id": eos,
                "server": server_name
            })

    except Exception as e:
        print(f"[SAV 파일 파싱 에러] {e}")

    return found_players

def create_status_embed(
    display_name,
    sid,
    history,
    mode="notify",
    player=None,
    is_private=False,
    eos_id=None,
    first_seen=None,
    last_seen=None,
    last_server=None
):

    colors = {
        "add": discord.Color.green(),
        "notify": discord.Color.gold(),
        "history": discord.Color.blue(),
        "exist": discord.Color.red()
    }

    titles = {
        "add": "✨ 새 감시 대상 설정",
        "notify": "🔔 닉네임 변경 알림",
        "history": "📋 상세 변경 내역",
        "exist": "❌ 정보 안내"
    }

    embed = discord.Embed(
        title=titles.get(mode, "알림"),
        color=colors.get(mode, discord.Color.light_grey())
    )

    if player:

        embed.set_thumbnail(url=player.get('avatarfull'))

        status_map = {
            0: "🔴 오프라인",
            1: "🟢 온라인",
            2: "⛔ 바쁨",
            3: "🌙 자리비움",
            4: "💤 취침 중"
        }

        state = status_map.get(
            player.get('personastate', 0),
            "❓ 정보 없음"
        )

        if is_private:
            state = "🔒 비공개 계정"

        elif 'gameextrainfo' in player:
            state = f"🕹️ 플레이 중: {player['gameextrainfo']}"

        embed.add_field(
            name="현재 상태",
            value=state,
            inline=False
        )

    embed.add_field(
        name="등록된 별명",
        value=display_name or "별명없음",
        inline=True
    )

    embed.add_field(
        name="최신 닉네임",
        value=history[-1] if history else "없음",
        inline=True
    )

    if eos_id:
        embed.add_field(
            name="EOS ID",
            value=eos_id,
            inline=False
        )

    if first_seen:
        embed.add_field(
            name="처음 조우",
            value=first_seen,
            inline=True
        )

    if last_seen:
        embed.add_field(
            name="마지막 조우",
            value=last_seen,
            inline=True
        )

    if last_server:
        embed.add_field(
            name="최근 서버",
            value=last_server,
            inline=True
        )

    history_text = " → ".join(history)

    if len(history_text) > 1000:
        history_text = "...(생략)... " + history_text[-950:]

    embed.add_field(
        name=f"변경 내역 ({len(history)}개)",
        value=history_text,
        inline=False
    )

    embed.add_field(
        name="스팀 프로필",
        value=f"https://steamcommunity.com/profiles/{sid}",
        inline=False
    )

    embed.set_footer(
        text=f"ID: {sid} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    return embed

# --- [4. 봇 클래스] ---

class MyBot(commands.Bot):

    def __init__(self):

        super().__init__(
            command_prefix="!",
            intents=discord.Intents.all()
        )

    async def setup_hook(self):

        try:
            synced = await self.tree.sync()
            print(f"슬래시 명령어 동기화 완료: {len(synced)}개")

        except Exception as e:
            print(f"[슬래시 동기화 오류] {e}")

    async def on_ready(self):

        print(f"Logged in as {self.user.name} ({self.user.id})")

        if not self.check_steam_nicknames.is_running():
            self.check_steam_nicknames.start()

    @tasks.loop(minutes=5.0)
    async def check_steam_nicknames(self):

        def fetch_users():

            conn = get_db()
            cursor = conn.cursor()

            cursor.execute('''
                SELECT
                    steam_id,
                    name_key,
                    history,
                    is_monitored,
                    eos_id,
                    first_seen,
                    last_seen,
                    last_server
                FROM users
            ''')

            rows = cursor.fetchall()

            cursor.execute(
                "SELECT guild_id, notify_id FROM channels"
            )

            channels = cursor.fetchall()

            conn.close()

            return rows, channels

        rows, notify_channels = await asyncio.to_thread(fetch_users)

        if not rows:
            return

        ids = [row[0] for row in rows]

        players = await get_steam_users_info(ids)

        p_dict = {p['steamid']: p for p in players}

        for (
            sid,
            name_key,
            history_str,
            is_monitored,
            eos_id,
            first_seen,
            last_seen,
            last_server
        ) in rows:

            history = history_str.split(" | ") if history_str else []

            player = p_dict.get(sid)

            curr_nick = (
                player.get('personaname')
                if player and player.get('communityvisibilitystate') == 3
                else await get_nickname_from_xml(sid)
            )

            if not curr_nick:
                continue

            curr_nick = str(curr_nick).strip()

            changed = False

            if not history:
                history.append(curr_nick)
                changed = True

            elif curr_nick != history[-1]:

                history.append(curr_nick)

                if len(history) > 30:
                    history = history[-30:]

                changed = True

            if changed:

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                new_history_str = " | ".join(history)

                def update_user():

                    conn = get_db()
                    cursor = conn.cursor()

                    cursor.execute(
                        '''
                        UPDATE users
                        SET
                            history = ?,
                            current_name = ?,
                            last_seen = ?
                        WHERE steam_id = ?
                        ''',
                        (
                            new_history_str,
                            curr_nick,
                            now,
                            sid
                        )
                    )

                    conn.commit()
                    conn.close()

                await asyncio.to_thread(update_user)

                if is_monitored == 1:

                    is_private = (
                        player.get('communityvisibilitystate') != 3
                        if player else True
                    )

                    embed = create_status_embed(
                        name_key,
                        sid,
                        history,
                        "notify",
                        player,
                        is_private,
                        eos_id,
                        first_seen,
                        now,
                        last_server
                    )

                    for guild_id, ch_id in notify_channels:

                        if not ch_id:
                            continue

                        try:

                            c = self.get_channel(ch_id)

                            if not c:
                                c = await self.fetch_channel(ch_id)

                            if c:
                                await c.send(embed=embed)

                        except Exception as e:
                            print(f"[채널 전송 실패] {e}")

bot = MyBot()

# --- [5. 명령어 구현] ---

@bot.tree.command(
    name="추가",
    description="유저를 감시 대상으로 등록합니다."
)
async def add_user(
    i: discord.Interaction,
    steam_id: str
):

    await i.response.defer()

    def get_user():

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            '''
            SELECT
                steam_id,
                eos_id,
                name_key,
                current_name,
                history,
                first_seen,
                last_seen,
                last_server,
                is_monitored
            FROM users
            WHERE steam_id = ?
            ''',
            (steam_id,)
        )

        row = cursor.fetchone()

        conn.close()

        return row

    row = await asyncio.to_thread(get_user)

    if not row:
        return await i.followup.send(
            "❌ DB에 없는 유저입니다. 먼저 /동기화 하세요."
        )

    (
        sid,
        eos_id,
        name_key,
        current_name,
        history_str,
        first_seen,
        last_seen,
        last_server,
        is_monitored
    ) = row

    history = history_str.split(" | ") if history_str else []

    if is_monitored == 1:

        return await i.followup.send(
            "❌ 이미 감시중인 유저입니다."
        )

    def enable_monitoring():

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            '''
            UPDATE users
            SET is_monitored = 1
            WHERE steam_id = ?
            ''',
            (steam_id,)
        )

        conn.commit()
        conn.close()

    await asyncio.to_thread(enable_monitoring)

    players = await get_steam_users_info([sid])

    player = players[0] if players else None

    embed = create_status_embed(
        name_key,
        sid,
        history,
        "add",
        player,
        False,
        eos_id,
        first_seen,
        last_seen,
        last_server
    )

    await i.followup.send(embed=embed)

@bot.tree.command(
    name="동기화",
    description="롱빈터 .sav 파일 업로드"
)
async def sync_sav_file(
    i: discord.Interaction,
    file: discord.Attachment
):

    if not file.filename.endswith('.sav'):

        return await i.response.send_message(
            "❌ .sav 파일만 업로드 가능합니다.",
            ephemeral=True
        )

    await i.response.defer()

    file_bytes = await file.read()

    discovered_players = await asyncio.to_thread(
        parse_sav_file,
        file_bytes,
        file.filename
    )

    if not discovered_players:

        return await i.followup.send(
            "❌ SteamID를 찾지 못했습니다."
        )

    players = await get_steam_users_info(
        [p["steam_id"] for p in discovered_players]
    )

    p_dict = {
        p['steamid']: p.get('personaname', 'Unknown')
        for p in players
    }

    added_count = 0
    updated_count = 0

    def bulk_insert():

        nonlocal added_count
        nonlocal updated_count

        conn = get_db()
        cursor = conn.cursor()

        now = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        for pdata in discovered_players:

            sid = pdata["steam_id"]
            eos = pdata["eos_id"]
            server = pdata["server"]

            current_nick = p_dict.get(
                sid,
                f"Unknown_{sid[-4:]}"
            )

            cursor.execute(
                '''
                SELECT history
                FROM users
                WHERE steam_id = ?
                ''',
                (sid,)
            )

            existing = cursor.fetchone()

            if existing:

                history_str = existing[0]

                history = (
                    history_str.split(" | ")
                    if history_str else []
                )

                if not history or current_nick != history[-1]:

                    history.append(current_nick)

                    if len(history) > 30:
                        history = history[-30:]

                new_history = " | ".join(history)

                cursor.execute(
                    '''
                    UPDATE users
                    SET
                        eos_id = ?,
                        current_name = ?,
                        history = ?,
                        last_seen = ?,
                        last_server = ?
                    WHERE steam_id = ?
                    ''',
                    (
                        eos,
                        current_nick,
                        new_history,
                        now,
                        server,
                        sid
                    )
                )

                updated_count += 1

                continue

            cursor.execute(
                '''
                INSERT INTO users (
                    steam_id,
                    eos_id,
                    name_key,
                    current_name,
                    history,
                    first_seen,
                    last_seen,
                    last_server,
                    is_monitored
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    sid,
                    eos,
                    current_nick,
                    current_nick,
                    current_nick,
                    now,
                    now,
                    server,
                    0
                )
            )

            added_count += 1

        conn.commit()
        conn.close()

    await asyncio.to_thread(bulk_insert)

    await i.followup.send(
        f"📊 동기화 완료!\n"
        f"새 유저: {added_count}명\n"
        f"기존 업데이트: {updated_count}명"
    )

@bot.tree.command(
    name="내역",
    description="닉네임 변경 내역 확인"
)
async def user_history(
    i: discord.Interaction,
    target: str
):

    await i.response.defer()

    def fetch_user():

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            '''
            SELECT
                steam_id,
                eos_id,
                name_key,
                current_name,
                history,
                first_seen,
                last_seen,
                last_server
            FROM users
            WHERE steam_id = ?
            OR name_key = ?
            ''',
            (target, target)
        )

        row = cursor.fetchone()

        conn.close()

        return row

    row = await asyncio.to_thread(fetch_user)

    if not row:

        return await i.followup.send(
            "❌ 유저를 찾을 수 없음"
        )

    (
        sid,
        eos_id,
        name_key,
        current_name,
        history_str,
        first_seen,
        last_seen,
        last_server
    ) = row

    history = history_str.split(" | ") if history_str else []

    players = await get_steam_users_info([sid])

    player = players[0] if players else None

    embed = create_status_embed(
        name_key,
        sid,
        history,
        "history",
        player,
        False,
        eos_id,
        first_seen,
        last_seen,
        last_server
    )

    await i.followup.send(embed=embed)

@bot.tree.command(
    name="현황",
    description="전체 리스트 확인"
)
async def status_list(i: discord.Interaction):

    await i.response.defer()

    def fetch_all():

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            '''
            SELECT
                name_key,
                current_name,
                steam_id,
                last_server,
                is_monitored
            FROM users
            '''
        )

        rows = cursor.fetchall()

        conn.close()

        return rows

    rows = await asyncio.to_thread(fetch_all)

    if not rows:

        return await i.followup.send(
            "📊 저장된 유저 없음"
        )

    pages = []

    B = "```"

    current_page = (
        f"📊 전체 현황\n{B}text\n"
        f"상태 / 별명 / 현재닉 / 서버\n"
    )

    for (
        name,
        current_name,
        sid,
        last_server,
        is_monitored
    ) in rows:

        icon = "🔔" if is_monitored else "🔇"

        line = (
            f"{icon} / "
            f"{name} / "
            f"{current_name} / "
            f"{last_server}\n"
        )

        if len(current_page + line) > 1900:

            pages.append(f"{current_page}{B}")

            current_page = f"{B}text\n{line}"

        else:
            current_page += line

    pages.append(f"{current_page}{B}")

    await i.followup.send(pages[0])

    for page in pages[1:]:
        await i.followup.send(page)

@bot.tree.command(
    name="삭제",
    description="유저 삭제"
)
async def delete_user(
    i: discord.Interaction,
    target: str
):

    await i.response.defer()

    def remove_user():

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            '''
            DELETE FROM users
            WHERE steam_id = ?
            OR name_key = ?
            ''',
            (target, target)
        )

        count = cursor.rowcount

        if count > 0:
            conn.commit()

        conn.close()

        return count

    row_count = await asyncio.to_thread(remove_user)

    if row_count > 0:

        await i.followup.send(
            f"✅ `{target}` 삭제 완료"
        )

    else:

        await i.followup.send(
            "❌ 찾을 수 없음"
        )

@bot.tree.command(
    name="채널설정",
    description="채널 설정"
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

    def update_channel():

        conn = get_db()
        cursor = conn.cursor()

        col = (
            "admin_id"
            if 역할 == "admin"
            else "notify_id"
        )

        cursor.execute(
            f'''
            INSERT INTO channels
            (guild_id, {col})
            VALUES (?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET
            {col}=excluded.{col}
            ''',
            (
                str(i.guild_id),
                i.channel_id
            )
        )

        conn.commit()
        conn.close()

    await asyncio.to_thread(update_channel)

    await i.followup.send(
        f"✅ {역할} 채널 설정 완료"
    )

if __name__ == "__main__":
    bot.run(TOKEN)
