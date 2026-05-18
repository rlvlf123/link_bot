import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
import sqlite3
import os
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime

# --- [1. 설정 정보] ---
TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
DB_PATH = os.getenv('DB_PATH', 'bot_data.db')

if not TOKEN:
    raise ValueError("DISCORD_TOKEN 환경변수가 없습니다.")

if not STEAM_API_KEY:
    raise ValueError("STEAM_API_KEY 환경변수가 없습니다.")

# --- [2. 데이터베이스 초기화] ---
def init_db():
    db_dir = os.path.dirname(DB_PATH)

    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT * FROM users LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                name_key TEXT PRIMARY KEY,
                steam_id TEXT,
                history TEXT
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
    return sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False
    )


def get_column_names():
    """DB 컬럼명 자동 감지"""

    conn = get_db()
    cursor = conn.cursor()

    try:
        cursor.execute("PRAGMA table_info(users);")
        cols = [c[1].lower() for c in cursor.fetchall()]
        conn.close()

        name_col = "name_key" if "name_key" in cols else "NAME"
        sid_col = "steam_id" if "steam_id" in cols else "STEAM_ID"
        hist_col = "history" if "history" in cols else "HISTORY"

        return name_col, sid_col, hist_col

    except:
        conn.close()
        return "name_key", "steam_id", "history"


# --- [3. 유틸리티] ---

def chunked(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


async def get_steam_users_info(steam_ids):

    if not steam_ids:
        return []

    all_players = []

    for chunk in chunked(steam_ids, 100):

        ids_str = ",".join(chunk)

        url = (
            f"http://api.steampowered.com/"
            f"ISteamUser/GetPlayerSummaries/v0002/"
            f"?key={STEAM_API_KEY}&steamids={ids_str}"
        )

        try:
            res = await asyncio.to_thread(
                requests.get,
                url,
                timeout=10
            )

            if res.status_code == 200:
                players = res.json().get('response', {}).get('players', [])
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

            node = root.find('steamID')

            if node is not None:
                return node.text

    except Exception as e:
        print(f"[XML 조회 오류] {e}")

    return None


def create_status_embed(
    display_name,
    sid,
    history,
    mode="notify",
    player=None,
    is_private=False
):

    colors = {
        "add": discord.Color.green(),
        "notify": discord.Color.gold(),
        "history": discord.Color.blue(),
        "exist": discord.Color.red()
    }

    titles = {
        "add": "✨ 새 감시 대상 추가",
        "notify": "🔔 닉네임 변경 알림",
        "history": "📋 상세 변경 내역",
        "exist": "❌ 이미 등록된 유저 정보"
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

        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True

        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):

        self.check_steam_nicknames.start()

        try:
            synced = await self.tree.sync()
            print(f"[슬래시 커맨드 동기화 완료] {len(synced)}개")
        except Exception as e:
            print(f"[슬래시 동기화 실패] {e}")

    @tasks.loop(minutes=5.0)
    async def check_steam_nicknames(self):

        name_col, sid_col, hist_col = get_column_names()

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            f"SELECT {name_col}, {sid_col}, {hist_col} FROM users"
        )

        rows = cursor.fetchall()

        if not rows:
            conn.close()
            return

        ids = [row[1] for row in rows]

        players = await get_steam_users_info(ids)

        p_dict = {
            p['steamid']: p for p in players
        }

        for name_key, sid, history_str in rows:

            history = history_str.split(" | ") if history_str else []

            player = p_dict.get(sid)

            is_private = True

            if player:
                is_private = (
                    player.get('communityvisibilitystate') != 3
                )

            curr_nick = (
                player.get('personaname')
                if player and not is_private
                else await get_nickname_from_xml(sid)
            )

            if not curr_nick:
                continue

            curr_nick = curr_nick.strip()

            if curr_nick == "":
                continue

            if history and curr_nick == history[-1]:
                continue

            if len(history) >= 2 and curr_nick == history[-2]:
                continue

            history.append(curr_nick)

            cursor.execute(
                f"""
                UPDATE users
                SET {hist_col} = ?
                WHERE {name_col} = ?
                """,
                (" | ".join(history), name_key)
            )

            conn.commit()

            embed = create_status_embed(
                name_key,
                sid,
                history,
                "notify",
                player,
                is_private
            )

            cursor.execute("SELECT notify_id FROM channels")

            channels = cursor.fetchall()

            for (ch_id,) in channels:

                if not ch_id:
                    continue

                try:
                    channel = (
                        self.get_channel(ch_id)
                        or await self.fetch_channel(ch_id)
                    )

                    if channel:
                        await channel.send(embed=embed)

                except Exception as e:
                    print(f"[채널 전송 실패] {e}")

        conn.close()


bot = MyBot()

# --- [5. 명령어 구현] ---

