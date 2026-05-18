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
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                        name_key TEXT PRIMARY KEY,
                        steam_id TEXT,
                        history TEXT,
                        is_monitored INTEGER DEFAULT 0)''')
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS channels (
                        guild_id TEXT PRIMARY KEY,
                        admin_id INTEGER,
                        notify_id INTEGER)''')
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect(DB_PATH)

def get_column_names():
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA table_info(users);")
        cols = [c[1].lower() for c in cursor.fetchall()]
        conn.close()
        
        name_col = "name_key" if "name_key" in cols else "NAME"
        sid_col = "steam_id" if "steam_id" in cols else "STEAM_ID"
        hist_col = "history" if "history" in cols else "HISTORY"
        mon_col = "is_monitored" if "is_monitored" in cols else "IS_MONITORED"
        return name_col, sid_col, hist_col, mon_col
    except:
        conn.close()
        return "name_key", "steam_id", "history", "is_monitored"

# --- [3. 유틸리티 ] ---
async def get_steam_users_info(steam_ids):
    if not steam_ids: 
        return []
    ids_str = ",".join(steam_ids)
    url = f"[http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key=](http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key=){STEAM_API_KEY}&steamids={ids_str}"
    try:
        res = await asyncio.to_thread(requests.get, url, timeout=10)
        if res.status_code == 200:
            return res.json().get('response', {}).get('players', [])
    except Exception as e:
        print(f"[스팀 API 에러] {e}")
    return []

async def get_nickname_from_xml(steam_id):
    url = f"[https://steamcommunity.com/profiles/](https://steamcommunity.com/profiles/){steam_id}/?xml=1"
    try:
        res = await asyncio.to_thread(requests.get, url, timeout=8)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            node = root.find('steamID')
            if node is not None: 
                return node.text
    except Exception as e:
        print(f"[XML 파싱 에러] {e}")
    return None

def parse_sav_file(file_bytes):
    found_players = []
    try:
        text_data = file_bytes.decode('utf-8', errors='ignore')
        steam_ids = set(re.findall(r'7656119\d{10}', text_data))
        for sid in steam_ids:
            found_players.append(sid)
    except Exception as e:
        print(f"[SAV 파일 파싱 에러] {e}")
    return found_players

