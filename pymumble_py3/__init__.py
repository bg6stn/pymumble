# -*- coding: utf-8 -*-

from .mumble import Mumble

# 可选导出 CM108 PTT 控制类（纯标准库实现，导入不应失败；
# 用 try/except 兜底，避免在极端环境下影响 Mumble 主类导入）
try:
    from .cm108_ptt import CM108PTT, list_cm108_devices
except ImportError:
    pass
