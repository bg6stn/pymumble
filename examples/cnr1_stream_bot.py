#!/usr/bin/env python3
# 中国之声流媒体播放机器人（支持文本命令控制）
# 用法: python3 examples/cnr1_stream_bot.py
#
# Mumble频道内发送文本命令：
#   p          播放当前电台
#   t          停止播放
#   p 1        切换到中国之声并播放
#   p 2        切换到经济之声并播放
#   list       查看电台列表
#   help       查看帮助
import pymumble_py3
import subprocess
import time
import threading
import queue
from pymumble_py3.callbacks import (
    PYMUMBLE_CLBK_TEXTMESSAGERECEIVED as PTX,
)

# ===== 配置 =====
SERVER = "127.0.0.1"
PORT = 64738
PASSWORD = ""
NICK = "Radio-Bot"

RATE = 48000
CHANNELS = 1
CHUNK_MS = 10
CHUNK_SIZE = int(RATE * 2 * CHUNK_MS / 1000)

# ===== 电台列表 =====
STATIONS = {
    "1": {"name": "中国之声", "url": "https://ngcdn001.cnr.cn/live/zgzs/index.m3u8"},
    "2": {"name": "经济之声", "url": "https://ngcdn001.cnr.cn/live/zgzs/index.m3u8"},
}

# ===== 全局状态 =====
current_station = "1"
current_proc = None
audio_queue = queue.Queue(maxsize=200)
playing = False


def ffmpeg_reader(url):
    """拉流并写入队列"""
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


def start_play(mumble, station_id=None):
    """开始播放指定电台"""
    global current_proc, playing, current_station

    if station_id and station_id in STATIONS:
        current_station = station_id

    # 清空旧队列
    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break

    # 杀掉旧进程
    if current_proc:
        try:
            current_proc.terminate()
        except Exception:
            pass

    playing = True
    threading.Thread(target=ffmpeg_reader, args=(STATIONS[current_station]["url"],), daemon=True).start()
    return "开始播放: " + STATIONS[current_station]["name"]


def stop_play():
    """停止播放"""
    global playing, current_proc
    playing = False
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
    return "已停止播放"


def on_text_received(message):
    """处理收到的文本消息"""
    text = message.message.strip()
    sender = message.actor
    user = mumble.users[sender]["name"] if sender in mumble.users else "未知"

    parts = text.lower().split()
    cmd = parts[0] if parts else ""

    reply = ""

    if cmd in ("p", "播放"):
        if len(parts) >= 2 and parts[1] in STATIONS:
            reply = start_play(mumble, parts[1])
        else:
            reply = start_play(mumble)
    elif cmd in ("t", "停止"):
        reply = stop_play()
    elif cmd == "list":
        lines = ["电台列表:"]
        for k, v in STATIONS.items():
            mark = " (当前)" if k == current_station else ""
            lines.append("  %s. %s%s" % (k, v["name"], mark))
        reply = "\n".join(lines)
    elif cmd == "help":
        reply = "命令:\n" \
                "  p / 播放      播放当前电台\n" \
                "  p 1 / p 2     切换电台\n" \
                "  t / 停止      停止播放\n" \
                "  list          电台列表\n" \
                "  help          帮助"
    else:
        return

    # 回复发送者
    try:
        mumble.users[sender].send_text_message(reply)
    except Exception:
        pass
    print("[%s] %s -> %s" % (user, text, reply.replace("\n", " | ")))


if __name__ == "__main__":
    mumble = pymumble_py3.Mumble(SERVER, NICK, port=PORT, password=PASSWORD)
    mumble.callbacks.set_callback(PTX, on_text_received)
    mumble.start()
    mumble.is_ready()

    print("Radio Bot 已连接 %s:%d" % (SERVER, PORT))
    print("命令: p=播放, t=停止, p 1=中国之声, p 2=经济之声, list=列表, help=帮助")

    threading.Thread(target=player, args=(mumble,), daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("退出")
