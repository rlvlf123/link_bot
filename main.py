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
        decoded_data = content.decoded_content.decode('utf-8')
        data = json.loads(decoded_data)
        # 채널 구조 호환성 확인
        if 'channels' not in data: data['channels'] = {}
        if data['channels'] and not isinstance(list(data['channels'].values())[0], dict):
            data['channels'] = {}
        return data
    except:
        print("⚠️ 데이터를 불러올 수 없어 기본 구조로 시작합니다.")
        return default_structure

def save_data(data, message="Update tracked data"):
    try:
        new_content = json.dumps(data, indent=4, ensure_ascii=False)
        try:
            content = repo.get_contents(DATA_FILE)
            repo.update_file(content.path, message, new_content, content.sha)
        except:
            repo.create_file(DATA_FILE, "Initial data create", new_content)
        print(f"✅ 데이터 저장 완료: {message}")
    except Exception as e:
        print(f"❌ 저장 실패: {e}")

db = load_data()

# --- [3. 유틸리티] ---
async def get_steam_user_info(steam_id):
    url = f"http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={steam_id}"
    try:
        res = await asyncio.to_thread(requests.get, url, timeout=10)
        if res.status_code == 200:
            players = res.json().get('response', {}).get('players', [])
            return players[0] if players else None
    except: return None

def get_status_display(player):
    if player.get('communityvisibilitystate') != 3: return "🔒 비공개 프로필", ""
    status_map = {0: "🔴 오프라인", 1: "🟢 온라인", 2: "⛔ 바쁨", 3: "🌙 자리비움", 4: "💤 취침 중"}
    status_text = status_map.get(player.get('personastate', 0), "❓ 정보 없음")
    game_info = f"\n🕹️ **플레이 중:** {player['gameextrainfo']}" if 'gameextrainfo' in player else ""
    return status_text, game_info

def crawl_steam_history(steam_id):
    url = f"https://steamcommunity.com/profiles/{steam_id}/ajaxaliases"
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        if res.status_code == 200: return [item['newname'] for item in res.json()][::-1]
    except: pass
    return []

def format_history_message(display_name, steam_id, history, mode="notify", player_info=None):
    history_chain = " → ".join(history)
    current_nick = history[-1] if history else "정보 없음"
    now = datetime.now().strftime('%Y.%m.%d %H:%M:%S')
    titles = {"history": "📋 정보 조회", "add": "✨ 감시 시작", "notify": "🔔 닉네임 변경 알림!"}
    header = titles.get(mode, "알림")
    status_str = ""
    if player_info:
        status, game = get_status_display(player_info)
        status_str = f"현재 상태: **{status}**{game}\n"
    msg = (f"**{header}**\n\n대상: **{display_name}**\n{status_str}"
           f"이전 기록: {history_chain}\n현재 닉네임: **{current_nick}**\n\n"
           f"프로필: https://steamcommunity.com/profiles/{steam_id}\n• 시각: {now}")
    if len(msg) > 1990: msg = f"**{header}**\n\n...(중략)...\n→ " + msg[-1800:]
    return msg

async def is_admin_channel(i: discord.Interaction):
    gid = str(i.guild_id)
    admin_ch_id = db['channels'].get(gid, {}).get('admin')
    if not admin_ch_id or i.channel_id != admin_ch_id:
        await i.response.send_message("❌ 이 명령어는 **관리 전용 채널**에서만 사용할 수 있습니다.\n먼저 `/채널설정`으로 관리 채널을 지정해주세요.", ephemeral=True)
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
        print(f"🤖 봇 준비 완료: {self.user}")

    @tasks.loop(minutes=2.0)
    async def check_steam_nicknames(self):
        if not db['users']: return
        changed = False
        for key, data in list(db['users'].items()):
            sid = data['steam_id']
            player = await get_steam_user_info(sid)
            if not player: continue
            curr_nick = player['personaname']
            history = data.get('history', [])
            if not history or curr_nick != history[-1]:
                history.append(curr_nick)
                db['users'][key]['history'] = history
                changed = True
                msg = format_history_message(key, sid, history, mode="notify", player_info=player)
                for gid in db['channels']:
                    notify_id = db['channels'][gid].get('notify')
                    if notify_id:
                        try:
                            channel = self.get_channel(int(notify_id)) or await self.fetch_channel(int(notify_id))
                            if channel: await channel.send(msg)
                        except: continue
            await asyncio.sleep(1) # API 과부하 방지
        if changed: save_data(db, "Auto Update: Nickname change detected")

bot = MyBot()

