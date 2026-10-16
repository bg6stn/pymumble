#!/usr/bin/env python3
# 播放机器人（流媒体 + 本地音乐 + 播放列表管理）
# 所有命令以 / 开头
import pymumble_py3
import subprocess
import time
import threading
import queue
import os
import random
import glob
import re
import json
import urllib.request
from pymumble_py3.callbacks import (
    PYMUMBLE_CLBK_TEXTMESSAGERECEIVED as PTX,
)

# ===== 配置 =====
SERVER = "127.0.0.1"
PORT = 64738
PASSWORD = ""
NICK = "Radio-Bot"
MUSIC_DIR = "/root/music"
MIC_DEVICE = "plughw:1,0"
STATIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stations.json")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

RATE = 48000
CHANNELS = 1
CHUNK_MS = 10
CHUNK_SIZE = int(RATE * 2 * CHUNK_MS / 1000)

# ===== 内置央广电台（URL路径，播放时自动刷新签名）=====
CNR_BUILTIN = [
    ("zgzs", "中国之声", "https://ngcdn001.cnr.cn/live/zgzs/index.m3u8"),
    ("jjzs", "经济之声", "https://ngcdn002.cnr.cn/live/jjzs/index.m3u8"),
    ("yyzs", "音乐之声", "https://ngcdn003.cnr.cn/live/yyzs/index.m3u8"),
    ("dszs", "经典音乐广播", "https://ngcdn004.cnr.cn/live/dszs/index.m3u8"),
    ("zhzs", "台海之声", "https://ngcdn005.cnr.cn/live/zhzs/index.m3u8"),
    ("szzs", "神州之声", "https://ngcdn006.cnr.cn/live/szzs/index.m3u8"),
    ("hxzs", "大湾区之声", "https://ngcdn007.cnr.cn/live/hxzs/index.m3u8"),
    ("xgzs", "香港之声", "https://ngcdn008.cnr.cn/live/xgzs/index.m3u8"),
    ("mzzs", "民族之声", "https://ngcdn009.cnr.cn/live/mzzs/index.m3u8"),
    ("wyzs", "文艺之声", "https://ngcdn010.cnr.cn/live/wyzs/index.m3u8"),
    ("lnzs", "老年之声", "https://ngcdn011.cnr.cn/live/lnzs/index.m3u8"),
    ("zygb", "藏语广播", "https://ngcdn012.cnr.cn/live/zygb/index.m3u8"),
    ("wygb", "维语广播", "https://ngcdn013.cnr.cn/live/wygb/index.m3u8"),
    ("gsgljtgb", "中国交通广播", "https://ngcdn016.cnr.cn/live/gsgljtgb/index.m3u8"),
    ("xczs", "中国乡村之声", "https://ngcdn017.cnr.cn/live/xczs/index.m3u8"),
    ("hygb", "哈语广播", "https://ngcdn025.cnr.cn/live/hygb/index.m3u8"),
]

DEFAULT_STATIONS = {}
for i, (code, name, url) in enumerate(CNR_BUILTIN, 1):
    DEFAULT_STATIONS[str(i)] = {"name": name, "url": url}


