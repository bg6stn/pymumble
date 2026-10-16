# -*- coding: utf-8 -*-
"""
CM108 系列 USB 声卡 HID GPIO PTT 控制模块
=========================================

本模块通过 Linux 的 /dev/hidraw* 字符设备直接写入 5 字节 HID 输出报告，
实现对 CM108 / CM108B / CM108AH / AIOC 等 USB 声卡芯片 GPIO 引脚的电平控制，
从而触发电台的 PTT（Push-To-Talk，发射）按键。

技术要点：
    - 不依赖 hidapi / pyhidapi 等第三方库，仅使用 Python 标准库；
    - HID 输出报告固定为 5 字节：[0x00, 0x00, iomask, iodata, 0x00]
    - iomask = 1 << (gpio_pin - 1)，即把目标 GPIO 对应位设为输出方向
    - iodata = state << (gpio_pin - 1)，state=1 输出高电平，state=0 输出低电平
    - 默认 gpio_pin=3，对应 CM108B 芯片物理引脚 13，是业余无线电最常用的 PTT 引脚
    - 设备自动检测：遍历 /dev/hidraw*，读取
      /sys/class/hidraw/hidrawX/device/uevent 中的 USB_VENDOR_ID / USB_PRODUCT_ID
      匹配 C-Media 0x0d8c 或 AIOC 1209:7388

典型用途：
    与 pymumble 结合，实现网络电台（Mumble）与物理电台（业余无线电对讲机）
    之间的半双工语音互联互通：Mumble 上有人说话时，自动打开发射，
    说完后延迟一小段时间（hangtime）再关闭发射。

作者: BG6STN
"""

import os
import glob
import time
import fcntl
import logging

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

#: CM108 系列常见的 USB VID:PID（十六进制字符串，大写）
#: C-Media 原厂芯片 VID=0d8c，覆盖 CM108 / CM108B / CM108AH / CM119 等
#: AIOC 设备（业余无线电常用的开源声卡）VID:PID=1209:7388
CM108_KNOWN_VID_PID = {
    ("0D8C", None),      # C-Media 任意 PID（CM108 系列通常为 000C/013A/013D 等）
    ("1209", "7388"),    # AIOC 设备
}

#: HID 输出报告固定长度（字节）
HID_REPORT_LEN = 5

#: 写入失败时的重试次数
WRITE_RETRY = 2


class CM108PTTError(Exception):
    """CM108 PTT 控制相关异常基类"""
    pass


class CM108DeviceNotFound(CM108PTTError):
    """未找到 CM108 设备"""
    pass


class CM108PermissionError(CM108PTTError):
    """打开 /dev/hidraw 设备权限不足"""
    pass


class CM108WriteError(CM108PTTError):
    """向 hidraw 设备写入 HID 报告失败"""
    pass


