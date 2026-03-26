import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
import json
import os
import asyncio
import xml.etree.ElementTree as ET # XML 파싱 위해 추가
from datetime import datetime
from github import Github, Auth # 최신 인증 방식 위해 Auth 추가

# --- [1. 설정 정보] ---
TOKEN = os.getenv('DISCORD_TOKEN')
STEAM_API_KEY = os.getenv('STEAM_API_KEY')
GH_TOKEN = os.getenv('GH_TOKEN')
GH_REPO = os.getenv('GH_REPO')
DATA_FILE = os.getenv('DATA_FILE', 'tracked_users.json')

# GitHub 연결 (최신 Auth 방식 적용 및 예외 처리 강화)
repo = None
try:
    if GH_TOKEN and GH_REPO:
        auth = Auth.Token(GH_TOKEN)
        g = Github(auth=auth)
        repo = g.get_repo(GH_REPO)
        print("✅ GitHub 리포지토리 연결 성공!")
    else:
        print("⚠️ GitHub 설정이 없거나 불완전합니다. 로컬 모드로 동작할 수 있습니다.")
except Exception as e:
    print(f"❌ GitHub 리포지토리 연결 실패: {e}")

# --- [2. 데이터 함수] ---
def load_data():
    default_structure = {'users': {}, 'channels': {}}
    if not repo: return default_structure # repo가 없으면 로컬 모드처럼 동작
    try:
        content = repo.get_contents(DATA_FILE)
        data = json.loads(content.decoded_content.decode('utf-8'))
        # 필수 키 존재 확인
        if 'channels' not in data: data['channels'] = {}
        if 'users' not in data: data['users'] = {}
        return data
    except Exception as e:
        # 파일이 없거나 읽기 실패 시 기본 구조 반환
        print(f"⚠️ 데이터 로드 실패 (파일이 없거나 읽을 수 없음): {e}")
        return default_structure

def save_data(data, message="Update tracked data"):
    if not repo: return # repo가 없으면 로컬 저장을 시도하지 않음 (이 봇은 GitHub 전용 저장 구조)
    try:
        new_content = json.dumps(data, indent=4, ensure_ascii=False)
        content = repo.get_contents(DATA_FILE)
        repo.update_file(content.path, message, new_content, content.sha)
        print(f"✅ GitHub 데이터 저장 성공: {message}")
    except Exception as e:
        try:
            # 파일이 없을 경우 새로 생성
            new_content = json.dumps(data, indent=4, ensure_ascii=False)
            repo.create_file(DATA_FILE, "Initial data create", new_content)
            print("✅ GitHub 데이터 파일 생성 성공!")
        except Exception as ce:
            print(f"❌ GitHub 저장 치명적 오류: {ce}")

# 초기 데이터 로드
db = load_data()

# --- [3. 유틸리티 & 비공개 계정 처리] ---
async def get_steam_users_info(steam_ids):
    if not steam_ids: return []
    ids_str = ",".join(steam_ids)
    url = f"http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={ids_str}"
    try:
        # 비동기 환경에서 requests 사용을 위해 run_in_executor 사용
        res = await asyncio.to_thread(requests.get, url, timeout=10)
        if res.status_code == 200:
            return res.json().get('response', {}).get('players', [])
    except Exception as e:
        print(f"⚠️ Steam API 호출 오류: {e}")
        return []
    return []

async def get_nickname_from_xml(steam_id):
    """
    [핵심 추가 기능]
    비공개 계정의 현재 닉네임을 XML 프로필 페이지에서 강제로 추출합니다.
    API가 닉네임을 주지 못할 때 사용합니다.
    """
    url = f"https://steamcommunity.com/profiles/{steam_id}/?xml=1"
    try:
        res = await asyncio.to_thread(requests.get, url, timeout=8)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            node = root.find('steamID')
            if node is not None:
                return node.text
    except Exception as e:
        print(f"⚠️ XML 추출 오류 ({steam_id}): {e}")
    return None