def refresh_cnr_urls():
    """从央广API获取带签名的最新URL，返回 {路径: url}"""
    try:
        req = urllib.request.Request(
            "https://apppc.cnr.cn/national",
            data=b'{}',
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        result = {}
        for cat in data.get("data", {}).get("categories", []):
            for item in cat.get("detail", []):
                for s in item.get("other_info11", []):
                    url = s.get("url", "")
                    m = re.search(r'/live/([a-z0-9]+)/', url)
                    if m:
                        result[m.group(1)] = url
        return result
    except Exception:
        return {}


def load_stations():
    if os.path.exists(STATIONS_FILE):
        try:
            with open(STATIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(DEFAULT_STATIONS)


def save_stations():
    try:
        with open(STATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(STATIONS, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


STATIONS = load_stations()

# ===== 状态 =====
mode = "idle"
current_station = "1"
play_mode = "seq"
music_list = []
music_index = 0
current_proc = None
mic_proc = None
audio_queue = queue.Queue(maxsize=300)
playing = False
listening = False


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "mode": mode,
                "current_station": current_station,
                "play_mode": play_mode,
                "music_index": music_index,
                "last_music": music_list[music_index] if mode == "music" and 0 <= music_index < len(music_list) else None,
            }, f, ensure_ascii=False)
    except Exception:
        pass


def load_state():
    global mode, current_station, play_mode, music_index
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            mode = s.get("mode", "idle")
            current_station = s.get("current_station", "1")
            play_mode = s.get("play_mode", "seq")
            music_index = s.get("music_index", 0)
        except Exception:
            pass


def strip_html(s):
    """去掉Mumble自动加的<a>标签"""
    return re.sub(r'<[^>]+>', '', s).strip()


def scan_music():
    global music_list, music_index
    exts = ("*.mp3", "*.wav", "*.ogg", "*.flac", "*.m4a")
    files = []
    for e in exts:
        files.extend(glob.glob(os.path.join(MUSIC_DIR, e)))
    music_list = sorted(files)
    music_index = 0
    return music_list


def ffmpeg_reader(url):
    global current_proc
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-i", url,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(RATE), "-ac", str(CHANNELS), "-"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=CHUNK_SIZE)
    current_proc = proc
    while True:
        if proc.poll() is not None:
            break
        data = proc.stdout.read(CHUNK_SIZE)
        if not data:
            break
        try:
            audio_queue.put(data, timeout=1)
        except queue.Full:
            try:
                audio_queue.get_nowait()
                audio_queue.put(data)
            except queue.Empty:
                pass


def mic_listener(mumble):
    """从声卡录制mic，直接播放到Mumble频道"""
    global mic_proc
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-f", "alsa",
        "-ac", str(CHANNELS),
        "-ar", str(RATE),
        "-i", MIC_DEVICE,
        "-f", "s16le", "-acodec", "pcm_s16le", "-"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=CHUNK_SIZE)
    mic_proc = proc
    while True:
        if proc.poll() is not None:
            break
        data = proc.stdout.read(CHUNK_SIZE)
        if not data:
            break
        mumble.sound_output.add_sound(data)


def stop_mic():
    global mic_proc, listening
    listening = False
    if mic_proc:
        try:
            mic_proc.terminate()
        except Exception:
            pass
        mic_proc = None


def player(mumble):
    time.sleep(1.0)
    while True:
        if playing:
            try:
                data = audio_queue.get(timeout=1)
                mumble.sound_output.add_sound(data)
                time.sleep(CHUNK_MS / 1000.0)
            except queue.Empty:
                time.sleep(0.05)
        else:
            time.sleep(0.1)


def stop_all():
    global playing, current_proc, mode
    playing = False
    mode = "idle"
    if current_proc:
        try:
            current_proc.terminate()
        except Exception:
            pass
        current_proc = None
    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break
    save_state()


def start_station(mumble, sid):
    global current_station, mode, playing
    stop_all()
    current_station = sid
    mode = "radio"
    playing = True
    url = STATIONS[sid]["url"]
    m = re.search(r'/live/([a-z0-9]+)/', url)
    if m:
        fresh = refresh_cnr_urls()
        if m.group(1) in fresh:
            url = fresh[m.group(1)]
    threading.Thread(target=ffmpeg_reader, args=(url,), daemon=True).start()
    save_state()
    return "播放电台: /p%s %s" % (sid, STATIONS[sid]["name"])


def play_next_song(mumble):
    global music_index, mode, playing
    if not music_list:
        return "播放列表为空，用 /s 扫描音乐目录"

    if play_mode == "rep":
        pass
    elif play_mode == "rand":
        music_index = random.randint(0, len(music_list) - 1)
    else:
        music_index = (music_index + 1) % len(music_list)

    song = music_list[music_index]
    name = os.path.basename(song)
    stop_all()
    mode = "music"
    playing = True
    threading.Thread(target=ffmpeg_reader, args=(song,), daemon=True).start()
    save_state()
    return "播放[%d/%d]: %s" % (music_index + 1, len(music_list), name)


def start_music(mumble):
    if not music_list:
        scan_music()
    if not music_list:
        return "本地音乐目录无歌曲: " + MUSIC_DIR
    return play_next_song(mumble)


def parse_cmd(text):
    t = strip_html(text)
    if not t.startswith("/"):
        return None
    t = t[1:].strip()
    if t.startswith("p") and len(t) >= 2 and t[1:].isdigit():
        return ["p", t[1:]]
    return t.split()


def on_text_received(message):
    global play_mode, music_index, STATIONS, listening
    text = strip_html(message.message).strip()
    sender = message.actor
    user = mumble.users[sender]["name"] if sender in mumble.users else "未知"

    parts = parse_cmd(text)
    if not parts:
        return

    cmd = parts[0].lower()
    reply = ""

    if cmd == "p":
        if len(parts) >= 2 and parts[1] in STATIONS:
            reply = start_station(mumble, parts[1])
        elif mode == "music":
            reply = play_next_song(mumble)
        elif mode == "radio":
            s = STATIONS.get(current_station, {})
            reply = "当前: /p%s %s\nURL: %s" % (current_station, s.get("name", "?"), s.get("url", "?"))
        else:
            reply = start_station(mumble, current_station)

    elif cmd == "t":
        stop_all()
        stop_mic()
        reply = "已停止"

    elif cmd in ("jt",):
        if listening:
            stop_mic()
            reply = "退出监听，恢复播放"
            start_station(mumble, current_station)
        else:
            stop_all()
            stop_mic()
            listening = True
            threading.Thread(target=mic_listener, args=(mumble,), daemon=True).start()
            reply = "话筒监听中 (%s)，再发 /jt 退出" % MIC_DEVICE

    elif cmd in ("local", "lo"):
        reply = start_music(mumble)

    elif cmd in ("next", "n"):
        if mode == "music":
            reply = play_next_song(mumble)
        elif mode == "radio":
            keys = list(STATIONS.keys())
            if len(keys) <= 1:
                reply = "只有一个电台"
            else:
                idx = keys.index(current_station) if current_station in keys else 0
                next_sid = keys[(idx + 1) % len(keys)]
                reply = start_station(mumble, next_sid)
        else:
            reply = "当前未播放，发 /lo 或 /p1 开始"

    elif cmd in ("rand", "r"):
        play_mode = "rand"
        reply = "随机播放"

    elif cmd in ("seq", "se"):
        play_mode = "seq"
        reply = "顺序播放"

    elif cmd in ("rep", "re"):
        play_mode = "rep"
        reply = "单曲循环"

    elif cmd in ("scan", "s"):
        n = len(music_list)
        scan_music()
        reply = "重新扫描完成: %d -> %d 首" % (n, len(music_list))

    elif cmd == "rm":
        if len(parts) < 2 or not parts[1].isdigit():
            reply = "用法: /rm 3  (删除列表第3首，不删文件)"
        else:
            idx = int(parts[1]) - 1
            if 0 <= idx < len(music_list):
                name = os.path.basename(music_list.pop(idx))
                if music_index >= idx and music_index > 0:
                    music_index -= 1
                reply = "已删除第%d首: %s" % (idx + 1, name)
            else:
                reply = "序号超出范围，当前共%d首" % len(music_list)

    elif cmd in ("clear", "c"):
        n = len(music_list)
        music_list.clear()
        stop_all()
        reply = "已清空播放列表 (%d首)" % n

    elif cmd in ("list", "ls"):
        lines = ["=== 电台 ==="]
        for k, v in STATIONS.items():
            mark = " (当前)" if k == current_station and mode == "radio" else ""
            lines.append("  /p%s %s%s" % (k, v["name"], mark))
        lines.append("")
        lines.append("=== 本地音乐 (%d首) ===" % len(music_list))
        lines.append("  目录: " + MUSIC_DIR)
        lines.append("  模式: %s" % {"seq": "顺序", "rand": "随机", "rep": "单曲循环"}[play_mode])
        lines.append("")
        if music_list:
            for i, f in enumerate(music_list):
                mark = " ->" if i == music_index else "   "
                lines.append("  %s%d. %s" % (mark, i + 1, os.path.basename(f)))
        reply = "\n".join(lines)

    elif cmd in ("add", "a"):
        if len(parts) < 2:
            reply = "用法: /add 名称 链接  或  /add 链接"
        elif len(parts) == 2:
            url = parts[1]
            name = re.sub(r'^https?://', '', url).split('/')[0]
            new_id = str(len(STATIONS) + 1)
            existing = [v["name"] for v in STATIONS.values()]
            base = name
            i = 2
            while name in existing:
                name = "%s%d" % (base, i)
                i += 1
            STATIONS[new_id] = {"name": name, "url": url}
            save_stations()
            reply = "已添加电台: /p%s %s" % (new_id, name)
        else:
            name = parts[1]
            url = parts[2]
            new_id = str(len(STATIONS) + 1)
            existing = [v["name"] for v in STATIONS.values()]
            base = name
            i = 2
            while name in existing:
                name = "%s%d" % (base, i)
                i += 1
            STATIONS[new_id] = {"name": name, "url": url}
            save_stations()
            reply = "已添加电台: /p%s %s" % (new_id, name)

    elif cmd in ("del", "d"):
        if len(parts) < 2 or parts[1] not in STATIONS:
            reply = "用法: /del 1  (删除指定电台)"
        else:
            sid = parts[1]
            name = STATIONS[sid]["name"]
            del STATIONS[sid]
            save_stations()
            reply = "已删除电台: %s" % name

    elif cmd in ("reset", "rs"):
        stop_all()
        STATIONS = dict(DEFAULT_STATIONS)
        save_stations()
        reply = "已恢复默认央广电台 (%d个)" % len(STATIONS)

    elif cmd in ("help", "h"):
        reply = (
            "=== 命令 (都以/开头) ===\n"
            "/p            播放/继续/查看当前\n"
            "/t            停止\n"
            "/jt           话筒监听/退出监听\n"
            "/p1 /p2       切换电台\n"
            "/lo /local    播放本地音乐\n"
            "/n /next      下一首/下一电台\n"
            "/r /rand      随机播放\n"
            "/se /seq      顺序播放\n"
            "/re /rep      单曲循环\n"
            "/s /scan      重新扫描音乐目录\n"
            "/rm 3         删除列表第3首(不删文件)\n"
            "/c /clear     清空播放列表\n"
            "/add 名称 url  添加电台\n"
            "/add url      自动识别名称\n"
            "/del 1        删除电台\n"
            "/rs /reset    恢复默认央广电台\n"
            "/ls /list     查看电台和歌曲\n"
            "/h /help      本帮助"
        )
    else:
        return

    try:
        mumble.users[sender].send_text_message(reply.replace("\n", "<br/>"))
    except Exception:
        pass
    print("[%s] %s" % (user, text))
    for line in reply.split("\n"):
        print("    %s" % line)


if __name__ == "__main__":
    load_state()
    scan_music()
    print("本地音乐目录: %s, 共%d首" % (MUSIC_DIR, len(music_list)))

    mumble = pymumble_py3.Mumble(SERVER, NICK, port=PORT, password=PASSWORD)
    mumble.callbacks.set_callback(PTX, on_text_received)
    mumble.start()
    mumble.is_ready()

    print("Radio Bot 已连接 %s:%d" % (SERVER, PORT))
    print("命令: /p /t /p1 /lo /n /r /se /re /s /rm /c /add /del /ls /h")

    threading.Thread(target=player, args=(mumble,), daemon=True).start()

    # 恢复上次播放
    if mode == "radio" and current_station in STATIONS:
        time.sleep(1)
        print("恢复播放电台: /p%s %s" % (current_station, STATIONS[current_station]["name"]))
        start_station(mumble, current_station)
    elif mode == "music" and music_list:
        time.sleep(1)
        print("恢复播放本地音乐: [%d/%d]" % (music_index + 1, len(music_list)))
        play_next_song(mumble)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("退出")
        save_state()
