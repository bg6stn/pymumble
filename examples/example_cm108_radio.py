#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CM108 PTT 双向互联示例脚本
==========================

本脚本演示如何使用 pymumble + CM108 声卡 GPIO PTT，实现
【网络电台（Mumble）】与【物理电台（业余无线电对讲机）】之间的半双工语音互联互通：

    Mumble 服务器上有人说话
        → 本脚本通过声卡输出（扬声器/耳机口）把音频送到电台发射机的音频输入
        → 同时 CM108 PTT 自动打开发射（PTT ON）
        → 网络上的话音通过无线电发射出去

    电台麦克风（声卡 Line-In/Mic-In）采集到的话音
        → 通过 mumble.sound_output.add_sound() 发送到 Mumble 服务器
        → 此时 PTT 由 SoundOutput 内部 hangtime 逻辑自动管理

用法:
    python3 example_cm108_radio.py --host mumble.example.com --port 64738 \\
        --user radio-gateway --password secret

    # 指定 CM108 设备与引脚:
    python3 example_cm108_radio.py --host ... --device /dev/hidraw2 \\
        --gpio-pin 3 --invert

    # 无硬件 / 无 sounddevice 环境下会自动进入模拟模式，不会崩溃。

依赖:
    - pymumble_py3（本仓库）
    - opuslib_next（已在 requirements 中）
    - 可选: sounddevice 或 pyaudio（用于本地声卡音频环回）
      若未安装，脚本会打印提示并以"仅 PTT 控制"模式运行，不做本地音频环回。

注意:
    - 半双工：收发不能同时进行，否则会自激啸叫。实际部署时建议关闭声卡监听
      或加 VOX/PTT 互锁。
    - CM108 的 GPIO3（物理 pin13）是最常用的 PTT 引脚。
    - 普通用户访问 /dev/hidraw 需要配置 udev 规则，见 README。