def create_status_embed(display_name, sid, history, mode="notify", player=None, is_private=False):
    colors = {"add": discord.Color.green(), "notify": discord.Color.gold(), "history": discord.Color.blue(), "exist": discord.Color.red()}
    titles = {"add": "✨ 새 감시 대상 설정", "notify": "🔔 닉네임 변경 알림", "history": "📋 상세 변경 내역", "exist": "❌ 정보 안내"}
    
    embed = discord.Embed(title=titles.get(mode, "알림"), color=colors.get(mode, discord.Color.light_grey()))
    
    if player:
        embed.set_thumbnail(url=player.get('avatarfull'))
        status_map = {0: "🔴 오프라인", 1: "🟢 온라인", 2: "⛔ 바쁨", 3: "🌙 자리비움", 4: "💤 취침 중"}
        state = status_map.get(player.get('personastate', 0), "❓ 정보 없음")
        if is_private: 
            state = "🔒 비공개 계정"
        elif 'gameextrainfo' in player: 
            state = f"🕹️ 플레이 중: {player['gameextrainfo']}"
        embed.add_field(name="현재 상태", value=state, inline=False)

    embed.add_field(name="등록된 별명", value=display_name or "별명없음", inline=True)
    embed.add_field(name="최신 닉네임", value=history[-1] if history else "없음", inline=True)
    
    history_text = " → ".join(history)
    if len(history_text) > 1000:
        history_text = "...(생략)... " + history_text[-950:]
    embed.add_field(name=f"변경 내역 ({len(history)}개)", value=history_text, inline=False)
    
    embed.add_field(name="스팀 프로필", value=f"[바로가기](https://steamcommunity.com/profiles/{sid})", inline=False)
    embed.set_footer(text=f"ID: {sid} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return embed

# --- [4. 봇 클래스] ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user.name} ({self.user.id})")
        if not self.check_steam_nicknames.is_running():
            self.check_steam_nicknames.start()

    @tasks.loop(minutes=5.0)
    async def check_steam_nicknames(self):
        def fetch_and_update_users():
            name_col, sid_col, hist_col, mon_col = get_column_names()
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(f"SELECT {name_col}, {sid_col}, {hist_col}, {mon_col} FROM users")
            rows = cursor.fetchall()
            
            cursor.execute("SELECT notify_id FROM channels")
            channels = [ch[0] for ch in cursor.fetchall() if ch[0]]
            
            conn.close()
            return rows, channels, (name_col, sid_col, hist_col, mon_col)

        rows, notify_channels, cols = await asyncio.to_thread(fetch_and_update_users)
        name_col, sid_col, hist_col, mon_col = cols
        
        if not rows: 
            return
            
        ids = [row[1] for row in rows]
        players = await get_steam_users_info(ids)
        p_dict = {p['steamid']: p for p in players}
        
        for name_key, sid, history_str, is_monitored in rows:
            history = history_str.split(" | ") if history_str else []
            player = p_dict.get(sid)
            
            curr_nick = (player.get('personaname') if player and player.get('communityvisibilitystate') == 3 
                         else await get_nickname_from_xml(sid))
            
            if not curr_nick or curr_nick.strip() == "": 
                continue
            if history and curr_nick == history[-1]: 
                continue
            if len(history) >= 2 and curr_nick == history[-2]: 
                continue

            history.append(curr_nick)
            new_history_str = " | ".join(history)
            
            def update_db(h_str, n_key):
                conn = get_db()
                cursor = conn.cursor()
                cursor.execute(f"UPDATE users SET {hist_col} = ? WHERE {name_col} = ?", (h_str, n_key))
                conn.commit()
                conn.close()
                
            await asyncio.to_thread(update_db, new_history_str, name_key)
            
            if is_monitored == 1:
                is_private = player.get('communityvisibilitystate') != 3 if player else True
                embed = create_status_embed(name_key, sid, history, "notify", player, is_private)
                
                for ch_id in notify_channels:
                    try:
                        c = self.get_channel(ch_id) or await self.fetch_channel(ch_id)
                        if c: 
                            await c.send(embed=embed)
                    except Exception:
                        pass

bot = MyBot()

# --- [5. 명령어 구현] ---

@bot.tree.command(name="추가", description="유저를 '감시 대상'으로 등록하거나 기존 수집 대상을 감시로 전환합니다.")
async def add_user(i: discord.Interaction, steam_id: str, nickname: str = None):
    await i.response.defer()
    name_col, sid_col, hist_col, mon_col = await asyncio.to_thread(get_column_names)
    
    def check_existing_user():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {name_col}, {sid_col}, {hist_col}, {mon_col} FROM users WHERE {sid_col} = ?", (steam_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    exist_sid_row = await asyncio.to_thread(check_existing_user)
    
    if exist_sid_row:
        exist_name, exist_sid, exist_hist, is_monitored = exist_sid_row
        history_list = exist_hist.split(" | ") if exist_hist else ["없음"]
        
        if is_monitored == 1:
            players = await get_steam_users_info([exist_sid])
            player = players[0] if players else None
            embed = create_status_embed(exist_name, exist_sid, history_list, "exist", player)
            return await i.followup.send(content="❌ 이미 감시 대상으로 알림이 켜져 있는 유저입니다.", embed=embed)
        
        def enable_monitoring():
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(f"UPDATE users SET {mon_col} = 1 WHERE {sid_col} = ?", (steam_id,))
            conn.commit()
            conn.close()
            
        await asyncio.to_thread(enable_monitoring)
        return await i.followup.send(content=f"✅ 기존 수집 유저 `{exist_name}`(을)를 **감시 대상**으로 전환했습니다! 이제 닉네임 변경 알림이 옵니다.")

    players = await get_steam_users_info([steam_id])
    player = players[0] if players else None
    curr = (player.get('personaname') if player and player.get('communityvisibilitystate') == 3 
            else await get_nickname_from_xml(steam_id))
    
    if not curr:
        return await i.followup.send("❌ 유효하지 않거나 비공개/정지된 SteamID입니다.")

    final_nickname = nickname.strip() if nickname else curr.strip()

    def save_new_monitored_user():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"INSERT INTO users ({name_col}, {sid_col}, {hist_col}, {mon_col}) VALUES (?, ?, ?, 1)", (final_nickname, steam_id, curr))
        conn.commit()
        conn.close()

    await asyncio.to_thread(save_new_monitored_user)
    await i.followup.send(embed=create_status_embed(final_nickname, steam_id, [curr], "add", player))


@bot.tree.command(name="동기화", description="롱빈터 .sav 파일을 업로드하여 만난 유저 목록을 조용히 저장합니다.")
async def sync_sav_file(i: discord.Interaction, file: discord.Attachment):
    if not file.filename.endswith('.sav'):
        return await i.response.send_message("❌ `.sav` 형식의 파일만 업로드할 수 있습니다.", ephemeral=True)
        
    await i.response.defer()
    
    file_bytes = await file.read()
    discovered_steam_ids = await asyncio.to_thread(parse_sav_file, file_bytes)
    
    if not discovered_steam_ids:
        return await i.followup.send("❌ 파일 내에서 유효한 스팀 ID 패턴을 찾지 못했거나 빈 파일입니다.")
        
    name_col, sid_col, hist_col, mon_col = await asyncio.to_thread(get_column_names)
    
    players = await get_steam_users_info(discovered_steam_ids)
    p_dict = {p['steamid']: p.get('personaname', 'Unknown') for p in players}
    
    added_count = 0
    
    def bulk_insert_silent_users():
        nonlocal added_count
        conn = get_db()
        cursor = conn.cursor()
        
        for sid in discovered_steam_ids:
            cursor.execute(f"SELECT 1 FROM users WHERE {sid_col} = ?", (sid,))
            if cursor.fetchone():
                continue
                
            initial_nick = p_dict.get(sid, f"Unsaved_{sid[-4:]}")
            
            cursor.execute(f"SELECT 1 FROM users WHERE {name_col} = ?", (initial_nick,))
            if cursor.fetchone():
                initial_nick = f"{initial_nick}_{sid[-4:]}"
                
            cursor.execute(f"INSERT INTO users ({name_col}, {sid_col}, {hist_col}, {mon_col}) VALUES (?, ?, ?, 0)", (initial_nick, sid, initial_nick))
            added_count += 1
            
        conn.commit()
        conn.close()
        
    await asyncio.to_thread(bulk_insert_silent_users)
    await i.followup.send(f"📊 **.sav 동기화 완료!**\n새로운 롱빈터 조우 유저 **{added_count}명**을 데이터베이스에 추가했습니다. (※ 알림 감시는 꺼짐 상태이며, `/추가`를 통해 활성화할 수 있습니다.)")


