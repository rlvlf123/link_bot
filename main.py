import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
import json
import os
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime
from github import Github, Auth

# --- [1. 설정 정보] ---
TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
GH_TOKEN = os.getenv('GH_TOKEN')
GH_REPO = os.getenv('GH_REPO')
DATA_FILE = os.getenv('DATA_FILE', 'tracked_users.json')

# GitHub 연결 (최신 Auth 방식 적용)
repo = None
try:
    if GH_TOKEN and GH_REPO:
        auth = Auth.Token(GH_TOKEN)
        g = Github(auth=auth)
        repo = g.get_repo(GH_REPO)
        print("✅ GitHub 리포지토리 연결 성공!")
    else:
        print("⚠️ GitHub 설정이 불완전합니다. 환경 변수를 확인하세요.")
except Exception as e:
    print(f"❌ GitHub 연결 실패: {e}")

# --- [2. 데이터 함수] ---
def load_data():
    default_structure = {'users': {}, 'channels': {}}
    if not repo: return default_structure
    try:
        content = repo.get_contents(DATA_FILE)
        data = json.loads(content.decoded_content.decode('utf-8'))
        if 'channels' not in data: data['channels'] = {}
        if 'users' not in data: data['users'] = {}
        return data
    except Exception:
        return default_structure

def save_data(data, message="Update tracked data"):
    if not repo: return
    try:
        new_content = json.dumps(data, indent=4, ensure_ascii=False)
        content = repo.get_contents(DATA_FILE)
        repo.update_file(content.path, message, new_content, content.sha)
    except Exception:
        try:
            repo.create_file(DATA_FILE, "Initial data create", json.dumps(data, indent=4, ensure_ascii=False))
        except Exception as e:
            print(f"❌ GitHub 저장 오류: {e}")

# 데이터 로드
db = load_data()

# --- [3. 유틸리티 & 비공개 계정 처리] ---
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
    """비공개 계정용 XML 정밀 추적"""
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
    titles = {"add": "✨ 새 감시 대상 추가", "notify": "🔔 닉네임 변경 알림", "history": "📋 유저 정보 조회"}
    
    embed = discord.Embed(title=titles.get(mode, "알림"), color=colors.get(mode, discord.Color.light_grey()))
    
    if player:
        embed.set_thumbnail(url=player.get('avatarfull'))
        status_map = {0: "🔴 오프라인", 1: "🟢 온라인", 2: "⛔ 바쁨", 3: "🌙 자리비움", 4: "💤 취침 중"}
        state = status_map.get(player.get('personastate', 0), "❓ 정보 없음")
        if is_private: state = "🔒 비공개 계정 (상태 확인 불가)"
        elif 'gameextrainfo' in player: state = f"🕹️ 플레이 중: {player['gameextrainfo']}"
        embed.add_field(name="현재 상태", value=state, inline=False)

    embed.add_field(name="식별 별명", value=display_name or "별명없음", inline=True)
    embed.add_field(name="최신 닉네임", value=history[-1] if history else "없음", inline=True)
    
    all_h = history.copy()
    if len(" → ".join(all_h)) > 1000:
        while len(" → ".join(all_h)) > 980:
            if len(all_h) <= 1: break
            all_h.pop(0)
        display_h = "...(생략)... → " + " → ".join(all_h)
    else:
        display_h = " → ".join(all_h)
    
    embed.add_field(name=f"변경 기록 ({len(history)}개)", value=display_h, inline=False)
    embed.add_field(name="스팀 프로필", value=f"[바로가기](https://steamcommunity.com/profiles/{sid})", inline=False)
    
    footer = f"ID: {sid} | {datetime.now().strftime('%H:%M:%S')}"
    if is_private: footer += " | 🛡️ XML 정밀 추적 중"
    embed.set_footer(text=footer)
    return embed

