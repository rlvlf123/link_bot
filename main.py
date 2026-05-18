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
    url = f"http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={STEAM_API_KEY}&steamids={ids_str}"
    try:
        res = await asyncio.to_thread(requests.get, url, timeout=10)
        if res.status_code == 200:
            return res.json().get('response', {}).get('players', [])
    except Exception as e:
        print(f"[스팀 API 에러] {e}")
    return []

async def get_nickname_from_xml(steam_id):
    url = f"https://steamcommunity.com/profiles/{steam_id}/?xml=1"
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
        status_map = {0: "🔴 오프라인", 1: "🟢 온라인