def create_status_embed(display_name, sid, history, mode="notify", player=None, is_private=False):
    """
    [개선된 기능]
    임베드 생성 시 비공개 계정 여부를 시각적으로 표시합니다.
    """
    colors = {"add": discord.Color.green(), "notify": discord.Color.gold(), "history": discord.Color.blue()}
    titles = {"add": "✨ 새 감시 대상 추가", "notify": "🔔 닉네임 변경 알림", "history": "📋 유저 정보 조회"}
    
    display_title = display_name if display_name else "별명없음"
    embed = discord.Embed(title=titles.get(mode, "알림"), color=colors.get(mode, discord.Color.light_grey()))
    
    if player:
        embed.set_thumbnail(url=player.get('avatarfull'))
        
        status_map = {0: "🔴 오프라인", 1: "🟢 온라인", 2: "⛔ 바쁨", 3: "🌙 자리비움", 4: "💤 취침 중"}
        state = status_map.get(player.get('personastate', 0), "❓ 정보 없음")
        
        # 비공개 계정 상태 처리 강화
        if is_private:
            state = "🔒 비공개 계정 (상태 확인 불가)"
        elif 'gameextrainfo' in player:
            state = f"🕹️ 플레이 중: {player['gameextrainfo']}"
            
        embed.add_field(name="현재 상태", value=state, inline=False)

    embed.add_field(name="식별 별명", value=display_title, inline=True)
    embed.add_field(name="최신 닉네임", value=history[-1] if history else "없음", inline=True)
    
    # --- [오래된 기록부터 생략 로직] ---
    all_history_list = history.copy()
    display_history = " → ".join(all_history_list)
    
    # 디스코드 필드 제한(1024자)을 넘지 않도록 앞에서부터 제거
    if len(display_history) > 1000:
        while len(" → ".join(all_history_list)) > 980:
            if len(all_history_list) <= 1: break # 최소 하나는 남김
            all_history_list.pop(0)
        display_history = f"...(생략)... → " + " → ".join(all_history_list)
    
    embed.add_field(name=f"전체 변경 기록 ({len(history)}개)", value=display_history, inline=False)
    # ----------------------------------------------

    embed.add_field(name="스팀 프로필", value=f"[바로가기](https://steamcommunity.com/profiles/{sid})", inline=False)
    
    # 푸터 설정 강화
    footer_text = f"ID: {sid} | {datetime.now().strftime('%H:%M:%S')}"
    if is_private:
        footer_text += " | 🛡️ XML 정밀 추적 중"
    embed.set_footer(text=footer_text)
    
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
        print(f"✅ 슬래시 명령어 동기화 완료 및 감시 루프 시작!")

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
                
                # --- [핵심 수정: 비공개 계정 처리 로직] ---
                curr_nick = None
                is_private = False
                
                if player:
                    # API가 정상적으로 닉네임을 준 경우
                    curr_nick = player['personaname']
                    # API가 주는 프로필 상태가 비공개(1)인지 확인 (단, 닉네임은 줄 수도 있음)
                    if player.get('communityvisibilitystate') == 1:
                        is_private = True
                else:
                    # API가 정보를 주지 못함 -> 비공개 계정일 확률 높음 -> XML 시도
                    curr_nick = await get_nickname_from_xml(sid)
                    is_private = True
                    # XML로도 못 가져오면 건너뜀
                    if not curr_nick: continue
                # ----------------------------------------------
                
                history = data.get('history', [])
                
                # 닉네임 변경 감지
                if not history or curr_nick != history[-1]:
                    history.append(curr_nick)
                    db['users'][key]['history'] = history
                    changed = True
                    
                    # 알림 임베드 생성 (is_private 인자 전달)
                    embed = create_status_embed(key, sid, history, mode="notify", player=player, is_private=is_private)
                    
                    # 모든 서버의 알림 채널에 전송
                    for gid, channels in db['channels'].items():
                        if 'notify' in channels:
                            try:
                                chan = self.get_channel(channels['notify']) or await self.fetch_channel(channels['notify'])
                                if chan: await chan.send(embed=embed)
                            except: continue
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
    
    await i.response.defer() # 응답 대기 
    
    rows = ["저장된별명 / 최근닉네임 / 스팀ID", "-" * 45]
    for nickname, data in db['users'].items():
        display_name = nickname if (nickname and nickname.strip()) else "별명없음"
        recent = data['history'][-1] if data.get('history') else "기록 없음"
        rows.append(f"{display_name} / {recent} / {data['steam_id']}")
    
    # 디스코드 메시지 길이 제한(2000자) 처리
    full_text = "📊 **실시간 감시 현황 (GitHub 데이터)**\n```text\n"
    current_chunk = full_text
    
    for row in rows:
        if len(current_chunk) + len(row) + 10 > 2000:
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

    # 중복 확인
    for ex_name, data in db['users'].items():
        if data['steam_id'] == steam_id:
            return await i.followup.send(f"❌ 이미 등록된 ID입니다. (별명: `{ex_name if ex_name else '별명없음'}`)")

    # 유저 정보 가져오기 (비공개 처리 포함)
    players = await get_steam_users_info([steam_id])
    player = players[0] if players else None
    
    curr_nick = None
    is_private = False
    
    if player:
        curr_nick = player['personaname']
        if player.get('communityvisibilitystate') == 1:
            is_private = True
    else:
        # API 실패 시 XML 시도
        curr_nick = await get_nickname_from_xml(steam_id)
        is_private = True
        if not curr_nick: return await i.followup.send("❌ 정보를 찾을 수 없습니다. 올바른 Steam ID인지 확인하세요.")

    # 별명 설정 및 중복 확인
    final_name = nickname or curr_nick
    if final_name in db['users']: return await i.followup.send(f"❌ `{final_name}` 별명은 이미 사용 중입니다.")

    # 스팀 닉네임 히스토리 API 시도 (공개 계정만 작동)
    url = f"https://steamcommunity.com/profiles/{steam_id}/ajaxaliases"
    history = [curr_nick]
    try:
        # 공개 계정인 경우에만 과거 기록 가져오기 시도
        if not is_private:
            res = await asyncio.to_thread(requests.get, url, timeout=5)
            if res.status_code == 200:
                # 과거부터 현재 순으로 정렬
                history = [item['newname']