@bot.tree.command(name="현황", description="전체 리스트 및 감시 여부 확인")
async def status_list(i: discord.Interaction):
    await i.response.defer()
    name_col, sid_col, hist_col, mon_col = await asyncio.to_thread(get_column_names)
    
    def fetch_all_users():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {name_col}, {sid_col}, {hist_col}, {mon_col} FROM users")
        rows = cursor.fetchall()
        conn.close()
        return rows

    rows = await asyncio.to_thread(fetch_all_users)

    if not rows: 
        return await i.followup.send("📊 저장된 유저가 없습니다.")
    
    pages = []
    # 💡 에러 원인이었던 코드 조각을 아래처럼 명확히 감싸서 수정했습니다.
    current_page = "📊 **전체 조우 현황 (🔔=감시중 / 🔇=기록만)**\n```text\n상태 / 별명 / 현재닉네임 / SteamID\n"
    
    for name, sid, hist, is_monitored in rows:
        last = hist.split(" | ")[-1] if hist else "없음"
        status_icon = "🔔" if is_monitored == 1 else "🔇"
        line = f"{status_icon} / {name} / {last} / {sid}\n"
        if len(current_page + line) > 1900:
            pages.append(current_page + "```")
            current_page = "
```text\n" + line
        else:
            current_page += line
            
    # 💡 이 부분이 완벽하게 닫히도록 마크다운 기호를 쌍따옴표 안에 온전히 채웠습니다.
    pages.append(current_page + "```")

    await i.followup.send(pages[0])
    for page in pages[1:]:
        await i.followup.send(page)

@bot.tree.command(name="내역", description="특정 유저의 변경 내역 확인")
async def user_history(i: discord.Interaction, target: str):
    await i.response.defer()
    name_col, sid_col, hist_col, _ = await asyncio.to_thread(get_column_names)
    
    def fetch_target_user():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {name_col}, {sid_col}, {hist_col} FROM users WHERE {name_col} = ? OR {sid_col} = ?", (target.strip(), target.strip()))
        row = cursor.fetchone()
        conn.close()
        return row

    row = await asyncio.to_thread(fetch_target_user)
    if not row: 
        return await i.followup.send(f"❌ `{target}`에 해당하는 유저를 목록에서 찾을 수 없습니다.")
    
    name, sid, hist_str = row
    history = hist_str.split(" | ") if hist_str else []
    players = await get_steam_users_info([sid])
    player = players[0] if players else None
    await i.followup.send(embed=create_status_embed(name, sid, history, "history", player))

@bot.tree.command(name="삭제", description="유저 데이터 완전 삭제")
async def delete_user(i: discord.Interaction, target: str):
    await i.response.defer()
    name_col, sid_col, _, _ = await asyncio.to_thread(get_column_names)
    def remove_user():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM users WHERE {name_col} = ? OR {sid_col} = ?", (target.strip(), target.strip()))
        count = cursor.rowcount
        if count > 0: conn.commit()
        conn.close()
        return count
    row_count = await asyncio.to_thread(remove_user)
    if row_count > 0: await i.followup.send(f"✅ `{target}` 정보 완전 삭제 완료")
    else: await i.followup.send("❌ 찾을 수 없습니다.")

@bot.tree.command(name="채널설정", description="채널 설정")
@app_commands.choices(역할=[app_commands.Choice(name="관리", value="admin"), app_commands.Choice(name="알림", value="notify")])
async def set_channel(i: discord.Interaction, 역할: str):
    if not i.user.guild_permissions.administrator: return await i.response.send_message("❌ 권한 없음", ephemeral=True)
    await i.response.defer()
    def update_channel_settings():
        conn = get_db()
        cursor = conn.cursor()
        col = "admin_id" if 역할 == "admin" else "notify_id"
        cursor.execute(f"INSERT INTO channels (guild_id, {col}) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET {col}=excluded.{col}", (str(i.guild_id), i.channel_id))
        conn.commit()
        conn.close()
    await asyncio.to_thread(update_channel_settings)
    await i.followup.send(f"✅ {역할} 채널 설정 완료")

if __name__ == "__main__":
    bot.run(TOKEN)