def _read_uevent_vidpid(hidraw_path):
    """
    读取指定 hidraw 设备对应的 uevent 文件，解析出 USB_VENDOR_ID 与 USB_PRODUCT_ID。

    参数:
        hidraw_path: /dev/hidrawX 的绝对路径

    返回:
        (vid, pid) 元组，均为大写十六进制字符串（不带 0x 前缀）；
        若不是 USB 设备或读取失败，返回 (None, None)。
    """
    # 由 /dev/hidrawX 反推 sysfs 路径：
    #   /sys/class/hidraw/hidrawX/device/uevent
    device_name = os.path.basename(hidraw_path)
    uevent_path = "/sys/class/hidraw/{name}/device/uevent".format(name=device_name)

    vid = None
    pid = None
    try:
        with open(uevent_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("USB_VENDOR_ID="):
                    vid = line.split("=", 1)[1].strip().upper()
                elif line.startswith("USB_PRODUCT_ID="):
                    pid = line.split("=", 1)[1].strip().upper()
    except (IOError, OSError):
        # sysfs 读取失败（权限/不存在），当作非 CM108 设备处理
        return (None, None)

    return (vid, pid)


def _is_cm108_vidpid(vid, pid):
    """
    判断给定的 VID:PID 是否属于已知的 CM108 系列设备。

    匹配规则：
        - VID 命中 0D8C（C-Media），任意 PID 均视为 CM108 系列；
        - VID:PID 精确命中 1209:7388（AIOC）。
    """
    if not vid:
        return False
    for known_vid, known_pid in CM108_KNOWN_VID_PID:
        if vid == known_vid:
            if known_pid is None or pid == known_pid:
                return True
    return False


def list_cm108_devices():
    """
    扫描系统中所有 /dev/hidraw* 设备，返回识别为 CM108 系列的设备路径列表。

    返回:
        list[str]: 形如 ['/dev/hidraw0', '/dev/hidraw2'] 的路径列表；
                   未找到任何设备时返回空列表。
    """
    found = []
    for path in sorted(glob.glob("/dev/hidraw*")):
        vid, pid = _read_uevent_vidpid(path)
        if _is_cm108_vidpid(vid, pid):
            found.append(path)
    return found


class CM108PTT(object):
    """
    CM108 系列 USB 声卡 GPIO PTT 控制器。

    通过向 /dev/hidrawX 写入 5 字节 HID 输出报告，控制指定 GPIO 引脚电平，
    从而驱动外接电台的 PTT 按键。支持自动设备发现、极性反转、上下文管理器。

    使用示例::

        ptt = CM108PTT()           # 自动检测设备，默认 GPIO3
        ptt.ptt_on()               # 打开发射
        time.sleep(1.0)
        ptt.ptt_off()              # 关闭发射
        ptt.close()

        # 或使用上下文管理器（退出时自动释放 PTT 并关闭设备）
        with CM108PTT(gpio_pin=3, invert=True) as ptt:
            ptt.ptt_on()
            time.sleep(0.5)
    """

    def __init__(self, device_path=None, gpio_pin=3, invert=False, auto_detect=True):
        """
        初始化 CM108 PTT 控制器。

        参数:
            device_path: 手动指定 hidraw 设备路径（如 '/dev/hidraw2'）。
                         为 None 时根据 auto_detect 决定是否自动扫描。
            gpio_pin:    逻辑 GPIO 编号，范围 1-4，默认 3。
                         对应关系（CM108B）：
                             GPIO1 -> 物理 pin 11
                             GPIO2 -> 物理 pin 12（CM108AH/B 不可用）
                             GPIO3 -> 物理 pin 13（最常用 PTT）
                             GPIO4 -> 物理 pin 14
            invert:      极性反转。
                         False（默认）：ptt_on 输出高电平，ptt_off 输出低电平；
                         True：         ptt_on 输出低电平，ptt_off 输出高电平。
                         适用于部分低电平触发 PTT 的电台。
            auto_detect: 当 device_path 为 None 时，是否自动扫描 /dev/hidraw*。
        """
        if gpio_pin < 1 or gpio_pin > 4:
            raise ValueError("gpio_pin 必须在 1-4 之间，当前为: %r" % gpio_pin)

        self.device_path = device_path
        self.gpio_pin = gpio_pin
        self.invert = invert
        self.auto_detect = auto_detect

        self._fd = None                 # os.open 返回的文件描述符
        self._ptt_active = False        # 当前 PTT 是否处于按下状态
        self._log = logging.getLogger("CM108PTT")

        # 位掩码：GPIO1 对应 bit0，GPIO2 对应 bit1，依此类推
        self._bit = 1 << (gpio_pin - 1)

        # 打开设备
        self._open_device()

    # ------------------------------------------------------------------
    # 设备打开 / 关闭 / 重连
    # ------------------------------------------------------------------

    def _open_device(self):
        """
        根据 device_path / auto_detect 打开 hidraw 设备文件。
        使用 os.open（带 O_WRONLY）而非 open()，避免缓冲，保证写入即时生效。
        """
        # 如果用户没有手动指定路径，则自动检测
        if self.device_path is None:
            if not self.auto_detect:
                raise CM108DeviceNotFound(
                    "未指定 device_path 且 auto_detect=False，无法定位 CM108 设备")
            devices = list_cm108_devices()
            if not devices:
                raise CM108DeviceNotFound(
                    "未在 /dev/hidraw* 中检测到 CM108 系列设备。"
                    "请确认 USB 声卡已插入，并配置了 udev 规则。")
            self.device_path = devices[0]
            if len(devices) > 1:
                self._log.warning("检测到多个 CM108 设备: %s，使用第一个: %s",
                                 devices, self.device_path)

        # 以只写、非阻塞方式打开
        try:
            self._fd = os.open(self.device_path, os.O_WRONLY)
        except PermissionError as e:
            raise CM108PermissionError(
                "打开 %s 权限不足: %s。请配置 /etc/udev/rules.d/99-cm108.rules "
                "或将当前用户加入 plugdev 组。" % (self.device_path, e))
        except FileNotFoundError as e:
            raise CM108DeviceNotFound(
                "hidraw 设备不存在: %s (%s)" % (self.device_path, e))
        except OSError as e:
            raise CM108PTTError(
                "打开 %s 失败: %s" % (self.device_path, e))

        # 打开后默认释放 PTT（输出低电平，或 invert 时输出高电平）
        self._log.info("CM108 PTT 设备已打开: %s (GPIO%d, invert=%s)",
                       self.device_path, self.gpio_pin, self.invert)
        try:
            self.ptt_off()
        except CM108WriteError as e:
            self._log.warning("初始化时释放 PTT 失败: %s", e)

    def reconnect(self):
        """
        重新连接设备。适用于 USB 设备被拔出后重新插入的场景。
        会先关闭旧 fd（如有），再重新打开。
        """
        self.close()
        self._open_device()

    def close(self):
        """
        关闭设备文件描述符。关闭前会先释放 PTT，确保电台不会卡在发射状态。
        """
        if self._fd is not None:
            # 先尝试释放 PTT，避免关设备时电台卡在发射
            try:
                if self._ptt_active:
                    self._write_gpio(0)
            except Exception:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._ptt_active = False

    # ------------------------------------------------------------------
    # GPIO 写入核心
    # ------------------------------------------------------------------

    def _build_report(self, level):
        """
        根据目标电平构造 5 字节 HID 输出报告。

        报告格式: [0x00, 0x00, iomask, iodata, 0x00]
            - 第 1 字节 0x00: Report ID（CM108 通常为 0）
            - 第 2 字节 0x00: 保留
            - 第 3 字节 iomask: 方向掩码，置 1 的位表示对应 GPIO 为输出
            - 第 4 字节 iodata: 输出数据，置 1 的位对应 GPIO 输出高电平
            - 第 5 字节 0x00: 保留

        参数:
            level: 1 表示输出高电平，0 表示输出低电平（未考虑 invert）

        返回:
            bytes: 长度为 5 的字节串
        """
        iomask = self._bit
        iodata = (level & 0x01) << (self.gpio_pin - 1)
        return bytes([0x00, 0x00, iomask & 0xFF, iodata & 0xFF, 0x00])

    def _write_gpio(self, level):
        """
        向 hidraw 设备写入一次 GPIO 输出报告。

        参数:
            level: 1=高电平，0=低电平（已考虑 invert 后的最终电平）

        异常:
            CM108WriteError: 写入失败（重试后仍失败）
        """
        if self._fd is None:
            raise CM108WriteError("设备未打开，无法写入 GPIO")

        report = self._build_report(level)
        last_err = None
        for _ in range(WRITE_RETRY + 1):
            try:
                written = os.write(self._fd, report)
                if written == HID_REPORT_LEN:
                    return
                last_err = "写入字节数 %d != %d" % (written, HID_REPORT_LEN)
            except OSError as e:
                last_err = str(e)
                # 设备可能被拔出，尝试不在这里自动重连，由上层决定
            time.sleep(0.01)
        raise CM108WriteError(
            "向 %s 写入 HID 报告失败: %s" % (self.device_path, last_err))

    # ------------------------------------------------------------------
    # 对外 PTT 控制接口
    # ------------------------------------------------------------------

    def ptt_on(self):
        """
        按下 PTT（打开发射）。
        根据 invert 参数决定实际输出电平：
            invert=False: 输出高电平
            invert=True:  输出低电平
        """
        # 最终电平：未反转时 1=高；反转时 0=低
        level = 0 if self.invert else 1
        self._write_gpio(level)
        self._ptt_active = True
        self._log.debug("PTT ON (level=%d)", level)

    def ptt_off(self):
        """
        释放 PTT（关闭发射）。
        根据 invert 参数决定实际输出电平：
            invert=False: 输出低电平
            invert=True:  输出高电平
        """
        level = 1 if self.invert else 0
        self._write_gpio(level)
        self._ptt_active = False
        self._log.debug("PTT OFF (level=%d)", level)

    def is_ptt_active(self):
        """
        返回当前 PTT 是否处于按下（发射中）状态。

        返回:
            bool: True=正在发射，False=已释放
        """
        return self._ptt_active

    # ------------------------------------------------------------------
    # 上下文管理器协议
    # ------------------------------------------------------------------

    def __enter__(self):
        """进入 with 块，返回 self 以便调用方使用"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出 with 块时，无论是否发生异常，都先释放 PTT 再关闭设备"""
        try:
            self.ptt_off()
        except Exception:
            pass
        self.close()
        return False  # 不吞异常
