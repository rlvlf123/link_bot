import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
import json
import os
import asyncio
from datetime import datetime
from github import Github

# --- [1. 설정 정보] ---
TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
GH_TOKEN = os.getenv('GH_TOKEN')
GH_REPO = os.getenv('GH_REPO')
DATA_FILE = os.getenv('DATA_FILE', 'tracked_users.json')

try:
    g = Github(GH_TOKEN)
    repo = g.get_repo(GH_REPO)
except Exception as e:
    print(f"❌ GitHub 리포지토리 연결 실패: {e}")

# --- [2. 데이터 함수] ---
def load_data():
    default_structure = {'users': {}, 'channels': {}}
    try:
        content = repo.get_contents(DATA_FILE)
        data = json.loads(content.decoded_content.decode('utf-8'))
        if 'channels' not in data: data['channels'] = {}
        if 'users' not in data: data['users'] = {}
        return data
    except:
        return default_structure

def save_data(data, message="Update tracked data"):
    try:
        new_content = json.dumps(data, indent=4, ensure_ascii=False)
        content = repo.get_contents(DATA_FILE)
        repo.update_file(content.path, message, new_content, content.sha)
    except:
        try: repo.create_file(DATA_FILE, "Initial data create", new_content)
        except: print("❌ GitHub 저장 치명적 오류")

db = load_data()

# --- [3. 유틸리티] ---
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

def create_status_embed(display_name, sid, history, mode="notify", player=None):
    colors = {"add": discord.Color.green(), "notify": discord.Color.gold(), "history": discord.Color.blue()}
    titles = {"add": "✨ 새 감시 대상 추가", "notify": "🔔 닉네임 변경 알림", "history": "📋 유저 정보 조회"}
    
    # 별명이 없을 경우 표시 처리
    display_title = display_name if display_name else "별명없음"
    
    embed = discord.Embed(title=titles.get(mode, "알림"), color=colors.get(mode, discord.Color.light_grey()))
    
    if player:
        embed.set_thumbnail(url=player.get('avatarfull'))
        status_map = {0: "🔴 오프라인", 1: "🟢 온라인", 2: "⛔ 바쁨", 3: "🌙 자리비움", 4: "💤 취침 중"}
        state = status_map.get(player.get('personastate', 0), "❓ 정보 없음")
        if 'gameextrainfo' in player: state = f"🕹️ 플레이 중: {player['gameextrainfo']}"
        embed.add_field(name="현재 상태", value=state, inline=False)

    embed.add_field(name="식별 별명", value=display_title, inline=True)
    embed.add_field(name="최신 닉네임", value=history[-1] if history else "없음", inline=True)
    embed.add_field(name="변경 기록(최근 5개)", value=" → ".join(history[-5:]), inline=False)
    embed.add_field(name="스팀 프로필", value=f"[바로가기](https://steamcommunity.com/profiles/{sid})", inline=False)
    embed.set_footer(text=f"ID: {sid} | {datetime.now().strftime('%H:%M:%S')}")
    return embed

async def is_admin_channel(i: discord.Interaction):
    gid = str(i.guild_id)
    admin_ch_id = db['channels'].get(gid, {}).get('admin')
    if not admin_ch_id or i.channel_id != admin_ch_id:
        await i.response.send_message("❌ 관리 전용 채널에서만 사용 가능합니다.", ephemeral=True)
        return False
    return True

# --- [4. 봇 클래스] ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all(), help_command=None)

    async def setup_hook(self):
        if not self.check_steam_nicknames.is_running():
            self.check_steam_nicknames.start()
        await self.tree.sync()

    @tasks.loop(minutes=5.0)
    async def check_steam_nicknames(self):
        if not db['users']: return
        
        steam_ids = [data['steam_id'] for data in db['users'].values()]
        players = await get_steam_users_info(steam_ids)
        player_dict = {p['steamid']: p for p in players}
        
        changed = False
        for key, data in list(db['users'].items()):
            try:
                sid = data['steam_id']
                player = player_dict.get(sid)
                if not player: continue
                
                curr_nick = player['personaname']
                history = data.get('history', [])
                
                if not history or curr_nick != history[-1]:
                    history.append(curr_nick)
                    db['users'][key]['history'] = history
                    changed = True
                    
                    embed = create_status_embed(key, sid, history, mode="notify", player=player)
                    for gid, channels in db['channels'].items():
                        if 'notify' in channels:
                            chan = self.get_channel(channels['notify']) or await self.fetch_channel(channels['notify'])
                            if chan: await chan.send(embed=embed)
            except Exception as e:
                print(f"⚠️ {key} 업데이트 오류: {e}")
                
        if changed: save_data(db, "Auto Update: Nickname changed")
        await self.change_presence(activity=discord.Game(name=f"{len(db['users'])}명 감시 중"))

