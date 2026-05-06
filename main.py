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

# 레일웨이 볼륨 기능을 사용한다면 아래 경로를 '/app/data/bot_data.db'로 설정하세요.
# 볼륨 설정을 하지 않았다면 그냥 'bot_data.db'로 유지됩니다.
DB_PATH = os.getenv('DB_PATH', 'bot_data.db')

# --- [2. 데이터베이스 초기화 및 연결] ---
def init_db():
    # 데이터베이스 파일이 있는 디렉토리가 없으면 생성 (볼륨 경로 대비)
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # 유저 테이블: name_key(별명), steam_id, history(기록)
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        name_key TEXT PRIMARY KEY,
                        steam_id TEXT,
                        history TEXT)''')
    # 채널 설정 테이블: 서버별 관리/알림 채널 저장
    cursor.execute('''CREATE TABLE IF NOT EXISTS channels (
                        guild_id TEXT PRIMARY KEY,
                        admin_id INTEGER,
                        notify_id INTEGER)''')
    conn.commit()
    conn.close()

# 앱 시작 시 DB 초기화
init_db()

def get_db_connection():
    return sqlite3.connect(DB_PATH)

# --- [3. 유틸리티 함수] ---
async def get_steam_users_info(steam_ids):
    if not steam_ids: return []
    ids_str = ",".join(steam_ids)
    url = f"http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={ids_str}"
    try:
        res = await asyncio.to_thread(requests.get, url, timeout=10)
        if res.status_code == 200:
            return res.json().get('response', {}).get('players', [])
    except: return []
    return []

async def get_nickname_from_xml(steam_id):
    url = f"https://steamcommunity.com/profiles/{steam_id}/?xml=1"
    try:
        res = await asyncio.to_thread(requests.get, url, timeout=8)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            node = root.find('steamID')
            if node is not None: return node.text
    except: return None
    return None

def create_status_embed(display_name, sid, history, mode="notify", player=None, is_private=False):
    colors = {"add": discord.Color.green(), "notify": discord.Color.gold(), "history": discord.Color.blue()}
    titles = {"add": "✨ 새 감시 대상 추가", "notify": "🔔 닉네임 변경 알림", "history": "📋 상세 변경 내역"}
    
    embed = discord.Embed(title=titles.get(mode, "알림"), color=colors.get(mode, discord.Color.light_grey()))
    
    if player:
        embed.set_thumbnail(url=player.get('avatarfull'))
        status_map = {0: "🔴 오프라인", 1: "🟢 온라인", 2: "⛔ 바쁨", 3: "🌙 자리비움", 4: "💤 취침 중"}
        state = status_map.get(player.get('personastate', 0), "❓ 정보 없음")
        if is_private: state = "🔒 비공개 계정"
        elif 'gameextrainfo' in player: state = f"🕹️ 플레이 중: {player['gameextrainfo']}"
        embed.add_field(name="현재 상태", value=state, inline=False)

    embed.add_field(name="등록된 별명", value=display_name or "별명없음", inline=True)
    embed.add_field(name="최신 닉네임", value=history[-1] if history else "없음", inline=True)
    
    display_h = " → ".join(history)
    if len(display_h) > 1024:
        display_h = "...(생략)... → " + display_h[-950:]
    
    embed.add_field(name=f"변경 기록 ({len(history)}개)", value=display_h, inline=False)
    embed.add_field(name="스팀 프로필", value=f"[바로가기](https://steamcommunity.com/profiles/{sid})", inline=False)
    embed.set_footer(text=f"ID: {sid} | {datetime.now().strftime('%H:%M:%S')}")
    return embed

# --- [4. 봇 클래스] ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        if not self.check_steam_nicknames.is_running():
            self.check_steam_nicknames.start()
        await self.tree.sync()

    @tasks.loop(minutes=5.0)
    async def check_steam_nicknames(self):
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name_key, steam_id, history FROM users")
        rows = cursor.fetchall()
        
        if not rows:
            conn.close()
            return
            
        ids = [row[1] for row in rows]
        players = await get_steam_users_info(ids)
        p_dict = {p['steamid']: p for p in players}
        
        for name_key, sid, history_str in rows:
            history = history_str.split(" | ")
            player = p_dict.get(sid)
            curr_nick = None
            is_private = False

            if player and player.get('communityvisibilitystate') == 3:
                curr_nick = player.get('personaname')
            else:
                curr_nick = await get_nickname_from_xml(sid)
                is_private = True

            if not curr_nick or curr_nick.strip() == "": continue
                
            if history and curr_nick != history[-1]:
                if len(history) >= 2 and curr_nick == history[-2]: continue

                history.append(curr_nick)
                new_history_str = " | ".join(history)
                cursor.execute("UPDATE users SET history = ? WHERE name_key = ?", (new_history_str, name_key))
                conn.commit()
                
                # 알림 전송 (등록된 모든 알림 채널로 발송)
                embed = create_status_embed(name_key if name_key != "None" else None, sid, history, "notify", player, is_private)
                cursor.execute("SELECT notify_id FROM channels WHERE notify_id IS NOT NULL")
                notify_channels = cursor.fetchall()
                for (ch_id,) in notify_channels:
                    try:
                        channel = self.get_channel(ch_id) or await self.fetch_channel(ch_id)
                        if channel: await channel.send(embed=embed)
                    except: pass
        conn.close()

bot = MyBot()

# --- [5. 명령어] ---
@bot.tree.command(name="추가", description="감시 유저 추가")
async def add_user(i: discord.Interaction, steam_id: str, nickname: str = None):
    await i.response.defer()
    name_key = str(nickname) if nickname else "None"
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE steam_id = ? OR name_key = ?", (steam_id, name_key))
    if cursor.fetchone():
        conn.close()
        return await i.followup.send("❌ 이미 등록된 SteamID 또는 별명입니다.")

    players = await get_steam_users_info([steam_id])
    player = players[0] if players else None
    
    if player and player.get('communityvisibilitystate') == 3:
        curr = player.get('personaname')
        is_p = False
    else:
        curr = await get_nickname_from_xml(steam_id)
        is_p = True
    
    if not curr:
        conn.close()
        return await i.followup.send("❌ 유효하지 않은 SteamID입니다.")

    history = [curr]
    cursor.execute("INSERT INTO users (name_key, steam_id, history) VALUES (?, ?, ?)", 
                   (name_key, steam_id, " | ".join(history)))
    conn.commit()
    conn.close()
    
    await i.followup.send(embed=create_status_embed(nickname, steam_id, history, "add", player, is_p))

@bot.tree.command(name="현황", description="전체 감시 현황 확인")
async def status_list(i: discord.Interaction):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name_key, steam_id, history FROM users")
    rows = cursor.fetchall()
    conn.close()

    if not rows: return await i.response.send_message("📊 감시 유저가 없습니다.")
    
    header = "📊 **감시 현황**\n```text\n별명 / 현재닉네임 / SteamID\n"
    msg = header
    for name, sid, hist in rows:
        last_nick = hist.split(" | ")[-1]
        line = f"{name} / {last_nick} / {sid}\n"
        if len(msg + line) > 1900:
            await i.response.send_message(msg + "```")
            msg = "```text\n" + line
        else:
            msg += line
    await i.followup.send(msg + "```") if i.response.is_done() else await i.response.send_message(msg + "```")

@bot.tree.command(name="삭제", description="유저 삭제")
async def delete_user(i: discord.Interaction, target: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE name_key = ? OR steam_id = ?", (target, target))
    if cursor.rowcount > 0:
        conn.commit()
        await i.response.send_message(f"✅ `{target}` 삭제 완료")
    else:
        await i.response.send_message("❌ 찾을 수 없습니다.")
    conn.close()

@bot.tree.command(name="채널설정", description="관리/알림 채널 설정")
@app_commands.choices(역할=[app_commands.Choice(name="관리", value="admin"), app_commands.Choice(name="알림", value="notify")])
async def set_channel(i: discord.Interaction, 역할: str):
    if not i.user.guild_permissions.administrator: 
        return await i.response.send_message("❌ 권한이 없습니다.")
    
    gid = str(i.guild_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    col = "admin_id" if 역할 == "admin" else "notify_id"
    
    cursor.execute(f"INSERT INTO channels (guild_id, {col}) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET {col}=excluded.{col}", (gid, i.channel_id))
    conn.commit()
    conn.close()
    await i.response.send_message(f"✅ 현재 채널을 {역할} 채널로 설정했습니다.")

if __name__ == "__main__":
    bot.run(TOKEN)