async def is_admin_channel(i: discord.Interaction):
    gid = str(i.guild_id)
    admin_ch = db['channels'].get(gid, {}).get('admin')
    if not admin_ch or i.channel_id != admin_ch:
        await i.response.send_message("❌ 관리 전용 채널에서만 사용 가능합니다.", ephemeral=True)
        return False
    return True

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
        if not db['users']: return
        ids = [d['steam_id'] for d in db['users'].values()]
        players = await get_steam_users_info(ids)
        p_dict = {p['steamid']: p for p in players}
        
        changed = False
        for key, data in list(db['users'].items()):
            sid = data['steam_id']
            player = p_dict.get(sid)
            is_private = False
            
            if player:
                curr_nick = player['personaname']
                if player.get('communityvisibilitystate') == 1: is_private = True
            else:
                curr_nick = await get_nickname_from_xml(sid)
                is_private = True
                if not curr_nick: continue

            history = data.get('history', [])
            if not history or curr_nick != history[-1]:
                history.append(curr_nick)
                db['users'][key]['history'] = history
                changed = True
                embed = create_status_embed(key, sid, history, "notify", player, is_private)
                for gid, chs in db['channels'].items():
                    if 'notify' in chs:
                        try:
                            c = self.get_channel(chs['notify']) or await self.fetch_channel(chs['notify'])
                            if c: await c.send(embed=embed)
                        except: pass
        if changed: save_data(db, "Auto Update: Nickname changed")

bot = MyBot()

# --- [5. 명령어] ---
@bot.tree.command(name="현황", description="감시 리스트 확인")
async def status_list(i: discord.Interaction):
    if not await is_admin_channel(i): return
    if not db['users']: return await i.response.send_message("📊 비어있음")
    await i.response.defer()
    msg = "📊 **감시 현황**\n```text\n별명 / 현재 / ID\n"
    for k, v in db['users'].items():
        msg += f"{k} / {v['history'][-1]} / {v['steam_id']}\n"
    await i.followup.send(msg + "```")

@bot.tree.command(name="추가", description="유저 추가")
async def add_user(i: discord.Interaction, steam_id: str, nickname: str = None):
    if not await is_admin_channel(i): return
    await i.response.defer()
    
    players = await get_steam_users_info([steam_id])
    player = players[0] if players else None
    is_p = False
    if player:
        curr = player['personaname']
        if player.get('communityvisibilitystate') == 1: is_p = True
    else:
        curr = await get_nickname_from_xml(steam_id)
        is_p = True
        if not curr: return await i.followup.send("❌ ID 오류")

    final_n = nickname or curr
    history = [curr]
    if not is_p:
        try:
            r = await asyncio.to_thread(requests.get, f"https://steamcommunity.com/profiles/{steam_id}/ajaxaliases", timeout=5)
            if r.status_code == 200:
                history = [x['newname'] for x in r.json()][::-1]
                if not history or history[-1] != curr: history.append(curr)
        except: pass

    db['users'][final_n] = {'steam_id': steam_id, 'history': history}
    save_data(db, f"Added: {final_n}")
    await i.followup.send(embed=create_status_embed(final_n, steam_id, history, "add", player, is_p))

@bot.tree.command(name="삭제", description="유저 삭제")
async def delete_user(i: discord.Interaction, target: str):
    if not await is_admin_channel(i): return
    if target in db['users']:
        del db['users'][target]
        save_data(db, f"Deleted: {target}")
        await i.response.send_message(f"✅ `{target}` 삭제됨")
    else: await i.response.send_message("❌ 없음")

@bot.tree.command(name="채널설정", description="채널 설정")
@app_commands.choices(역할=[app_commands.Choice(name="관리", value="admin"), app_commands.Choice(name="알림", value="notify")])
async def set_channel(i: discord.Interaction, 역할: str):
    if not i.user.guild_permissions.administrator: return await i.response.send_message("❌ 권한없음")
    gid = str(i.guild_id)
    if gid not in db['channels']: db['channels'][gid] = {}
    db['channels'][gid][역할] = i.channel_id
    save_data(db, f"Channel: {역할}")
    await i.response.send_message(f"✅ {역할} 채널 설정 완료")

if __name__ == "__main__":
    bot.run(TOKEN)