bot = MyBot()

# --- [5. 슬래시 명령어] ---

@bot.tree.command(name="현황", description="등록된 유저 리스트를 확인합니다.")
async def status_list(i: discord.Interaction):
    if not await is_admin_channel(i): return
    if not db['users']: return await i.response.send_message("📊 감시 중인 유저가 없습니다.")
    
    await i.response.defer() 
    
    rows = ["저장된별명 / 최근닉네임 / 스팀ID", "-" * 45]
    for nickname, data in db['users'].items():
        # [수정된 부분] 별명이 없거나 비어있으면 '별명없음'으로 표시
        display_name = nickname if (nickname and nickname.strip()) else "별명없음"
        recent = data['history'][-1] if data.get('history') else "기록 없음"
        rows.append(f"{display_name} / {recent} / {data['steam_id']}")
    
    full_text = "📊 **실시간 감시 현황 (로컬 데이터)**\n```text\n"
    current_chunk = full_text
    
    for row in rows:
        if len(current_chunk) + len(row) + 5 > 1950:
            current_chunk += "```"
            await i.followup.send(current_chunk)
            current_chunk = "```text\n" + row + "\n"
        else:
            current_chunk += row + "\n"
    
    current_chunk += "```"
    await i.followup.send(current_chunk)

@bot.tree.command(name="추가", description="유저를 추가합니다.")
async def add_user(i: discord.Interaction, steam_id: str, nickname: str = None):
    if not await is_admin_channel(i): return
    await i.response.defer()

    for ex_name, data in db['users'].items():
        if data['steam_id'] == steam_id:
            return await i.followup.send(f"❌ 이미 등록된 ID입니다. (별명: `{ex_name if ex_name else '별명없음'}`)")

    players = await get_steam_users_info([steam_id])
    if not players: return await i.followup.send("❌ 정보를 찾을 수 없습니다.")
    player = players[0]

    final_name = nickname or player['personaname']
    if final_name in db['users']: return await i.followup.send(f"❌ `{final_name}` 별명은 이미 사용 중입니다.")

    url = f"https://steamcommunity.com/profiles/{steam_id}/ajaxaliases"
    history = [player['personaname']]
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            history = [item['newname'] for item in res.json()][::-1]
    except: pass
        
    db['users'][final_name] = {'steam_id': steam_id, 'history': history}
    save_data(db, f"User Added: {final_name}")
    
    embed = create_status_embed(final_name, steam_id, history, mode="add", player=player)
    await i.followup.send(embed=embed)

# (나머지 /내역, /삭제, /채널설정 명령어는 이전과 동일하게 유지됩니다.)
@bot.tree.command(name="내역", description="유저 상세 정보를 조회합니다.")
async def history_command(i: discord.Interaction, target: str):
    if not await is_admin_channel(i): return
    await i.response.defer()
    
    found_key = target if target in db['users'] else next((k for k, v in db['users'].items() if v['steam_id'] == target), None)
    if not found_key: return await i.followup.send("❌ 유저를 찾을 수 없습니다.")
        
    data = db['users'][found_key]
    players = await get_steam_users_info([data['steam_id']])
    player = players[0] if players else None
    
    embed = create_status_embed(found_key, data['steam_id'], data['history'], mode="history", player=player)
    await i.followup.send(embed=embed)

@bot.tree.command(name="삭제", description="유저를 삭제합니다.")
@app_commands.default_permissions(administrator=True)
async def delete_user(i: discord.Interaction, target: str):
    if not await is_admin_channel(i): return
    target_key = target if target in db['users'] else next((k for k, v in db['users'].items() if v['steam_id'] == target), None)

    if target_key:
        del db['users'][target_key]
        save_data(db, f"User Deleted: {target_key}")
        await i.response.send_message(f"✅ `{target_key if target_key else '별명없음'}` 삭제 완료.")
    else: await i.response.send_message("❌ 대상을 찾을 수 없습니다.")

@bot.tree.command(name="채널설정", description="채널 역할을 지정합니다.")
@app_commands.choices(역할=[
    app_commands.Choice(name="관리 채널", value="admin"),
    app_commands.Choice(name="알림 채널", value="notify")
])
async def set_channel(i: discord.Interaction, 역할: str):
    if not i.user.guild_permissions.administrator:
        return await i.response.send_message("❌ 서버 관리자만 가능합니다.", ephemeral=True)
    
    gid = str(i.guild_id)
    if gid not in db['channels']: db['channels'][gid] = {}
    db['channels'][gid][역할] = i.channel_id
    save_data(db, f"Channel Set: {역할}")
    await i.response.send_message(f"✅ 설정 완료: 이 채널은 이제 **{역할}** 역할을 수행합니다.")

if __name__ == "__main__":
    if TOKEN: bot.run(TOKEN)
