#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radio-Bot: Mumble 网络电台/对讲网关
- 播放网络电台（实时获取最新签名 URL）
- 播放本地音乐（支持断点续播）
- 对讲模式（Mumble <-> 物理电台，CM108 GPIO PTT）
- 纯数字播放列表切换
- 完整状态持久化（音量/喇叭/增益/播放位置等）
"""

import pymumble_py3
import subprocess
import time
import threading
import queue
import os
import random
import re
import json
import urllib.request
import struct
import traceback
from pymumble_py3.callbacks import (
    PYMUMBLE_CLBK_TEXTMESSAGERECEIVED as PTX,
    PYMUMBLE_CLBK_SOUNDRECEIVED as PSND,
)

# ==================== 配置 ====================
SERVER       = "127.0.0.1"
PORT         = 64738
PASSWORD     = ""
NICK         = "Radio-Bot"

MUSIC_DIR    = "/root/music"

# ===== PTT =====
PTT_GPIO_PIN = 3
PTT_INVERT   = True
PTT_DEVICE   = "/dev/hidraw0"
PTT_HANGTIME = 0.2

SPEAKER_DEVICE = "plughw:0,0"
MIC_DEVICE     = "plughw:0,0"

RATE       = 48000
CHANNELS   = 1
FRAME_MS   = 20
FRAME_SIZE = RATE * 2 * CHANNELS * FRAME_MS // 1000

BUFFER_FRAMES      = 50
MAX_PCM_QUEUE      = 10
TARGET_PCM_QUEUE   = 4
PLAYER_TICK_MS     = 10
BATCH_FRAMES       = 2

USE_CODEC_PROFILE = False
USE_SET_BANDWIDTH = False
TARGET_BANDWIDTH  = 72000

DEFAULT_VOLUME    = 0.2
DEFAULT_SPEAKER   = False
DEFAULT_MIC_GAIN  = 3.0
DEFAULT_BW_KBPS   = 128
DEFAULT_PLAY_MODE = "seq"

# 位置保存间隔（秒）
POSITION_SAVE_INTERVAL = 5

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
STATIONS_FILE = os.path.join(BASE_DIR, "stations.json")
STATE_FILE    = os.path.join(BASE_DIR, "state.json")

START_TIME = time.time()

# ==================== 央广内置电台 ====================
CNR_BUILTIN = [
    ("zgzs", "中国之声",     "https://ngcdn001.cnr.cn/live/zgzs/index.m3u8"),
    ("jjzs", "经济之声",     "https://ngcdn002.cnr.cn/live/jjzs/index.m3u8"),
    ("yyzs", "音乐之声",     "https://ngcdn003.cnr.cn/live/yyzs/index.m3u8"),
    ("dszs", "经典音乐广播", "https://ngcdn004.cnr.cn/live/dszs/index.m3u8"),
    ("zhzs", "台海之声",     "https://ngcdn005.cnr.cn/live/zhzs/index.m3u8"),
    ("szzs", "神州之声",     "https://ngcdn006.cnr.cn/live/szzs/index.m3u8"),
    ("hxzs", "大湾区之声",   "https://ngcdn007.cnr.cn/live/hxzs/index.m3u8"),
    ("xgzs", "香港之声",     "https://ngcdn008.cnr.cn/live/xgzs/index.m3u8"),
    ("mzzs", "民族之声",     "https://ngcdn009.cnr.cn/live/mzzs/index.m3u8"),
    ("wyzs", "文艺之声",     "https://ngcdn010.cnr.cn/live/wyzs/index.m3u8"),
    ("lnzs", "老年之声",     "https://ngcdn011.cnr.cn/live/lnzs/index.m3u8"),
    ("zygb", "藏语广播",     "https://ngcdn012.cnr.cn/live/zygb/index.m3u8"),
    ("wygb", "维语广播",     "https://ngcdn013.cnr.cn/live/wygb/index.m3u8"),
    ("gsgljtgb", "中国交通广播", "https://ngcdn016.cnr.cn/live/gsgljtgb/index.m3u8"),
    ("xczs", "中国乡村之声", "https://ngcdn017.cnr.cn/live/xczs/index.m3u8"),
    ("hygb", "哈语广播",     "https://ngcdn025.cnr.cn/live/hygb/index.m3u8"),
]
DEFAULT_STATIONS = {str(i): {"name": n, "url": u}
                    for i, (_, n, u) in enumerate(CNR_BUILTIN, 1)}


# ==================== 工具 ====================
def refresh_cnr_urls():
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
    except Exception as e:
        print("[cnr] 刷新URL失败: %s" % e)
        return {}


def strip_html(s):
    return re.sub(r'<[^>]+>', '', s).strip()


def fmt_uptime(seconds):
    seconds = int(seconds)
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if d > 0:
        return "%dd %dh %dm %ds" % (d, h, m, s)
    elif h > 0:
        return "%dh %dm %ds" % (h, m, s)
    elif m > 0:
        return "%dm %ds" % (m, s)
    else:
        return "%ds" % s


def fmt_duration(seconds):
    """格式化秒为 mm:ss 或 hh:mm:ss"""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return "%d:%02d:%02d" % (h, m, s)
    else:
        return "%d:%02d" % (m, s)


# ==================== 状态 ====================
class State:
    def __init__(self):
        # 播放状态
        self.mode = "idle"
        self.current_station = "1"
        self.play_mode = DEFAULT_PLAY_MODE
        self.music_list = []
        self.music_index = 0
        self.playing = False
        self.listening = False

        # 音频参数
        self.speaker_enabled = DEFAULT_SPEAKER
        self.mic_gain = DEFAULT_MIC_GAIN
        self.play_volume = DEFAULT_VOLUME
        self.bandwidth_kbps = DEFAULT_BW_KBPS

        # 会话
        self.session_id = 0
        self.lock = threading.Lock()

        # 断点续播
        self.current_song_path = None
        self.music_position = 0.0        # 当前音乐已播放秒数
        self._position_dirty = False     # 是否需要写盘

    def next_session(self):
        with self.lock:
            self.session_id += 1
            return self.session_id

    def current_session(self):
        with self.lock:
            return self.session_id


S = State()

mumble = None
ptt = None
STATIONS = {}

audio_queue = queue.Queue(maxsize=BUFFER_FRAMES)

current_proc = None
speaker_proc = None
mic_proc = None

channel_timer = None
channel_active = False
mic_active = False
mic_timer = None

_feed_count = 0
_feed_err_count = 0
_underrun_count = 0

_auto_advance_lock = threading.Lock()
_auto_advance_pending = False

# 位置跟踪
_position_lock = threading.Lock()
_current_reader_frames = 0        # 当前 reader 已读帧数


# ==================== 电台/音乐文件管理 ====================
def load_stations():
    global STATIONS
    if os.path.exists(STATIONS_FILE):
        try:
            with open(STATIONS_FILE, "r", encoding="utf-8") as f:
                STATIONS = json.load(f)
                return
        except Exception:
            pass
    STATIONS = dict(DEFAULT_STATIONS)


def save_stations():
    try:
        with open(STATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(STATIONS, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def scan_music():
    exts = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".wma", ".aac", ".opus")
    files = []
    for root, _, fs in os.walk(MUSIC_DIR):
        for f in fs:
            if f.lower().endswith(exts):
                files.append(os.path.join(root, f))
    S.music_list = sorted(files)
    return S.music_list


# ==================== 状态持久化 ====================
def save_state(verbose=False):
    """把全部状态写入 state.json"""
    try:
        data = {
            "version": 2,
            # 播放
            "mode": S.mode,
            "current_station": S.current_station,
            "play_mode": S.play_mode,
            "music_index": S.music_index,
            # 音频参数
            "play_volume": S.play_volume,
            "speaker_enabled": S.speaker_enabled,
            "mic_gain": S.mic_gain,
            "bandwidth_kbps": S.bandwidth_kbps,
            # 断点续播
            "current_song_path": S.current_song_path,
            "music_position": S.music_position,
            # 时间戳
            "saved_at": time.time(),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if verbose:
            print("[state] 已保存: mode=%s song=%s pos=%.1fs vol=%d%%" % (
                S.mode,
                os.path.basename(S.current_song_path) if S.current_song_path else "-",
                S.music_position,
                int(S.play_volume * 100)))
        S._position_dirty = False
    except Exception as e:
        print("[state] 保存失败: %s" % e)


def load_state():
    """读取 state.json，恢复全部参数"""
    if not os.path.exists(STATE_FILE):
        print("[state] 无历史状态，使用默认值")
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)

        # 播放
        S.mode = d.get("mode", "idle")
        S.current_station = d.get("current_station", "1")
        S.play_mode = d.get("play_mode", DEFAULT_PLAY_MODE)
        S.music_index = d.get("music_index", 0)

        # 音频参数
        S.play_volume = float(d.get("play_volume", DEFAULT_VOLUME))
        S.speaker_enabled = bool(d.get("speaker_enabled", DEFAULT_SPEAKER))
        S.mic_gain = float(d.get("mic_gain", DEFAULT_MIC_GAIN))
        S.bandwidth_kbps = int(d.get("bandwidth_kbps", DEFAULT_BW_KBPS))

        # 断点续播
        S.current_song_path = d.get("current_song_path")
        S.music_position = float(d.get("music_position", 0.0))

        saved_at = d.get("saved_at", 0)
        print("[state] 已恢复: mode=%s station=%s song=%s pos=%.1fs vol=%d%% 喇叭=%s 增益=%.1fx" % (
            S.mode, S.current_station,
            os.path.basename(S.current_song_path) if S.current_song_path else "-",
            S.music_position,
            int(S.play_volume * 100),
            "开" if S.speaker_enabled else "关",
            S.mic_gain))
        if saved_at > 0:
            age = time.time() - saved_at
            print("[state] 上次保存于 %s 前" % fmt_uptime(age))
    except Exception as e:
        print("[state] 加载失败: %s" % e)


def mark_position_dirty():
    S._position_dirty = True


def position_saver_thread():
    """每 POSITION_SAVE_INTERVAL 秒把播放位置写盘"""
    while True:
        try:
            time.sleep(POSITION_SAVE_INTERVAL)
            if S._position_dirty and S.mode == "music" and S.playing:
                save_state()
        except Exception as e:
            print("[position_saver] error: %s" % e)


# ==================== 播放列表 ====================
def get_playlist():
    pl = []
    keys = sorted(STATIONS.keys(), key=lambda x: int(x) if x.isdigit() else 9999)
    for sid in keys:
        pl.append({"type": "radio", "id": sid,
                   "name": STATIONS[sid]["name"],
                   "url": STATIONS[sid]["url"]})
    for i, song in enumerate(S.music_list):
        pl.append({"type": "music", "id": i,
                   "name": os.path.basename(song),
                   "path": song})
    return pl


# ==================== PTT ====================
def init_ptt():
    global ptt
    try:
        from pymumble_py3.cm108_ptt import (
            CM108PTT, CM108PTTError, list_cm108_devices
        )
        try:
            devices = list_cm108_devices()
            print("[ptt] 发现 CM108 设备: %s" % devices)
        except Exception as e:
            print("[ptt] list_cm108_devices 失败: %s" % e)

        if not os.path.exists(PTT_DEVICE):
            print("[ptt] 设备不存在: %s" % PTT_DEVICE)
            print("[ptt] 用 ls /dev/hidraw* 确认实际路径")
            ptt = None
            return

        ptt = CM108PTT(
            device_path=PTT_DEVICE,
            gpio_pin=PTT_GPIO_PIN,
            invert=PTT_INVERT,
            auto_detect=False,
        )
        print("[ptt] CM108 初始化成功: %s (GPIO%d, invert=%s)" % (
            ptt.device_path, PTT_GPIO_PIN, PTT_INVERT))
    except CM108PTTError as e:
        print("[ptt] CM108 初始化失败: %s" % e)
        ptt = None
    except Exception as e:
        print("[ptt] 未知错误: %s" % e)
        traceback.print_exc()
        ptt = None


def ptt_on():
    if ptt:
        try:
            ptt.ptt_on()
        except Exception:
            pass


def ptt_off():
    if ptt:
        try:
            ptt.ptt_off()
        except Exception:
            pass


def set_auto_ptt(enabled):
    if mumble is None or ptt is None:
        return
    try:
        if enabled:
            mumble.sound_output.ptt_controller = ptt
            mumble.sound_output.ptt_hangtime = PTT_HANGTIME
        else:
            mumble.sound_output.ptt_controller = None
    except Exception:
        pass


# ==================== 本地喇叭 ====================
def close_speaker():
    global speaker_proc
    if speaker_proc:
        try:
            speaker_proc.stdin.close()
        except Exception:
            pass
        try:
            speaker_proc.terminate()
            speaker_proc.wait(timeout=1)
        except Exception:
            try:
                speaker_proc.kill()
            except Exception:
                pass
        speaker_proc = None


def open_speaker(channels=1):
    global speaker_proc
    close_speaker()
    try:
        speaker_proc = subprocess.Popen(
            ["aplay", "-D", SPEAKER_DEVICE,
             "-f", "S16_LE", "-r", str(RATE), "-c", str(channels),
             "--buffer-time=500000", "--period-time=100000",
             "-q"],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print("[speaker] 打开失败: %s" % e)
        speaker_proc = None


# ==================== pymumble 内部队列兼容 ====================
def get_pcm_obj():
    if mumble is None:
        return None
    try:
        return mumble.sound_output.pcm
    except Exception:
        return None


def get_pcm_size():
    pcm = get_pcm_obj()
    if pcm is None:
        return 0
    if hasattr(pcm, "qsize"):
        try:
            return pcm.qsize()
        except Exception:
            return 0
    try:
        return len(pcm)
    except Exception:
        return 0


def clear_pcm():
    if mumble is None:
        return
    try:
        mumble.sound_output.clear_buffer()
        return
    except Exception:
        pass
    pcm = get_pcm_obj()
    if pcm is None:
        return
    if hasattr(pcm, "get_nowait"):
        while not pcm.empty():
            try:
                pcm.get_nowait()
            except Exception:
                break
    elif isinstance(pcm, list):
        pcm.clear()


def reset_sequence(sleep_after=True):
    if mumble is None:
        return
    try:
        so = mumble.sound_output
        so.sequence = 0
        so.sequence_start_time = 0
        so.sequence_last_time = 0
    except Exception as e:
        print("[seq] 重置失败: %s" % e)
    if sleep_after:
        time.sleep(0.05)


def get_encoder_info():
    if mumble is None:
        return "no mumble"
    so = mumble.sound_output
    return "codec_type=%s encoder=%s bw=%s" % (
        getattr(so, "codec_type", None),
        "OK" if getattr(so, "encoder", None) else "NONE",
        getattr(so, "bandwidth", None),
    )


# ==================== self_mute / self_deaf ====================
def ensure_unmuted():
    if mumble is None:
        return
    try:
        me = mumble.users.myself
        sm = me.get("self_mute")
        sd = me.get("self_deaf")
        print("[me] self_mute=%s self_deaf=%s" % (sm, sd))
        if sm:
            try:
                me.mute()
                print("[me] 已取消 mute")
            except Exception:
                pass
        if sd:
            try:
                me.deafen()
                print("[me] 已取消 deafen")
            except Exception:
                pass
    except Exception as e:
        print("[me] 检查失败: %s" % e)


# ==================== 音频发送核心 ====================
def feed_mumble(data):
    global _feed_count, _feed_err_count
    if mumble is None:
        return False
    try:
        mumble.sound_output.add_sound(data)
        _feed_count += 1
        return True
    except Exception as e:
        _feed_err_count += 1
        if _feed_err_count < 5:
            print("[feed] add_sound 异常 #%d: %s" % (_feed_err_count, e))
        return False


# ==================== 自动下一首 ====================
def on_song_finished(my_session, path):
    global _auto_advance_pending
    if my_session != S.current_session():
        return
    if S.mode != "music":
        return

    with _auto_advance_lock:
        if _auto_advance_pending:
            return
        _auto_advance_pending = True

    try:
        print("[auto] 歌曲结束，准备切换: %s" % os.path.basename(path))
        time.sleep(0.3)
        if my_session != S.current_session():
            return

        if S.play_mode == "rep":
            print("[auto] 单曲循环，重播")
            play_song(path, is_auto=True, start_pos=0.0)
        else:
            print("[auto] 播放下一首")
            play_next(is_auto=True)
    finally:
        with _auto_advance_lock:
            _auto_advance_pending = False


# ==================== ffmpeg 读流线程 ====================
def ffmpeg_reader(url, my_session, is_music=False, path=None, start_pos=0.0):
    global current_proc, _current_reader_frames
    af = "volume=%.2f" % S.play_volume if S.play_volume != 1.0 else "volume=1.0"
    cmd = [
        "ffmpeg", "-loglevel", "warning",
    ]
    # 从指定位置开始（音乐断点续播）
    if is_music and start_pos > 0:
        cmd += ["-ss", "%.3f" % start_pos]
    cmd += [
        "-i", url,
        "-af", af,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(RATE), "-ac", str(CHANNELS),
        "-"
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                bufsize=FRAME_SIZE * 16)
    except Exception as e:
        print("[ffmpeg] 启动失败: %s" % e)
        return
    current_proc = proc

    def drain_err():
        try:
            for line in iter(proc.stderr.readline, b""):
                if line:
                    print("[ffmpeg] %s" % line.decode("utf-8", "replace").rstrip())
        except Exception:
            pass
    threading.Thread(target=drain_err, daemon=True).start()

    frame_bytes = FRAME_SIZE
    count = 0
    natural_end = False
    # 每秒保存一次位置
    last_pos_save = time.time()
    while True:
        if my_session != S.current_session():
            break
        if proc.poll() is not None:
            natural_end = True
            print("[ffmpeg] 退出 code=%s url=%s" % (proc.returncode, url))
            break
        try:
            data = proc.stdout.read(frame_bytes)
        except Exception as e:
            print("[ffmpeg] read 异常: %s" % e)
            break
        if not data:
            natural_end = True
            break
        if len(data) < frame_bytes:
            data += b"\x00" * (frame_bytes - len(data))
        try:
            audio_queue.put(data, timeout=0.5)
        except queue.Full:
            try:
                audio_queue.get_nowait()
                audio_queue.put_nowait(data)
            except Exception:
                pass
        count += 1

        # 更新当前位置（每 1 秒写一次内存 + 标 dirty）
        if is_music and path:
            now = time.time()
            if now - last_pos_save >= 1.0:
                # 已读秒数 = start_pos + 读的帧数 * 20ms
                pos = start_pos + count * (FRAME_MS / 1000.0)
                with _position_lock:
                    S.music_position = pos
                    _current_reader_frames = count
                mark_position_dirty()
                last_pos_save = now

    try:
        proc.terminate()
        proc.wait(timeout=1)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    print("[ffmpeg] reader 结束 url=%s frames=%d" % (url, count))

    # 如果是音乐且自然结束，清位置
    if natural_end and is_music and path:
        with _position_lock:
            S.music_position = 0.0
        mark_position_dirty()

    if natural_end and is_music and path:
        def waiter():
            while not audio_queue.empty():
                if my_session != S.current_session():
                    return
                time.sleep(0.1)
            time.sleep(0.2)
            on_song_finished(my_session, path)
        threading.Thread(target=waiter, daemon=True).start()


# ==================== player 线程 ====================
def player_thread():
    global speaker_proc, _underrun_count
    time.sleep(1.0)
    last_stat = 0
    while True:
        try:
            if not S.playing or S.listening:
                time.sleep(0.05)
                continue

            now = time.time()
            if now - last_stat > 5:
                print("[player] q=%d pcm=%d speaker=%s enc=%s feed=%d err=%d under=%d" % (
                    audio_queue.qsize(), get_pcm_size(),
                    "on" if speaker_proc else "off",
                    "ok" if (mumble and mumble.sound_output.encoder) else "NONE",
                    _feed_count, _feed_err_count, _underrun_count))
                last_stat = now

            pcm_now = get_pcm_size()
            if pcm_now >= MAX_PCM_QUEUE:
                time.sleep(PLAYER_TICK_MS / 1000.0)
                continue

            need = max(1, TARGET_PCM_QUEUE - pcm_now)
            need = min(need, BATCH_FRAMES)

            got_any = False
            for _ in range(need):
                try:
                    data = audio_queue.get_nowait()
                except queue.Empty:
                    break
                got_any = True
                feed_mumble(data)

                if S.speaker_enabled and S.play_volume > 0:
                    if not speaker_proc:
                        open_speaker(CHANNELS)
                    if speaker_proc:
                        try:
                            speaker_proc.stdin.write(data)
                            speaker_proc.stdin.flush()
                        except Exception:
                            close_speaker()

            if not got_any:
                _underrun_count += 1
                time.sleep(PLAYER_TICK_MS / 1000.0)
            else:
                time.sleep(0.001)

        except Exception as e:
            print("[player] error: %s" % e)
            time.sleep(0.1)


# ==================== 对讲录音 ====================
def mic_listener():
    global mic_proc, mic_active, mic_timer
    time.sleep(0.5)
    rec = ["arecord", "-D", MIC_DEVICE,
           "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw",
           "--period-size=960", "--buffer-size=1920", "-q", "-"]
    gain = ["ffmpeg", "-loglevel", "error",
            "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
            "-af", "volume=%.1f" % S.mic_gain,
            "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-"]
    print("[mic] 启动录音 %s (gain=%.1fx)" % (MIC_DEVICE, S.mic_gain))
    try:
        p1 = subprocess.Popen(rec, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, bufsize=FRAME_SIZE)
        p2 = subprocess.Popen(gain, stdin=p1.stdout, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, bufsize=FRAME_SIZE)
        p1.stdout.close()
    except Exception as e:
        print("[mic] 启动失败: %s" % e)
        return
    mic_proc = p2
    while True:
        if p2.poll() is not None:
            break
        data = p2.stdout.read(FRAME_SIZE)
        if not data:
            break
        n = len(data) // 2
        if n > 0:
            samples = struct.unpack("<%dh" % n, data)
            energy = sum(abs(s) for s in samples) // n
            if energy > 800:
                mic_active = True
                if mic_timer:
                    mic_timer.cancel()
                mic_timer = threading.Timer(0.3, set_mic_inactive)
                mic_timer.start()
        if not channel_active:
            feed_mumble(data)
    print("[mic] 退出")


def stop_mic():
    global mic_proc
    S.listening = False
    if mic_proc:
        try:
            mic_proc.terminate()
        except Exception:
            pass
        mic_proc = None


def set_mic_inactive():
    global mic_active
    mic_active = False


# ==================== 播放控制 ====================
def stop_all(save_pos=True):
    global current_proc
    # 先把当前音乐位置写盘
    if save_pos and S.mode == "music" and S.current_song_path:
        save_state()
    S.next_session()
    S.playing = False
    if S.mode != "talk":
        S.mode = "idle"
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
    clear_pcm()
    reset_sequence(sleep_after=False)


def exit_talk_mode():
    if S.listening:
        stop_mic()
        set_auto_ptt(True)
        ptt_off()


def start_station(sid):
    if sid not in STATIONS:
        return "电台不存在: %s" % sid
    exit_talk_mode()
    stop_all()
    S.current_station = sid
    S.mode = "radio"
    S.current_song_path = None
    S.music_position = 0.0
    set_auto_ptt(True)

    url = STATIONS[sid]["url"]
    m = re.search(r'/live/([a-z0-9]+)/', url)
    if m:
        code = m.group(1)
        fresh = refresh_cnr_urls()
        if code in fresh:
            url = fresh[code]
            print("[cnr] 使用最新URL: %s" % url)

    S.playing = True
    session = S.next_session()
    threading.Thread(target=ffmpeg_reader,
                     args=(url, session, False, None, 0.0),
                     daemon=True).start()
    save_state()
    return "播放电台: %s %s" % (sid, STATIONS[sid]["name"])


def play_song(path, is_auto=False, start_pos=None, save_pos_before=True):
    """
    path: 音乐文件路径
    is_auto: 是否自动触发（避免清队列打断）
    start_pos: 起始秒数（None 表示从 S.music_position 读）
    save_pos_before: 切换前是否保存上一个位置
    """
    exit_talk_mode()
    if save_pos_before and not is_auto:
        stop_all(save_pos=True)
    else:
        global current_proc
        S.next_session()
        if current_proc:
            try:
                current_proc.terminate()
            except Exception:
                pass
            current_proc = None
        clear_pcm()

    # 决定起始位置
    if start_pos is None:
        start_pos = 0.0
    if is_auto:
        start_pos = 0.0

    # 如果是同一首歌且是自动切换（循环），从 0 开始
    if S.current_song_path == path and not is_auto:
        # 用户手动重播同一首 → 从 0 开始
        start_pos = 0.0

    S.mode = "music"
    S.current_song_path = path
    S.music_position = start_pos
    set_auto_ptt(True)
    S.playing = True
    session = S.next_session()
    threading.Thread(target=ffmpeg_reader,
                     args=(path, session, True, path, start_pos),
                     daemon=True).start()
    save_state()
    msg = "播放: %s" % os.path.basename(path)
    if start_pos > 0:
        msg += " (从 %s 继续)" % fmt_duration(start_pos)
    return msg


def play_next(is_auto=False):
    if S.mode == "music":
        if not S.music_list:
            return "音乐列表为空"
        if S.play_mode == "rand":
            S.music_index = random.randint(0, len(S.music_list) - 1)
        elif S.play_mode == "rep":
            if 0 <= S.music_index < len(S.music_list):
                return play_song(S.music_list[S.music_index],
                                 is_auto=is_auto, start_pos=0.0)
            S.music_index = 0
        else:
            S.music_index = (S.music_index + 1) % len(S.music_list)
        return play_song(S.music_list[S.music_index],
                         is_auto=is_auto, start_pos=0.0)
    elif S.mode == "radio":
        keys = list(STATIONS.keys())
        if len(keys) <= 1:
            return "只有一个电台"
        idx = keys.index(S.current_station) if S.current_station in keys else 0
        return start_station(keys[(idx + 1) % len(keys)])
    else:
        return "当前未播放"


def play_item(idx, start_pos=0.0):
    pl = get_playlist()
    if idx < 0 or idx >= len(pl):
        return "序号不存在 (共%d项)" % len(pl)
    item = pl[idx]
    if item["type"] == "radio":
        return start_station(item["id"])
    else:
        S.music_index = item["id"]
        return play_song(item["path"], start_pos=start_pos)


def start_music():
    if not S.music_list:
        scan_music()
    if not S.music_list:
        return "音乐目录为空: " + MUSIC_DIR
    exit_talk_mode()
    S.mode = "music"
    return play_next()


# ==================== 对讲回调 ====================
def on_sound_received(user, sound):
    global channel_active, channel_timer
    if not S.listening:
        return
    try:
        pcm = sound.pcm
        if not pcm:
            return
        if not mic_active:
            if S.speaker_enabled and speaker_proc:
                try:
                    speaker_proc.stdin.write(pcm)
                    speaker_proc.stdin.flush()
                except Exception:
                    pass
            ptt_on()
        channel_active = True
        if channel_timer:
            channel_timer.cancel()
        channel_timer = threading.Timer(0.3, on_channel_silent)
        channel_timer.start()
    except Exception:
        pass


def on_channel_silent():
    global channel_active
    channel_active = False
    ptt_off()


# ==================== /z 状态信息 ====================
def build_status():
    lines = []
    lines.append("======= 运行状态 =======")
    lines.append("运行时间: %s" % fmt_uptime(time.time() - START_TIME))
    mode_map = {"idle": "空闲", "radio": "电台", "music": "音乐", "talk": "对讲"}
    lines.append("当前模式: %s" % mode_map.get(S.mode, S.mode))
    lines.append("正在播放: %s" % ("是" if S.playing else "否"))
    lines.append("对讲模式: %s" % ("是" if S.listening else "否"))

    if S.mode == "radio":
        st = STATIONS.get(S.current_station, {})
        lines.append("当前电台: %s %s" % (S.current_station, st.get("name", "?")))
    elif S.mode == "music":
        if 0 <= S.music_index < len(S.music_list):
            lines.append("当前音乐: [%d/%d] %s" % (
                S.music_index + 1, len(S.music_list),
                os.path.basename(S.music_list[S.music_index])))
            lines.append("播放位置: %s" % fmt_duration(S.music_position))

    pm_map = {"seq": "顺序", "rand": "随机", "rep": "单曲循环"}
    lines.append("播放模式: %s" % pm_map.get(S.play_mode, S.play_mode))
    lines.append("播放音量: %d%%" % int(S.play_volume * 100))

    if S.speaker_enabled and speaker_proc:
        lines.append("本地喇叭: 开 (进程运行中)")
    elif S.speaker_enabled:
        lines.append("本地喇叭: 开 (进程未启动)")
    else:
        lines.append("本地喇叭: 关")

    lines.append("话筒增益: %.1fx" % S.mic_gain)
    lines.append("带宽设置: %dkbps" % S.bandwidth_kbps)

    if ptt:
        ptt_state = "未知"
        try:
            if hasattr(ptt, "_ptt_active"):
                ptt_state = "发射中" if ptt._ptt_active else "接收"
        except Exception:
            pass
        dev_path = getattr(ptt, "device_path", PTT_DEVICE)
        lines.append("CM108 PTT: 已启用 (%s, GPIO%d, invert=%s, 状态: %s)" % (
            dev_path, PTT_GPIO_PIN, PTT_INVERT, ptt_state))
    else:
        lines.append("CM108 PTT: 未启用 (设备: %s)" % PTT_DEVICE)

    if mumble and ptt:
        try:
            auto = getattr(mumble.sound_output, "ptt_controller", None)
            lines.append("自动 PTT: %s" % ("开" if auto else "关"))
        except Exception:
            pass

    if mumble:
        try:
            so = mumble.sound_output
            enc = "OK" if getattr(so, "encoder", None) else "NONE"
            lines.append("Opus编码器: %s (codec_type=%s, bw=%s)" % (
                enc, getattr(so, "codec_type", "?"),
                getattr(so, "bandwidth", "?")))
        except Exception:
            lines.append("Opus编码器: 无法读取")

    lines.append("音频队列: %d/%d" % (audio_queue.qsize(), BUFFER_FRAMES))
    lines.append("pymumble队列: %d/%d" % (get_pcm_size(), MAX_PCM_QUEUE))
    lines.append("喂帧计数: %d (错误 %d)" % (_feed_count, _feed_err_count))
    lines.append("队列空转: %d" % _underrun_count)

    procs = []
    if current_proc:
        procs.append("ffmpeg")
    if speaker_proc:
        procs.append("aplay")
    if mic_proc:
        procs.append("arecord")
    lines.append("运行进程: %s" % (", ".join(procs) if procs else "无"))
    lines.append("音乐数量: %d 首" % len(S.music_list))
    lines.append("电台数量: %d 个" % len(STATIONS))

    if mumble:
        try:
            lines.append("在线用户: %d" % len(mumble.users))
        except Exception:
            pass
        try:
            lines.append("连接状态: %s" % ("正常" if mumble.is_alive() else "断开"))
        except Exception:
            pass

    # 状态文件
    if os.path.exists(STATE_FILE):
        try:
            age = time.time() - os.path.getmtime(STATE_FILE)
            lines.append("上次存档: %s 前" % fmt_uptime(age))
        except Exception:
            pass

    lines.append("服务器: %s:%d" % (SERVER, PORT))
    lines.append("昵称: %s" % NICK)
    lines.append("========================")
    return "\n".join(lines)


# ==================== 命令解析 ====================
def parse_cmd(text):
    t = strip_html(text)
    if not t:
        return None
    if t.isdigit():
        return ["#num", t]
    if not t.startswith("/"):
        return None
    t = t[1:].strip()
    if t.startswith("p") and len(t) >= 2 and t[1:].isdigit():
        return ["p", t[1:]]
    for cmd in ("vol", "mic", "bw", "rm"):
        if t.startswith(cmd) and len(t) > len(cmd) and t[len(cmd):].isdigit():
            return [cmd, t[len(cmd):]]
    return t.split()


def on_text_received(message):
    global STATIONS
    text = strip_html(message.message).strip()
    sender = message.actor
    user_name = mumble.users[sender]["name"] if sender in mumble.users else "?"

    parts = parse_cmd(text)
    if not parts:
        return
    cmd = parts[0].lower()
    reply = ""

    if cmd == "#num":
        reply = play_item(int(parts[1]) - 1)

    elif cmd == "p":
        if len(parts) >= 2 and parts[1].isdigit():
            reply = play_item(int(parts[1]) - 1)
        elif S.mode == "music":
            reply = play_next()
        elif S.mode == "radio":
            s = STATIONS.get(S.current_station, {})
            reply = "当前: %s %s\nURL: %s" % (S.current_station, s.get("name", "?"), s.get("url", "?"))
        else:
            reply = start_station(S.current_station)

    elif cmd == "t":
        stop_all(save_pos=True)
        stop_mic()
        reply = "已停止"

    elif cmd == "dj":
        if S.listening:
            exit_talk_mode()
            reply = "退出对讲"
            if S.mode == "radio" and S.current_station in STATIONS:
                start_station(S.current_station)
            elif S.mode == "music" and S.music_list:
                play_song(S.music_list[S.music_index])
        else:
            stop_all(save_pos=True)
            stop_mic()
            set_auto_ptt(False)
            S.listening = True
            S.mode = "talk"
            open_speaker(1)
            threading.Thread(target=mic_listener, daemon=True).start()
            reply = "对讲中（话筒<->频道），发 /dj 退出"

    elif cmd == "mic":
        if len(parts) < 2:
            S.mic_gain = DEFAULT_MIC_GAIN
        elif parts[1] == "+":
            S.mic_gain = min(10.0, S.mic_gain + 1.0)
        elif parts[1] == "-":
            S.mic_gain = max(0.5, S.mic_gain - 1.0)
        reply = "话筒增益: %.1fx" % S.mic_gain
        save_state()

    elif cmd == "vol":
        if len(parts) < 2:
            S.play_volume = DEFAULT_VOLUME
        elif parts[1].isdigit():
            v = max(0, min(99, int(parts[1])))
            S.play_volume = v / 100.0
        elif parts[1] == "+":
            v = min(99, int(S.play_volume * 100) + 5)
            S.play_volume = v / 100.0
        elif parts[1] == "-":
            v = max(0, int(S.play_volume * 100) - 5)
            S.play_volume = v / 100.0
        v = int(S.play_volume * 100)
        subprocess.run(["amixer", "set", "Speaker", "%d%%" % v], capture_output=True)
        reply = "音量: %d%%" % v
        save_state()

    elif cmd == "bw":
        if len(parts) < 2:
            S.bandwidth_kbps = DEFAULT_BW_KBPS
        elif parts[1] == "+":
            S.bandwidth_kbps = min(512, S.bandwidth_kbps + 16)
        elif parts[1] == "-":
            S.bandwidth_kbps = max(32, S.bandwidth_kbps - 16)
        reply = "带宽: %dkbps" % S.bandwidth_kbps
        save_state()

    elif cmd == "on":
        S.speaker_enabled = True
        S.play_volume = DEFAULT_VOLUME
        v = int(DEFAULT_VOLUME * 100)
        subprocess.run(["amixer", "set", "Speaker", "%d%%" % v], capture_output=True)
        reply = "本地喇叭已开 (%d%%)" % v
        save_state()

    elif cmd == "off":
        S.speaker_enabled = False
        S.play_volume = 0.0
        subprocess.run(["amixer", "set", "Speaker", "0%"], capture_output=True)
        reply = "本地喇叭已关"
        save_state()

    elif cmd in ("local", "lo"):
        reply = start_music()

    elif cmd in ("next", "n"):
        reply = play_next()

    elif cmd in ("rand", "r"):
        S.play_mode = "rand"
        reply = "随机播放"
        save_state()

    elif cmd in ("seq", "se"):
        S.play_mode = "seq"
        reply = "顺序播放"
        save_state()

    elif cmd in ("rep", "re"):
        S.play_mode = "rep"
        reply = "单曲循环"
        save_state()

    elif cmd in ("scan", "s"):
        n = len(S.music_list)
        scan_music()
        reply = "扫描完成: %d -> %d 首" % (n, len(S.music_list))

    elif cmd == "rm":
        if len(parts) < 2 or not parts[1].isdigit():
            reply = "用法: /rm 3"
        else:
            idx = int(parts[1]) - 1
            if 0 <= idx < len(S.music_list):
                name = os.path.basename(S.music_list.pop(idx))
                if S.music_index >= idx and S.music_index > 0:
                    S.music_index -= 1
                reply = "删除: %s" % name
            else:
                reply = "序号超出范围"

    elif cmd in ("clear", "c"):
        n = len(S.music_list)
        S.music_list.clear()
        stop_all(save_pos=False)
        S.music_position = 0.0
        S.current_song_path = None
        save_state()
        reply = "清空列表 (%d首)" % n

    elif cmd in ("list", "ls"):
        pl = get_playlist()
        lines = ["=== 播放列表 (%d项) ===" % len(pl)]
        for i, it in enumerate(pl):
            tag = "电台" if it["type"] == "radio" else "音乐"
            mark = ""
            if S.mode == it["type"]:
                if it["type"] == "radio" and S.current_station == it["id"]:
                    mark = " (当前)"
                elif it["type"] == "music" and S.music_index == it["id"]:
                    mark = " (当前)"
            lines.append("  %d [%s] %s%s" % (i + 1, tag, it["name"], mark))
        reply = "\n".join(lines)

    elif cmd in ("z", "status"):
        reply = build_status()

    # ===== 记忆功能命令 =====
    elif cmd == "save":
        save_state(verbose=True)
        reply = "已保存当前状态（含播放位置 %.1fs）" % S.music_position

    elif cmd == "load":
        load_state()
        # 应用音量到 amixer
        v = int(S.play_volume * 100)
        subprocess.run(["amixer", "set", "Speaker", "%d%%" % v], capture_output=True)
        reply = "已重载状态: 音量 %d%%, 喇叭 %s, 增益 %.1fx" % (
            v, "开" if S.speaker_enabled else "关", S.mic_gain)

    elif cmd == "forget":
        # 清除记忆
        if os.path.exists(STATE_FILE):
            try:
                os.remove(STATE_FILE)
            except Exception:
                pass
        S.music_position = 0.0
        S.current_song_path = None
        reply = "已清除记忆（下次启动从默认状态开始）"

    elif cmd in ("add", "a"):
        if len(parts) < 2:
            reply = "用法: /add 名称 链接"
        elif len(parts) == 2:
            url = parts[1]
            name = re.sub(r'^https?://', '', url).split('/')[0]
            new_id = str(max([int(k) for k in STATIONS if k.isdigit()] + [0]) + 1)
            STATIONS[new_id] = {"name": name, "url": url}
            save_stations()
            reply = "添加: %s %s" % (new_id, name)
        else:
            name, url = parts[1], parts[2]
            new_id = str(max([int(k) for k in STATIONS if k.isdigit()] + [0]) + 1)
            STATIONS[new_id] = {"name": name, "url": url}
            save_stations()
            reply = "添加: %s %s" % (new_id, name)

    elif cmd in ("del", "d"):
        if len(parts) < 2 or parts[1] not in STATIONS:
            reply = "用法: /del 1"
        else:
            sid = parts[1]
            name = STATIONS[sid]["name"]
            del STATIONS[sid]
            save_stations()
            reply = "删除: %s" % name

    elif cmd in ("reset", "rs"):
        stop_all(save_pos=False)
        STATIONS = dict(DEFAULT_STATIONS)
        save_stations()
        reply = "已恢复默认央广电台"

    elif cmd in ("help", "h"):
        reply = (
            "=== 命令 ===\n"
            "数字           切换播放列表第N项\n"
            "/p            播放/继续/查看当前\n"
            "/t            停止\n"
            "/dj           对讲模式\n"
            "/z            查看运行状态\n"
            "/save         手动保存状态（含位置）\n"
            "/load         重载状态\n"
            "/forget       清除记忆\n"
            "/on /off      本地喇叭\n"
            "/mic + /mic - 话筒增益\n"
            "/vol 0-99     音量\n"
            "/bw + /bw -   带宽\n"
            "/lo           播放本地音乐\n"
            "/n            下一首\n"
            "/r /se /re    随机/顺序/单曲\n"
            "/s            重扫描音乐\n"
            "/rm 3         删除第3首\n"
            "/c            清空列表\n"
            "/add 名称 url  添加电台\n"
            "/del 1        删除电台\n"
            "/rs           恢复默认\n"
            "/ls           播放列表\n"
            "/h            本帮助"
        )
    else:
        return

    try:
        mumble.users[sender].send_text_message(reply.replace("\n", "<br/>"))
    except Exception:
        pass
    print("[%s] %s" % (user_name, text))
    for line in reply.split("\n"):
        print("    %s" % line)


# ==================== 主程序 ====================
def main():
    global mumble

    load_stations()
    load_state()
    scan_music()
    print("本地音乐目录: %s, 共%d首" % (MUSIC_DIR, len(S.music_list)))
    print("PTT 配置: device=%s, GPIO=%d, invert=%s" % (
        PTT_DEVICE, PTT_GPIO_PIN, PTT_INVERT))
    print("恢复参数: 音量=%d%%, 喇叭=%s, 增益=%.1fx, 模式=%s" % (
        int(S.play_volume * 100),
        "开" if S.speaker_enabled else "关",
        S.mic_gain, S.play_mode))

    # 启动时应用恢复的音量到 amixer
    v = int(S.play_volume * 100) if S.speaker_enabled else 0
    try:
        subprocess.run(["amixer", "set", "Speaker", "%d%%" % v],
                       capture_output=True, timeout=2)
    except Exception:
        pass

    mumble = pymumble_py3.Mumble(
        SERVER, NICK, port=PORT, password=PASSWORD,
        stereo=False,
    )
    mumble.callbacks.set_callback(PTX, on_text_received)
    mumble.callbacks.set_callback(PSND, on_sound_received)
    mumble.start()
    mumble.is_ready()

    if USE_CODEC_PROFILE:
        try:
            mumble.set_codec_profile("audio")
            print("[codec] set_codec_profile('audio') OK")
        except Exception as e:
            print("[codec] set_codec_profile 失败: %s" % e)

    if USE_SET_BANDWIDTH:
        try:
            mumble.set_bandwidth(TARGET_BANDWIDTH)
            print("[bw] set_bandwidth(%d) OK" % TARGET_BANDWIDTH)
        except Exception as e:
            print("[bw] set_bandwidth 失败: %s" % e)

    mumble.set_receive_sound(True)

    time.sleep(2)
    print("[check] 初始状态: %s" % get_encoder_info())

    ensure_unmuted()

    from pymumble_py3 import soundqueue
    for uid, u in mumble.users.items():
        if getattr(u, "sound", None) is None:
            try:
                u.sound = soundqueue.SoundQueue(mumble)
            except Exception:
                pass

    def fix_users():
        while True:
            for uid, u in mumble.users.items():
                if getattr(u, "sound", None) is None:
                    try:
                        u.sound = soundqueue.SoundQueue(mumble)
                    except Exception:
                        pass
            time.sleep(0.5)
    threading.Thread(target=fix_users, daemon=True).start()

    init_ptt()
    set_auto_ptt(True)

    print("Radio Bot 已连接 %s:%d (单声道/音乐模式)" % (SERVER, PORT))

    # 启动位置保存线程
    threading.Thread(target=position_saver_thread, daemon=True).start()

    threading.Thread(target=player_thread, daemon=True).start()

    time.sleep(1)

    # ===== 恢复上次播放 =====
    if S.mode == "radio" and S.current_station in STATIONS:
        print("恢复电台: %s %s" % (S.current_station, STATIONS[S.current_station]["name"]))
        start_station(S.current_station)
    elif S.mode == "music" and S.music_list:
        # 优先用记录的歌曲路径
        restore_path = None
        if S.current_song_path and os.path.exists(S.current_song_path):
            restore_path = S.current_song_path
            # 找到在列表里的索引
            try:
                S.music_index = S.music_list.index(restore_path)
            except ValueError:
                pass
        else:
            idx = min(S.music_index, len(S.music_list) - 1)
            restore_path = S.music_list[idx]
            S.music_index = idx

        start_pos = S.music_position if S.music_position > 0 else 0.0
        print("恢复音乐: %s (从 %s)" % (
            os.path.basename(restore_path), fmt_duration(start_pos)))
        play_song(restore_path, start_pos=start_pos, save_pos_before=False)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("退出中...")
        # 退出前保存
        save_state(verbose=True)
        stop_all(save_pos=False)
        stop_mic()
        ptt_off()
        if ptt:
            try:
                ptt.close()
            except Exception:
                pass
        print("已退出")


if __name__ == "__main__":
    main()
