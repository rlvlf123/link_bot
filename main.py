import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
import json
import os
import asyncio
from datetime import datetime
from github import Github  # pip install PyGithub 필요

# --- [1. 설정 정보] ---
TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
GH_TOKEN = os.getenv('GH_TOKEN')    # GitHub Personal Access Token
GH_REPO = os.getenv('GH_REPO')      # "계정명/리포지토리명"
DATA_FILE = os.getenv('DATA_FILE', 'tracked_users.json')

# GitHub API 초기화
try:
    g = Github(GH_TOKEN)
    repo = g.get_repo(GH_REPO)
except Exception as e:
    print(f"❌ GitHub 리포지토리 연결 실패: {e}")

# --- [2. GitHub 데이터 동기화 함수] ---
def load_data():
    """GitHub에서 데이터를 불러옵니다. 실패 시 기본값을 반환합니다."""
    default_structure = {'users': {}, 'channels': {}}
    try:
        content = repo.get_contents(DATA_FILE)
        decoded_data = content.decoded_content.decode('utf-8')
        print("✅ GitHub에서 데이터를 성공적으로 불러왔습니다.")
        return json.loads(decoded_data)
    except Exception as e:
        print(f"⚠️ GitHub 데이터 로드 실패 (새 파일로 시작합니다): {e}")
        return default_structure

def save_data(data, message="Update tracked data"):
    """GitHub에 데이터를 업로드(커밋)합니다."""
    try:
        new_content = json.dumps(data, indent=4, ensure_ascii=False)
        try:
            content = repo.get_contents(DATA_FILE)
            repo.update_file(content.path, message, new_content, content.sha)
        except:
            repo.create_file(DATA_FILE, "Initial data create", new_content)
        print(f"✅ GitHub 동기화 완료: {message}")
    except Exception as e:
        print(f"❌ GitHub 저장 실패: {e}")

# 초기 데이터 로드
db = load_data()

# --- [3. 스팀 API 및 유틸리티] ---
async def get_steam_user_info(steam_id):
    url = f"http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={steam_id}"
    try:
        res = await asyncio.to_thread(requests.get, url, timeout=10)
        if res.status_code == 200:
            players = res.json().get('response', {}).get('players', [])
            return players[0] if players else None
    except: return None
    return None

def get_status_display(player):
    if player.get('communityvisibilitystate') != 3:
        return "🔒 비공개 프로필", ""
    status_map = {0: "🔴 오프라인", 1: "🟢 온라인", 2: "⛔ 바쁨", 3: "🌙 자리비움", 4: "💤 취침 중"}
    status_text = status_map.get(player.get('personastate', 0), "❓ 정보 없음")
    game_info = f"\n🕹️ **플레이 중:** {player['gameextrainfo']}" if 'gameextrainfo' in player else ""
    return status_text, game_info

def crawl_steam_history(steam_id):
    url = f"https://steamcommunity.com/profiles/{steam_id}/ajaxaliases"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            return [item['newname'] for item in res.json()][::-1]
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
           f"이전 닉네임: {history_chain}\n현재 닉네임: **{current_nick}**\n\n"
           f"프로필: https://steamcommunity.com/profiles/{steam_id}\n• 시각: {now}")
    if len(msg) > 1990: msg = f"**{header}**\n\n...(생략)...\n→ " + msg[-1800:]
    return msg

# --- [4. 봇 클래스] ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        if not self.check_steam_nicknames.is_running():
            self.check_steam_nicknames.start()
        await self.tree.sync()
        
        print("\n" + "="*50)
        print(f"🚀 스팀 감시 봇 로그인이 완료되었습니다!")
        print(f"🤖 봇 이름: {self.user.name} ({self.user.id})")
        print(f"📊 감시 중인 유저 수: {len(db['users'])}명")
        print(f"📢 알림 채널 수: {len(db['channels'])}개")
        print("="*50 + "\n")

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
                for gid, ch_id in list(db['channels'].items()):
                    try:
                        channel = self.get_channel(int(ch_id)) or await self.fetch_channel(int(ch_id))
                        if channel: await channel.send(msg)
                    except: continue
            await asyncio.sleep(1)
        if changed: save_data(db, "Auto Update: Nickname change detected")

