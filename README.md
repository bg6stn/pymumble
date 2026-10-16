# Important Announcement
Hello everyone,
I don't know what to do with this project.

First, let's look at the problems and possible solutions:
1. I don't use mumble anymore, working on a bot you don't use leads to a lack of testing and motivation.
2. I don't code like I used to, my hobbies have changed, I maintain stuff I still use, but no real coding anymore.
3. pymumble isn't asynchronous. Many workarounds/patches were used to avoid freezing. Even now the latency increase with connexion time because of this.

Even if the asyncio needs a complete rewrite, I'm looking for maintainers.

Mumble teams is working on a mumble librairy, I hope it will replace this projet.
Until that time, I don't archive this project.

It was really fun, thank you all for your support!

See you in space, cowboy.

-- Azlux

-----

# PYMUMBLE python library

## Description
This library acts as a mumble client, connecting to a murmur server, exchanging states and audio.

[![Build Status](https://ci.azlux.fr/api/badges/azlux/pymumble/status.svg)](https://ci.azlux.fr/azlux/pymumble)

The wiki/API explanation is [HERE](https://github.com/azlux/pymumble/blob/pymumble_py3/API.md).

## Installing/Getting started

### Requirements

**`libopus` is a mandatory OS library when sending and receiving audio**. Please refer to your package manager to install it.

### With pip

- `pip install pymumble`
- You need to `import pymumble_py3 as pymumble` into your code.

[![PyPI version](https://badge.fury.io/py/pymumble.svg)](https://badge.fury.io/py/pymumble)   Deployment script is available [here](https://packages.azlux.fr/scripts/pymumble.txt)

### With git

- `git clone https://github.com/azlux/pymumble.git`
- `pip3 install -r requirements.txt`
- You need to `import pymumble.pymumble_py3 as pymumble` into your code.
- It's will be the same if you use a git sub-module

## CHANGELOG

The changelog is available on the release note.

## Applications list using `pymumble`

For client application examples, you can check this list :
- [Botamusique](https://github.com/azlux/botamusique)
- [MumbleRadioPlayer](https://github.com/azlux/MumbleRadioPlayer) (archived)
- [Abot](https://github.com/ranomier/pymumble-abot)
- [MumbleRecbot](https://github.com/Robert904/mumblerecbot) (deprecated)

## Features

### Currently implemented:
- Compatible with Mumble 1.3 and normally until 1.2.2
- Support OPUS. Speex is not supported
- Receive and send audio, get users and channels status
- Set properties for users (mute, comments, etc.) and go to a specific channel
- Kick and ban users
- Callback mechanism to react on server events
- Manage the blobs (images, long comments, etc.)
- Can send text messages to user and channel
- Ping statistics
- Audio targets (whisper, etc.)
- Read ACL groups
- **CM108 声卡 GPIO PTT 控制**（本 fork 新增，见下方「CM108 PTT 控制」章节）

### 服务器版本兼容性

本库协议版本号固定为 `(1, 4, 287)`（对应 Mumble 1.4.287），**实测可连接
Mumble 1.3.x ~ 1.6.x 服务器（TCP 模式）**。

- Mumble 1.5 起，UDP 直连音频改用 Protobuf 格式（`MumbleUDP.proto`），
  并只保留 Opus 编码。
- 本库当前 `udp_active=False`，所有音频均通过 **TCP UDPTunnel** 传输，
  因此不受 Mumble 1.5 Protobuf UDP 变化影响。
- Mumble 1.5+ 的 Protobuf UDP 音频模式暂未实现，作为未来扩展方向
  （`pymumble_py3/MumbleUDP.proto` 已作为参考保留）。

### What is missing:

>  I don't need these features, so if you want one, open an issue and I will work on it.

- UDP media. Currently it works only in TCP tunneling mode (the standard fallback of Mumble when UDP is not working)
- Some server management features (user creation, editing ACLs, etc.)
- Positioning is not managed, but it should be easy to add
- Probably a lot of other small features
- **WONTFIX** The **Python 2** version is available in the [master branch](https://github.com/azlux/pymumble/tree/master). It's working! But since we have moved on to Python 3, the Python 2 version will not receive future improvements.

## Architecture

The library is based on the Mumble object, which a thread. When started, it will try
to connect to the server and start exchanging the connection messages.
This thread implements a loop which takes care of the pings, sends commands to the server,
checks for incoming messages including audio, and checks for audio to be sent out.
The rate of this loop is controlled by how long it will wait for an incoming message before continuing.

You can check if the thread is alive with `mumble_object.is_alive()`.
The Mumble thread will stop if it disconnects from the server.
This can be useful if you need to restart the thread when using a supervisor.

## License
See the [License GPL file](./LICENSE)
If needed, Pymumble can be used and ship with another (permissive) license like the MIT or BSD one.

## Thanks

- [@raylu](https://github.com/raylu) for making `pymumble` speak into channels
- [@schlarpc](https://github.com/schlarpc) for fixes on buffer
- [@Robert904](https://github.com/Robert904) for the inital pymumble implementation

This library is a fork of a fork of a fork (initial from https://github.com/Robert904/pymumble).
But we will try to make `pymumble` better.
So I consider this fork (the [@Azlux](https://github.com/azlux/pymumble) one) the current live fork of `pymumble`.

## CM108 PTT 控制

本 fork 新增了对 CM108 系列 USB 声卡 GPIO 的 PTT（Push-To-Talk，发射）控制能力，
用于把 pymumble 变成一个网络电台（Mumble）↔ 物理电台（业余无线电对讲机）的
半双工语音网关。

### 功能简介

- 通过 Linux `/dev/hidraw*` 字符设备直接写入 5 字节 HID 输出报告控制 GPIO 电平
- 纯 Python 标准库实现，**无需 hidapi / pyhidapi 等第三方依赖**
- 自动扫描 `/dev/hidraw*`，通过 sysfs `uevent` 识别 C-Media `0d8c` 与 AIOC `1209:7388`
- 支持极性反转（`invert=True`），适配低电平触发 PTT 的电台
- 可注入 `SoundOutput`：有音频要发时自动打开发射，发完延迟 `hangtime`（默认 200ms）后自动释放

### 依赖说明

- Python 标准库即可，**不需要任何额外第三方库**
- Linux 系统需有权限访问 `/dev/hidraw*` 设备文件（见下方 udev 规则）
- 仅支持 Linux（依赖 `/dev/hidraw` 与 sysfs）

### udev 规则配置

普通用户默认无法直接读写 `/dev/hidraw*`。新建 `/etc/udev/rules.d/99-cm108.rules`：

```udev
# /etc/udev/rules.d/99-cm108.rules
# 允许普通用户访问 CM108 系列 USB 声卡的 hidraw 设备（用于 PTT 控制）

# C-Media CM108 / CM108B / CM108AH / CM119 系列
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0d8c", MODE="0660", GROUP="plugdev", TAG+="uaccess"

# AIOC 设备（VID:PID = 1209:7388）
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="7388", MODE="0660", GROUP="plugdev", TAG+="uaccess"
```

执行后重新加载规则并重插 USB 声卡：

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

确认当前用户在 `plugdev` 组：`sudo usermod -aG plugdev $USER`（需重新登录生效）。

### 硬件接线说明

#### CM108B GPIO 物理引脚对照表

| 逻辑 GPIO | CM108B 物理引脚 | 备注 |
|-----------|------------------|------|
| GPIO1     | pin 11           | 可用作 PTT |
| GPIO2     | pin 12           | CM108AH / CM108B 上不可用 |
| GPIO3     | **pin 13**       | **最常用 PTT 引脚（默认）** |
| GPIO4     | pin 14           | 可用作 PTT |

#### 接线

1. **CM108B 物理引脚 13 (GPIO3) → 电台 PTT 输入（PTT 键接点）**
2. **CM108 声卡 GND ↔ 电台 GND 共地**（必须共地，否则电平不确定）
3. 声卡扬声器输出 → 电台发射机音频输入（Disc/MIC 口）
4. 电台接收音频 → 声卡 Line-In/Mic-In

> **极性说明**：默认 `invert=False`，PTT ON 时 GPIO 输出高电平。
> 部分电台要求低电平触发 PTT，请在初始化时传 `invert=True`。

> **电平注意**：CM108 GPIO 输出为 3.3V 逻辑电平，若电台 PTT 输入是 5V TTL
> 或需要隔离，建议用光耦/三极管做电平转换与电气隔离（参考 BG6STN 常用方案）。

### 代码使用示例

```python
from pymumble_py3.cm108_ptt import CM108PTT

# 自动检测设备，使用默认 GPIO3（CM108B pin13）
ptt = CM108PTT()

# 手动控制
ptt.ptt_on()    # 打开发射
# ... 说话 ...
ptt.ptt_off()   # 关闭发射

# 传入 Mumble 的 sound_output（自动 PTT + hangtime）
import pymumble_py3 as pymumble
mumble = pymumble.Mumble("radio.example.com", "gateway", port=64738)
mumble.start()
mumble.is_ready()

# 注入 PTT 控制器后，add_sound() 时自动 ptt_on()，
# 音频发完 0.2s 后自动 ptt_off()
mumble.sound_output.ptt_controller = ptt
mumble.sound_output.ptt_hangtime = 0.2

# 上下文管理器用法（退出自动释放 PTT + 关闭设备）
with CM108PTT(gpio_pin=3, invert=True) as ptt2:
    ptt2.ptt_on()
    # ...
```

### 示例脚本运行

```bash
# 自动检测 CM108 设备
python3 examples/example_cm108_radio.py \
    --host mumble.example.com --port 64738 \
    --user radio-gateway --password secret

# 手动指定设备与引脚、反转极性
python3 examples/example_cm108_radio.py \
    --host mumble.example.com --port 64738 \
    --device /dev/hidraw2 --gpio-pin 3 --invert
```

无硬件环境下脚本会打印清晰提示并进入模拟模式，不会崩溃。

### 注意事项

- **半双工**：网络电台 ↔ 物理电台是半双工链路，收发不要同时进行，否则会自激啸叫。
- **避免自激**：建议关闭声卡监听（monitor），或在发射方向加 VOX/PTT 互锁；
  接收方向（Mumble→电台）不要与采集方向（电台→Mumble）同时打开。
- **hangtime 调整**：`ptt_hangtime` 默认 200ms，太短会截断话音尾音，太长会浪费
  信道；一般 150~300ms 即可。
- **紧急释放**：`CM108PTT.close()` 与上下文管理器 `__exit__` 都会先 `ptt_off()`
  再关设备，确保 Ctrl+C 退出时电台不会卡在发射状态。