# --- [5. 슬래시 명령어] ---
@bot.tree.command(name="도움말", description="주요 명령어 안내")
async def help_command(i: discord.Interaction):
    if not await is_admin_channel(i): return
    embed = discord.Embed(title="🎮 스팀 감시 봇 명령어 가이드", color=discord.Color.blue())
    embed.add_field(name="➕ `/추가 [ID] [별명]`", value="새 유저 등록 (17자리 SteamID)", inline=False)
    embed.add_field(name="📜 `/내역 [대상]`", value="현재 상태 및 이전 닉네임 기록 조회", inline=False)
    embed.add_field(name="📊 `/현황`", value="현재 감시 중인 전체 유저 명단 확인", inline=False)
    embed.add_field(name="❌ `/삭제 [대상]`", value="감시 목록에서 유저 제거", inline=False)
    embed.set_footer(text="알림은 설정된 전용 채널로 자동 전송됩니다.")
    await i.response.send_message(embed=embed)

@bot.tree.command(name="채널설정", description="채널 역할을 지정합니다 (관리용/알림용)")
@app_commands.describe(역할="채널의 역할을 선택하세요")
@app_commands.choices(역할=[
    app_commands.Choice(name="관리 및 명령어 채널", value="admin"),
    app_commands.Choice(name="닉네임 변경 알림 채널", value="notify")
])
@app_commands.checks.has_permissions(administrator=True)
async def set_channel(i: discord.Interaction, 역할: str):
    gid = str(i.guild_id)
    if gid not in db['channels']: db['channels'][gid] = {}
    db['channels'][gid][역할] = i.channel_id
    save_data(db, f"Channel Set: {역할} in server {gid}")
    await i.response.send_message(f"✅ 이 채널은 이제 **{역할}** 역할을 수행합니다.")

@bot.tree.command(name="추가", description="감시 유저 추가")
async def add_user(i: discord.Interaction, steam_id: str, nickname: str = None):
    if not await is_admin_channel(i): return
    await i.response.defer()
    player = await get_steam_user_info(steam_id)
    if not player:
        return await i.followup.send("❌ 유저 정보를 찾을 수 없습니다. SteamID가 올바른지 확인해주세요.")
    
    key = nickname or player['personaname']
    history = crawl_steam_history(steam_id)
    if not history or history[-1] != player['personaname']:
        history.append(player['personaname'])
        
    db['users'][key] = {'steam_id': steam_id, 'history': history}
    save_data(db, f"Added User: {key}")
    await i.followup.send(format_history_message(key, steam_id, history, mode="add", player_info=player))

@bot.tree.command(name="내역", description="유저 기록 조회")
async def history_command(i: discord.Interaction, target: str):
    if not await is_admin_channel(i): return
    await i.response.defer()
    found_key = None
    found_data = None
    
    for k, v in db['users'].items():
        if k == target or v['steam_id'] == target:
            found_key, found_data = k, v
            break
            
    if not found_data:
        return await i.followup.send("❌ 등록되지 않은 유저입니다.")
        
    player = await get_steam_user_info(found_data['steam_id'])
    await i.followup.send(format_history_message(found_key, found_data['steam_id'], found_data['history'], mode="history", player_info=player))

@bot.tree.command(name="현황", description="감시 목록 확인")
async def status_list(i: discord.Interaction):
    if not await is_admin_channel(i): return
    if not db['users']:
        return await i.response.send_message("📊 현재 감시 중인 유저가 없습니다.")
    
    user_list = [f"• {k} ({v['steam_id']})" for k, v in db['users'].items()]
    msg = f"📊 **실시간 감시 현황 ({len(db['users'])}명)**\n```text\n" + "\n".join(user_list) + "```"
    await i.response.send_message(msg)

@bot.tree.command(name="삭제", description="유저 삭제")
@app_commands.default_permissions(administrator=True)
async def delete_user(i: discord.Interaction, target: str):
    if not await is_admin_channel(i): return
    
    if target in db['users']:
        del db['users'][target]
        save_data(db, f"Deleted User: {target}")
        await i.response.send_message(f"✅ `{target}` 유저를 삭제했습니다.")
    else:
        # ID로 검색해서 삭제 시도
        target_key = next((k for k, v in db['users'].items() if v['steam_id'] == target), None)
        if target_key:
            del db['users'][target_key]
            save_data(db, f"Deleted User: {target_key}")
            await i.response.send_message(f"✅ `{target_key}` 유저를 삭제했습니다.")
        else:
            await i.response.send_message("❌ 목록에서 해당 별명이나 ID를 찾을 수 없습니다.")

if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ 오류: DISCORD_TOKEN 환경 변수가 설정되지 않았습니다.")