bot = MyBot()

# --- [5. 슬래시 명령어] ---
@bot.tree.command(name="도움말", description="명령어 안내")
async def help_command(i: discord.Interaction):
    embed = discord.Embed(title="🎮 스팀 감시 봇 가이드", color=discord.Color.blue())
    embed.add_field(name="📢 `/채널설정`", value="알림 받을 채널로 지정 (관리자용)", inline=False)
    embed.add_field(name="➕ `/추가 [ID] [별명]`", value="유저 등록 (17자리 SteamID)", inline=False)
    embed.add_field(name="📜 `/내역 [별명/ID]`", value="상태 및 기록 조회", inline=False)
    embed.add_field(name="📊 `/현황`", value="전체 감시 목록 확인", inline=False)
    embed.add_field(name="❌ `/삭제 [별명/ID]`", value="감시 중단 (관리자용)", inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name="채널설정", description="현재 채널을 알림 수신지로 지정")
@app_commands.checks.has_permissions(administrator=True)
async def set_channel(i: discord.Interaction):
    db['channels'][str(i.guild_id)] = i.channel_id
    save_data(db, f"Channel Set: {i.channel.name}")
    await i.response.send_message(f"📢 **설정 완료!** 이제 이 채널로 알림이 전송됩니다.")

@bot.tree.command(name="추가", description="감시할 스팀 유저 추가")
async def add_user(i: discord.Interaction, steam_id: str, nickname: str = None):
    await i.response.defer()
    key = nickname or steam_id
    player = await get_steam_user_info(steam_id)
    if not player: return await i.followup.send("❌ 유저 정보를 가져올 수 없습니다.")
    
    history = crawl_steam_history(steam_id)
    curr_name = player['personaname']
    if not history or history[-1] != curr_name: history.append(curr_name)
    
    db['users'][key] = {'steam_id': steam_id, 'history': history}
    save_data(db, f"User Added: {key}")
    await i.followup.send(format_history_message(key, steam_id, history, mode="add", player_info=player))

@bot.tree.command(name="내역", description="유저 상태 및 기록 조회")
async def history_command(i: discord.Interaction, target: str):
    await i.response.defer()
    found_key, found_data = None, None
    for key, data in db['users'].items():
        if key == target or data['steam_id'] == target:
            found_key, found_data = key, data
            break
    if not found_data: return await i.followup.send("❌ 유저를 찾을 수 없습니다.")
    player = await get_steam_user_info(found_data['steam_id'])
    await i.followup.send(format_history_message(found_key, found_data['steam_id'], found_data['history'], mode="history", player_info=player))

@bot.tree.command(name="현황", description="감시 목록 확인")
async def status_list(i: discord.Interaction):
    if not db['users']: return await i.response.send_message("📊 감시 중인 유저가 없습니다.")
    msg = f"📊 **실시간 감시 현황 ({len(db['users'])}명)**\n```text\n"
    for key, data in db['users'].items():
        msg += f"• {key} ({data['steam_id']})\n"
    await i.response.send_message(msg + "```")

@bot.tree.command(name="삭제", description="유저 삭제 (관리자용)")
@app_commands.default_permissions(administrator=True)
async def delete_user(i: discord.Interaction, target: str):
    if target in db['users']:
        del db['users'][target]
        save_data(db, f"User Deleted: {target}")
        await i.response.send_message(f"✅ `{target}` 유저를 삭제했습니다.")
    else:
        await i.response.send_message("❌ 찾을 수 없습니다.")

if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ 오류: DISCORD_TOKEN 환경 변수가 설정되지 않았습니다.")