@bot.tree.command(
    name="추가",
    description="유저 추가"
)
async def add_user(
    i: discord.Interaction,
    steam_id: str,
    nickname: str = None
):

    await i.response.defer()

    name_col, sid_col, hist_col = get_column_names()

    conn = get_db()
    cursor = conn.cursor()

    # SteamID 중복 검사
    cursor.execute(
        f"""
        SELECT {name_col}, {sid_col}, {hist_col}
        FROM users
        WHERE {sid_col} = ?
        """,
        (steam_id,)
    )

    exist_sid_row = cursor.fetchone()

    if exist_sid_row:

        exist_name, exist_sid, exist_hist = exist_sid_row

        history_list = (
            exist_hist.split(" | ")
            if exist_hist else ["없음"]
        )

        conn.close()

        players = await get_steam_users_info([exist_sid])

        player = players[0] if players else None

        embed = create_status_embed(
            exist_name,
            exist_sid,
            history_list,
            "exist",
            player
        )

        return await i.followup.send(
            content="❌ 이미 등록된 SteamID입니다.",
            embed=embed
        )

    # Steam 정보 조회
    players = await get_steam_users_info([steam_id])

    player = players[0] if players else None

    is_private = True

    if player:
        is_private = (
            player.get('communityvisibilitystate') != 3
        )

    curr = (
        player.get('personaname')
        if player and not is_private
        else await get_nickname_from_xml(steam_id)
    )

    if not curr:

        conn.close()

        return await i.followup.send(
            "❌ 유효하지 않거나 비공개/정지된 SteamID입니다."
        )

    final_nickname = (
        nickname.strip()
        if nickname
        else curr.strip()
    )

    # 별명 중복 검사
    cursor.execute(
        f"""
        SELECT {name_col}, {sid_col}, {hist_col}
        FROM users
        WHERE {name_col} = ?
        """,
        (final_nickname,)
    )

    exist_name_row = cursor.fetchone()

    if exist_name_row:

        exist_name, exist_sid, exist_hist = exist_name_row

        history_list = (
            exist_hist.split(" | ")
            if exist_hist else ["없음"]
        )

        conn.close()

        embed = create_status_embed(
            exist_name,
            exist_sid,
            history_list,
            "exist",
            player
        )

        return await i.followup.send(
            content=f"❌ `{final_nickname}` 별명이 이미 존재합니다.",
            embed=embed
        )

    # DB 저장
    cursor.execute(
        f"""
        INSERT INTO users
        ({name_col}, {sid_col}, {hist_col})
        VALUES (?, ?, ?)
        """,
        (
            final_nickname,
            steam_id,
            curr
        )
    )

    conn.commit()
    conn.close()

    await i.followup.send(
        embed=create_status_embed(
            final_nickname,
            steam_id,
            [curr],
            "add",
            player,
            is_private
        )
    )


@bot.tree.command(
    name="현황",
    description="전체 감시 리스트"
)
async def status_list(i: discord.Interaction):

    name_col, sid_col, hist_col = get_column_names()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        f"""
        SELECT {name_col}, {sid_col}, {hist_col}
        FROM users
        """
    )

    rows = cursor.fetchall()

    conn.close()

    if not rows:
        return await i.response.send_message(
            "📊 감시 유저가 없습니다."
        )

    pages = []

    current_page = (
        "📊 **감시 현황**\n"
        "```text\n"
        "별명 / 현재닉네임 / SteamID\n"
    )

    for name, sid, hist in rows:

        last = (
            hist.split(" | ")[-1]
            if hist else "없음"
        )

        line = f"{name} / {last} / {sid}\n"

        if len(current_page + line) > 1900:

            pages.append(current_page + "```")

            current_page = "```text\n" + line

        else:
            current_page += line

    pages.append(current_page + "```")

    await i.response.send_message(pages[0])

    for page in pages[1:]:
        await i.followup.send(page)


@bot.tree.command(
    name="내역",
    description="닉네임 변경 내역 조회"
)
async def user_history(
    i: discord.Interaction,
    target: str
):

    name_col, sid_col, hist_col = get_column_names()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        f"""
        SELECT {name_col}, {sid_col}, {hist_col}
        FROM users
        WHERE {name_col} = ?
        OR {sid_col} = ?
        """,
        (
            target.strip(),
            target.strip()
        )
    )

    row = cursor.fetchone()

    conn.close()

    if not row:
        return await i.response.send_message(
            f"❌ `{target}` 유저를 찾을 수 없습니다."
        )

    name, sid, hist_str = row

    history = (
        hist_str.split(" | ")
        if hist_str else []
    )

    players = await get_steam_users_info([sid])

    player = players[0] if players else None

    await i.response.send_message(
        embed=create_status_embed(
            name,
            sid,
            history,
            "history",
            player
        )
    )


@bot.tree.command(
    name="삭제",
    description="유저 삭제"
)
async def delete_user(
    i: discord.Interaction,
    target: str
):

    name_col, sid_col, hist_col = get_column_names()

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute(
        f"""
        DELETE FROM users
        WHERE {name_col} = ?
        OR {sid_col} = ?
        """,
        (
            target.strip(),
            target.strip()
        )
    )

    if cursor.rowcount > 0:

        conn.commit()

        await i.response.send_message(
            f"✅ `{target}` 삭제 완료"
        )

    else:

        await i.response.send_message(
            "❌ 찾을 수 없습니다."
        )

    conn.close()


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
            "❌ 관리자 권한 필요"
        )

    conn = get_db()
    cursor = conn.cursor()

    col = (
        "admin_id"
        if 역할 == "admin"
        else "notify_id"
    )

    cursor.execute(
        f"""
        INSERT INTO channels
        (guild_id, {col})
        VALUES (?, ?)

        ON CONFLICT(guild_id)
        DO UPDATE SET
        {col}=excluded.{col}
        """,
        (
            str(i.guild_id),
            i.channel_id
        )
    )

    conn.commit()
    conn.close()

    await i.response.send_message(
        f"✅ {역할} 채널 설정 완료"
    )


# --- [6. 실행] ---

if __name__ == "__main__":

    print("[봇 시작 중...]")

    bot.run(TOKEN)
