import discord
from discord import app_commands
from discord.ext import commands, tasks
import requests
import json
import os
import asyncio
import xml.etree.ElementTree as ET
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

# --- [3. 유틸리티 & XML 파싱] ---
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
    """비공개 계정의 현재 닉네임을 XML 프로필 페이지에서 강제로 추출합니다."""
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
    colors = {"add": discord.Color.green(), "notify": discord.Color.gold(), "history": discord.Color.blue()}
    titles = {"add": "✨ 새 감시 대상 추가", "notify": "🔔 닉네임 변경 알림", "history": "📋 유저 정보 조회"}
    
    display_title = display_name if display_name else "별명없음"
    embed = discord.Embed(title=titles.get(mode, "알림"), color=colors.get(mode, discord.Color.light_grey()))
    
    if player:
        embed.set_thumbnail(url=player.get('avatarfull'))
        status_map = {0: "🔴 오프라인", 1: "🟢 온라인", 2: "⛔ 바쁨", 3: "🌙 자리비움", 4: "💤 취침 중"}
        state = status_map.get(player.get('personastate', 0), "❓ 정보 없음")
        if is_private: state = "🔒 비공개 계정 (상태 확인 불가)"
        elif 'gameextrainfo' in player: state = f"🕹️ 플레이 중: {player['gameextrainfo']}"
        embed.add_field(name="현재 상태", value=state, inline=False)

    embed.add_field(name="식별 별명", value=display_title, inline=True)
    embed.add_field(name="최신 닉네임", value=history[-1] if history else "없음", inline=True)
    
    all_history_list = history.copy()
    display_history = " → ".join(all_history_list)
    if len(display_history) > 1000:
        while len(" → ".join(all_history_list)) > 980:
            if len(all_history_list) <= 1: break
            all_history_list.pop(0)
        display_history = f"...(생략)... → " + " → ".join(all_history_list)
    
    embed.add_field(name=f"전체 변경 기록 ({len(history)}개)", value=display_history, inline=False)
    embed.add_field(name="스팀 프로필", value=f"[바로가기](https://steamcommunity.com/profiles/{sid})", inline=False)
    
    footer_text = f"ID: {sid} | {datetime.now().strftime('%H:%M:%S')}"
    if is_private: footer_text += " | 🛡️ XML 정밀 추적 중"
    embed.set_footer(text=footer
