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
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                            name_key TEXT PRIMARY KEY,
                            steam_id TEXT,
                            history TEXT)''')
    
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
    """DB 파일의 실제 컬럼명 구조를 감지하여 반환합니다 (동기 함수)."""
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
    except Exception as e:
        print(f"[DB 컬럼 확인 에러] {e}")
        try: conn.close() 
        except: pass
        return "name_key", "steam_id", "history"

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
    except ET.ParseError:
        print(f"[XML 파싱 에러] 올바르지 않은 XML 형식이거나 스팀 서버 점검 중입니다.")
    except Exception as e:
        print(f"[XML 통신 에러] {e}")
    return None

def create_status_embed(display_name, sid, history, mode="notify", player=None, is_private=False):
    colors = {"add": discord.Color.green(), "notify": discord.Color.gold(), "history": discord.Color.blue(), "exist": discord.Color.red()}
    titles = {"add": "✨ 새 감시 대상 추가", "notify": "🔔 닉네임 변경 알림", "history": "📋 상세 변경 내역", "exist": "❌ 이미 등록된 유저 정보"}
    
    embed = discord.Embed(title=titles.get(mode, "알림"), color=colors.get(mode, discord.Color.light_grey()))
    
    if player:
        if player.get('avatarfull'):
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
        # 슬래시 명령어 동기화
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user.name} ({self.user.id})")
        # 봇 로그인 완료 후 안정적으로 루프 시작
        if not self.check_steam_nicknames.is_running():
            self.check_steam_nicknames.start()

    @tasks.loop(minutes=5.0)
    async def check_steam_nicknames(self):
        def fetch_and_update_users():
            name_col, sid_col, hist_col = get_column_names()
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(f"SELECT {name_col}, {sid_col}, {hist_col} FROM users")
            rows = cursor.fetchall()
            
            cursor.execute("SELECT notify_id FROM channels")
            # 디스코드 채널 ID 조회를 위해 int 형변환 보장
            channels = [int(ch[0]) for ch in cursor.fetchall() if ch[0]]
            
            conn.close()
            return rows, channels, (name_col, sid_col, hist_col)

        try:
            rows, notify_channels, cols = await asyncio.to_thread(fetch_and_update_users)
            name_col, sid_col, hist_col = cols
        except Exception as e:
            print(f"[루프 DB 조회 에러] {e}")
            return
        
        if not rows: 
            return
            
        ids = [row[1] for row in rows]
        players = await get_steam_users_info(ids)
        p_dict = {p['steamid']: p for p in players}
        
        for name_key, sid, history_str in rows:
            history = history_str.split(" | ") if history_str else []
            player = p_dict.get(sid)
            
            curr_nick = (player.get('personaname') if player and player.get('communityvisibilitystate') == 3 
                         else await get_nickname_from_xml(sid))
            
            if not curr_nick or curr_nick.strip() == "": 
                continue
            if history and curr_nick == history[-1]: 
                continue
            # 스팀 API 오동작 등으로 이전 닉네임이 임시 조회되었을 때 알림 폭탄 방지
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
                
            try:
                await asyncio.to_thread(update_db, new_history_str, name_key)
            except Exception as e:
                print(f"[루프 DB 업데이트 에러] {e}")
                continue
            
            # 알림 메시지 생성 및 전송
            is_private = player.get('communityvisibilitystate') != 3 if player else True
            embed = create_status_embed(name_key, sid, history, "notify", player, is_private)
            
            for ch_id in notify_channels:
                try:
                    c = self.get_channel(ch_id) or await self.fetch_channel(ch_id)
                    if c: 
                        await c.send(embed=embed)
                except discord.NotFound:
                    print(f"[채널 에러] 존재하지 않는 채널 ID: {ch_id}")
                except discord.Forbidden:
                    print(f"[권한 에러] 채널 {ch_id}에 메시지를 보낼 권한이 없습니다.")
                except Exception as e:
                    print(f"[알림 전송 실패] {e}")

bot = MyBot()

# --- [5. 명령어 구현] ---
@bot.tree.command(name="추가", description="유저 추가 (별명 생략 시 스팀 닉네임으로 자동 등록)")
async def add_user(i: discord.Interaction, steam_id: str, nickname: str = None):
    await i.response.defer()
    
    name_col, sid_col, hist_col = await asyncio.to_thread(get_column_names)
    
    def check_existing_user():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {name_col}, {sid_col}, {hist_col} FROM users WHERE {sid_col} = ?", (steam_id,))
        row = cursor.fetchone()
        conn.close()
        return row

    exist_sid_row = await asyncio.to_thread(check_existing_user)
    
    if exist_sid_row:
        exist_name, exist_sid, exist_hist = exist_sid_row
        history_list = exist_hist.split(" | ") if exist_hist else ["없음"]
        players = await get_steam_users_info([exist_sid])
        player = players[0] if players else None
        embed = create_status_embed(exist_name, exist_sid, history_list, "exist", player)
        return await i.followup.send(content="❌ 이미 감시 목록에 등록되어 있는 스팀 ID입니다!", embed=embed)

    # 2) 스팀 프로필 정보 조회
    players = await get_steam_users_info([steam_id])
    player = players[0] if players else None
    curr = (player.get('personaname') if player and player.get('communityvisibilitystate') == 3 
            else await get_nickname_from_xml(steam_id))
    
    if not curr:
        return await i.followup.send("❌ 유효하지 않거나 비공개/정지된 SteamID입니다.")

    final_nickname = nickname.strip() if nickname else curr.strip()

    # 3) 새로 등록하려는 별명 중복 검사
    def check_existing_name():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {name_col}, {sid_col}, {hist_col} FROM users WHERE {name_col} = ?", (final_nickname,))
        row = cursor.fetchone()
        conn.close()
        return row

    exist_name_row = await asyncio.to_thread(check_existing_name)
    if exist_name_row:
        exist_name, exist_sid, exist_hist = exist_name_row
        history_list = exist_hist.split(" | ") if exist_hist else ["없음"]
        embed = create_status_embed(exist_name, exist_sid, history_list, "exist", player)
        return await i.followup.send(content=f"❌ 이미 `{final_nickname}`이라는 별명으로 등록된 다른 유저가 존재합니다!", embed=embed)

    # 4) DB 저장
    def save_user():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"INSERT INTO users ({name_col}, {sid_col}, {hist_col}) VALUES (?, ?, ?)", (final_nickname, steam_id, curr))
        conn.commit()
        conn.close()

    await asyncio.to_thread(save_user)
    await i.followup.send(embed=create_status_embed(final_nickname, steam_id, [curr], "add", player))

@bot.tree.command(name="현황", description="전체 리스트 확인")
async def status_list(i: discord.Interaction):
    await i.response.defer() 
    name_col, sid_col, hist_col = await asyncio.to_thread(get_column_names)
    
    def fetch_all_users():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {name_col}, {sid_col}, {hist_col} FROM users")
        rows = cursor.fetchall()
        conn.close()
        return rows

    rows = await asyncio.to_thread(fetch_all_users)

    if not rows: 
        return await i.followup.send("📊 감시 유저가 없습니다.")
    
    pages = []
    current_page = "📊 **감시 현황**\n```text\n별명 / 현재닉네임 / SteamID\n"
    
    for name, sid, hist in rows:
        last = hist.split(" | ")[-1] if hist else "없음"
        line = f"{name} / {last} / {sid}\n"
        if len(current_page + line) > 1900:
            pages.append(current_page + "```")
            current_page = "
```text\n" + line
        else:
            current_page += line
    pages.append(current_page + "```") # 오타 수정 완료

    await i.followup.send(pages[0])
    for page in pages[1:]:
        await i.followup.send(page)

@bot.tree.command(name="내역", description="특정 유저의 변경 내역 확인 (별명 또는 스팀아이디 입력 가능)")
async def user_history(i: discord.Interaction, target: str):
    await i.response.defer()
    name_col, sid_col, hist_col = await asyncio.to_thread(get_column_names)
    
    def fetch_target_user():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {name_col}, {sid_col}, {hist_col} FROM users WHERE {name_col} = ? OR {sid_col} = ?", (target.strip(), target.strip()))
        row = cursor.fetchone()
        conn.close()
        return row

    row = await asyncio.to_thread(fetch_target_user)

    if not row: 
        return await i.followup.send(f"❌ `{target}`에 해당하는 유저를 감시 목록에서 찾을 수 없습니다.")
    
    name, sid, hist_str = row
    history = hist_str.split(" | ") if hist_str else []
    players = await get_steam_users_info([sid])
    player = players[0] if players else None
    
    await i.followup.send(embed=create_status_embed(name, sid, history, "history", player))

@bot.tree.command(name="삭제", description="유저 삭제 (별명 또는 스팀아이디 입력 가능)")
async def delete_user(i: discord.Interaction, target: str):
    await i.response.defer()
    name_col, sid_col, hist_col = await asyncio.to_thread(get_column_names)
    
    def remove_user():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM users WHERE {name_col} = ? OR {sid_col} = ?", (target.strip(), target.strip()))
        count = cursor.rowcount
        if count > 0:
            conn.commit()
        conn.close()
        return count

    row_count = await asyncio.to_thread(remove_user)
    if row_count > 0:
        await i.followup.send(f"✅ `{target}` 삭제 완료")
    else:
        await i.followup.send("❌ 찾을 수 없습니다.")

@bot.tree.command(name="채널설정", description="채널 설정")
@app_commands.choices(역할=[app_commands.Choice(name="관리", value="admin"), app_commands.Choice(name="알림", value="notify")])
async def set_channel(i: discord.Interaction, 역할: str):
    if not i.user.guild_permissions.administrator: 
        return await i.response.send_message("❌ 권한 없음", ephemeral=True)
        
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
    if not TOKEN:
        print("Error: DISCORD_TOKEN 환경변수가 설정되지 않았습니다.")
    else:
        bot.run(TOKEN)