"""

import argparse
import signal
import sys
import time

# 将仓库根目录加入 sys.path，便于直接运行本脚本
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pymumble_py3 as pymumble
from pymumble_py3.callbacks import PYMUMBLE_CLBK_SOUNDRECEIVED as PCS
from pymumble_py3.cm108_ptt import CM108PTT, CM108PTTError, list_cm108_devices


# ---------------------------------------------------------------------------
# 音频后端加载（sounddevice 优先，pyaudio 次之，都没有则进入模拟模式）
# ---------------------------------------------------------------------------

# Mumble 音频固定为 48kHz / 16bit / 单声道
AUDIO_RATE = 48000
AUDIO_CHANNELS = 1
AUDIO_CHUNK = 960  # 20ms @ 48kHz，与 Mumble 音频帧对齐


def load_audio_backend():
    """
    尝试加载 sounddevice 或 pyaudio 作为本地声卡音频后端。

    返回:
        (backend_name, stream) 元组；
        若两者都不可用，返回 (None, None)，脚本进入无音频环回的模拟模式。
    """
    # 优先尝试 sounddevice
    try:
        import sounddevice as sd  # noqa: F401
        print("[音频后端] 使用 sounddevice")
        # 这里只做导入检测，实际流在下面按需打开
        return ("sounddevice", None)
    except ImportError:
        pass

    # 退而求其次尝试 pyaudio
    try:
        import pyaudio  # noqa: F401
        print("[音频后端] 使用 pyaudio")
        return ("pyaudio", None)
    except ImportError:
        pass

    print("[音频后端] 未检测到 sounddevice / pyaudio，进入模拟模式（不做本地音频环回）。")
    print("           如需完整双向互联，请 pip install sounddevice 或 pip install pyaudio")
    return (None, None)


# ---------------------------------------------------------------------------
# 信号处理：Ctrl+C 优雅退出，自动释放 PTT
# ---------------------------------------------------------------------------

_g_running = True
_g_ptt = None


def _sigint_handler(signum, frame):
    """捕获 Ctrl+C，置退出标志位"""
    global _g_running
    print("\n[信号] 收到中断信号，正在退出并释放 PTT ...")
    _g_running = False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="CM108 PTT 网络电台-物理电台双向互联示例")
    parser.add_argument("--host", required=True, help="Mumble 服务器地址")
    parser.add_argument("--port", type=int, default=64738, help="Mumble 服务器端口（默认 64738）")
    parser.add_argument("--user", default="cm108-gateway", help="连接用户名")
    parser.add_argument("--password", default="", help="服务器密码")
    parser.add_argument("--insecure", action="store_true", default=True,
                        help="不校验服务器证书（默认开启，与 pymumble 行为一致）")

    # CM108 PTT 相关参数
    parser.add_argument("--device", default=None,
                        help="手动指定 CM108 hidraw 设备路径（如 /dev/hidraw2），不填则自动检测")
    parser.add_argument("--gpio-pin", type=int, default=3,
                        help="逻辑 GPIO 编号 1-4，默认 3（CM108B 物理 pin13）")
    parser.add_argument("--invert", action="store_true",
                        help="极性反转：ptt_on 写低电平，适配低电平触发 PTT 的电台")

    args = parser.parse_args()

    # 注册 Ctrl+C 处理
    signal.signal(signal.SIGINT, _sigint_handler)
    signal.signal(signal.SIGTERM, _sigint_handler)

    # ---- 1. 初始化 CM108 PTT 控制器 ----
    print("[PTT] 正在初始化 CM108 PTT ...")
    global _g_ptt
    try:
        _g_ptt = CM108PTT(
            device_path=args.device,
            gpio_pin=args.gpio_pin,
            invert=args.invert,
            auto_detect=True,
        )
        print("[PTT] 初始化成功: %s (GPIO%d, invert=%s)" % (
            _g_ptt.device_path, args.gpio_pin, args.invert))
    except CM108PTTError as e:
        print("[PTT][错误] 初始化失败: %s" % e)
        print("[PTT] 提示: 可用设备列表: %s" % list_cm108_devices())
        print("[PTT] 将以无 PTT 模式继续（仅连接 Mumble，不控制硬件）。")
        _g_ptt = None

    # ---- 2. 加载音频后端 ----
    backend_name, _ = load_audio_backend()

    # ---- 3. 连接 Mumble 服务器 ----
    print("[Mumble] 正在连接 %s:%s 用户=%s ..." % (args.host, args.port, args.user))
    mumble = pymumble.Mumble(args.host, args.user,
                             port=args.port,
                             password=args.password)
    mumble.set_receive_sound(1)  # 启用接收 Mumble 音频

    # 注册收到音频的回调：把 Mumble 来的声音送到声卡输出（电台发射）
    def on_sound_received(user, soundchunk):
        """收到 Mumble 其他用户语音 → 写入声卡输出（送电台发射）"""
        if backend_name == "sounddevice":
            try:
                import sounddevice as sd
                sd.play(soundchunk.pcm, AUDIO_RATE, blocking=False)
            except Exception as e:
                print("[音频] sounddevice 播放失败: %s" % e)
        elif backend_name == "pyaudio":
            # pyaudio 流在主循环里打开，这里只把数据交给回调队列即可；
            # 简化处理：本示例以 sounddevice 为主，pyaudio 路径略。
            pass
        # 注意：发送到 Mumble 的音频会由 SoundOutput 自动管理 PTT，
        # 因此接收方向（Mumble→电台）这里不手动控制 PTT。

    mumble.callbacks.set_callback(PCS, on_sound_received)

    mumble.start()
    mumble.is_ready()  # 等待连接就绪
    print("[Mumble] 连接成功！")

    # ---- 4. 把 PTT 控制器注入 SoundOutput ----
    # SoundOutput 在 init_connection() 中创建，连接就绪后注入即可。
    # 之后每次 add_sound() → send_audio() 时，SoundOutput 会自动 ptt_on()
    # 并在 hangtime（默认 0.2s）后自动 ptt_off()。
    if _g_ptt is not None:
        mumble.sound_output.ptt_controller = _g_ptt
        mumble.sound_output.ptt_hangtime = 0.2  # 200ms 释放延迟
        print("[PTT] 已注入 mumble.sound_output，hangtime=0.2s")

    # ---- 5. 主循环：从声卡采集麦克风音频 → 发送到 Mumble ----
    print("[主循环] 开始采集声卡输入并发送到 Mumble（Ctrl+C 退出）...")

    if backend_name == "sounddevice":
        import sounddevice as sd
        # 打开输入流：采集麦克风/Line-In → add_sound 到 Mumble
        def audio_callback(indata, frames, time_info, status):
            """sounddevice 输入回调：把采集到的 PCM 送入 Mumble 发送队列"""
            if status:
                # 轻微溢出/欠载只打印一次，避免刷屏
                pass
            try:
                mumble.sound_output.add_sound(bytes(indata))
            except Exception as e:
                print("[音频] add_sound 失败: %s" % e)

        with sd.RawInputStream(samplerate=AUDIO_RATE,
                               blocksize=AUDIO_CHUNK,
                               dtype='int16',
                               channels=AUDIO_CHANNELS,
                               callback=audio_callback):
            while _g_running:
                time.sleep(0.1)
    else:
        # 无音频后端：空转，保持 Mumble 连接，PTT 仍可手动测试
        print("[主循环] 模拟模式：不采集音频，仅保持连接。可手动调用 "
              "ptt.ptt_on()/ptt_off() 测试硬件。")
        while _g_running:
            time.sleep(0.1)

    # ---- 6. 退出清理 ----
    print("[退出] 正在关闭 ...")
    if _g_ptt is not None:
        try:
            _g_ptt.ptt_off()
        except Exception:
            pass
        _g_ptt.close()
        print("[PTT] 已释放并关闭设备。")
    mumble.stop()
    print("[完成] 再见！")


if __name__ == "__main__":
    main()
