#!/usr/bin/env python3
# 中国之声流媒体播放机器人
# 用法: python3 examples/cnr1_stream_bot.py
import pymumble_py3
import subprocess
import time
import threading
import queue

# ===== 配置 =====
SERVER = "127.0.0.1"
PORT = 64738
PASSWORD = ""
NICK = "CNR1-Radio"
STREAM_URL = "https://ngcdn001.cnr.cn/live/zgzs/index.m3u8"

RATE = 48000
CHANNELS = 1
CHUNK_MS = 10
CHUNK_SIZE = int(RATE * 2 * CHUNK_MS / 1000)

audio_queue = queue.Queue(maxsize=200)


def ffmpeg_reader():
    """后台拉取流媒体，解码为PCM写入队列"""
    cmd = [
        "ffmpeg", "-loglevel", "error",
        "-i", STREAM_URL,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(RATE), "-ac", str(CHANNELS), "-"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=CHUNK_SIZE)
    while True:
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


def player(mumble):
    """从队列取数据喂给Mumble"""
    time.sleep(0.5)  # 先缓冲0.5秒
    while True:
        try:
            data = audio_queue.get(timeout=2)
            mumble.sound_output.add_sound(data)
            time.sleep(CHUNK_MS / 1000.0)
        except queue.Empty:
            time.sleep(0.05)


if __name__ == "__main__":
    mumble = pymumble_py3.Mumble(SERVER, NICK, port=PORT, password=PASSWORD)
    mumble.start()
    mumble.is_ready()
    print("已连接 %s:%d，开始播放中国之声..." % (SERVER, PORT))

    threading.Thread(target=ffmpeg_reader, daemon=True).start()
    threading.Thread(target=player, args=(mumble,), daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("停止播放")
