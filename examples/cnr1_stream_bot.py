#!/usr/bin/env python3
# 播放机器人（流媒体 + 本地音乐）
# Mumble频道文本命令：
#   p / 播放       播放/继续
#   t / 停止       停止
#   p1 p2 p3      切换电台（p1和p 1都识别）
#   local / 本地   开始播放本地音乐
#   rand / 随机   随机播放
#   seq / 顺序    顺序播放
#   rep / 单曲    单曲循环
#   list / 列表   查看电台/歌曲列表
#   help / 帮助   查看命令
import pymumble_py3
import subprocess
import time
import threading
import queue
import os
import random
import glob
from pymumble_py3.callbacks import (
    PYMUMBLE_CLBK_TEXTMESSAGERECEIVED as PTX,
)

# ===== 配置 =====
SERVER = "127.0.0.1"
PORT = 64738
PASSWORD = ""
NICK = "Radio-Bot"
MUSIC_DIR = "/root/music"   # 本地音乐目录

RATE = 48000
CHANNELS = 1
CHUNK_MS = 10
CHUNK_SIZE = int(RATE * 2 * CHUNK_MS / 1000)

# ===== 电台列表 =====
STATIONS = {
    "1": {"name": "中国之声", "url": "https://ngcdn001.cnr.cn/live/zgzs/index.m3u8"},
    "2": {"name": "经济之声", "url": "https://ngcdn001.cnr.cn/live/zgzs/index.m3u8"},
}

# ===== 状态 =====
mode = "idle"          # idle / radio / music
current_station = "1"
play_mode = "seq"      # seq=顺序, rand=随机, rep=单曲
music_list = []
music_index = 0
current_proc = None
audio_queue = queue.Queue(maxsize=300)
playing = False


def scan_music():
    """扫描本地音乐目录"""
    global music_list
    exts = ("*.mp3", "*.wav", "*.ogg", "*.flac", "*.m4a")
    files = []
    for e in exts:
        files.extend(glob.glob(os.path.join(MUSIC_DIR, e)))
    music_list = sorted(files)
    return music_list


def ffmpeg_reader(url):
    """拉流/读文件并写入队列"""
    global current_proc
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-i", url,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(RATE), "-ac", str(CHANNELS), "-"
    ]
    current_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=CHUNK_SIZE)
    while True:
        data = current_proc.stdout.read(CHUNK_SIZE)
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


def player(mumble):
    """从队列取数据喂给Mumble"""
    time.sleep(0.5)
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
    """停止一切播放"""
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


def start_station(mumble, sid):
    """开始播放电台"""
    global current_station, mode
    stop_all()
    current_station = sid
    mode = "radio"
    playing = True
    threading.Thread(target=ffmpeg_reader, args=(STATIONS[sid]["url"],), daemon=True).start()
    return "播放电台: " + STATIONS[sid]["name"]


def play_next_song(mumble):
    """播放下一首本地歌曲"""
    global music_index
    if not music_list:
        scan_music()
    if not music_list:
        return "本地音乐目录无歌曲"

    if play_mode == "rep":
        pass  # 单曲循环，不切歌
    elif play_mode == "rand":
        music_index = random.randint(0, len(music_list) - 1)
    else:  # seq
        music_index = (music_index + 1) % len(music_list)

    song = music_list[music_index]
    name = os.path.basename(song)
    stop_all()
    mode = "music"
    playing = True
    threading.Thread(target=ffmpeg_reader, args=(song,), daemon=True).start()
    return "播放: " + name


def start_music(mumble):
    """开始本地音乐播放"""
    songs = scan_music()
    if not songs:
        return "本地音乐目录无歌曲: " + MUSIC_DIR
    return play_next_song(mumble)


def parse_cmd(text):
    """解析命令，支持p1和p 1两种写法"""
    t = text.strip().lower()

    # 处理 p1 p2 这种连写
    if t.startswith("p") and len(t) >= 2 and t[1:].isdigit():
        return ["p", t[1:]]

    return t.split()


def on_text_received(message):
    """处理文本命令"""
    text = message.message.strip()
    sender = message.actor
    user = mumble.users[sender]["name"] if sender in mumble.users else "未知"

    parts = parse_cmd(text)
    cmd = parts[0] if parts else ""

    reply = ""

    if cmd in ("p", "播放"):
        if len(parts) >= 1 and parts[-1] in STATIONS:
            reply = start_station(mumble, parts[-1])
        elif mode == "music":
            # 继续播放下一首
            reply = play_next_song(mumble)
        else:
            reply = start_station(mumble, current_station)

    elif cmd in ("t", "停止"):
        stop_all()
        reply = "已停止"

    elif cmd in ("local", "本地"):
        reply = start_music(mumble)

    elif cmd in ("rand", "随机"):
        play_mode = "rand"
        reply = "随机播放"

    elif cmd in ("seq", "顺序"):
        play_mode = "seq"
        reply = "顺序播放"

    elif cmd in ("rep", "单曲"):
        play_mode = "rep"
        reply = "单曲循环"

    elif cmd == "list":
        lines = ["=== 电台 ==="]
        for k, v in STATIONS.items():
            mark = " (当前)" if k == current_station and mode == "radio" else ""
            lines.append("  p%s %s%s" % (k, v["name"], mark))
        lines.append("")
        lines.append("=== 本地音乐 ===")
        lines.append("  目录: " + MUSIC_DIR)
        lines.append("  歌曲数: %d" % len(music_list))
        lines.append("  模式: %s" % {"seq": "顺序", "rand": "随机", "rep": "单曲循环"}[play_mode])
        reply = "\n".join(lines)

    elif cmd in ("help", "帮助"):
        reply = (
            "=== 命令 ===\n"
            "p / 播放      播放/继续\n"
            "t / 停止      停止\n"
            "p1 p2         切换电台（p1和p 1都行）\n"
            "local / 本地  播放本地音乐\n"
            "rand / 随机   随机播放\n"
            "seq / 顺序    顺序播放\n"
            "rep / 单曲    单曲循环\n"
            "list / 列表   查看电台和歌曲\n"
            "help / 帮助   本帮助"
        )
    else:
        return

    try:
        mumble.users[sender].send_text_message(reply)
    except Exception:
        pass
    print("[%s] %s -> %s" % (user, text, reply.replace("\n", " | ")))


if __name__ == "__main__":
    # 启动先扫描一次音乐
    scan_music()
    print("本地音乐目录: %s, 共%d首" % (MUSIC_DIR, len(music_list)))

    mumble = pymumble_py3.Mumble(SERVER, NICK, port=PORT, password=PASSWORD)
    mumble.callbacks.set_callback(PTX, on_text_received)
    mumble.start()
    mumble.is_ready()

    print("Radio Bot 已连接 %s:%d" % (SERVER, PORT))
    print("命令: p=播放, t=停止, p1/p2=电台, local=本地音乐, rand/seq/rep=播放模式")

    threading.Thread(target=player, args=(mumble,), daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("退出")
