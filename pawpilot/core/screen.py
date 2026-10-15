"""屏幕采集。

这里有两个坑,不处理就一定会对不准:

1. **DPI 缩放**。Windows 上如果进程不是 DPI-aware,系统会给它一套"虚拟"坐标,
   而截图拿到的却是物理像素,两者差一个 1.25/1.5/2 的系数。表现是
   "AI 明明说点这儿,实际点到了隔壁"。
   解决:启动时把自己提升为 Per-Monitor DPI Aware,让 pyautogui 看到的坐标系
   和 mss 截出来的像素严格一致。

2. **多显示器的原点偏移**。副屏的左上角未必是 (0,0),鼠标坐标要考虑偏移。
   我们把 mss 返回的 left/top 保存下来,换算时补回去。
"""
from __future__ import annotations

import ctypes
import io
import platform
from dataclasses import dataclass

from PIL import Image

# 模型侧能接受的最大边长。再大也只是白白烧 token,视觉模型内部照样会缩放。
MAX_IMAGE_EDGE = 1568
JPEG_QUALITY = 88


def setup_dpi_awareness() -> str:
    """在 Windows 上开启 Per-Monitor DPI Awareness。返回描述,便于排查环境差异。"""
    if platform.system() != "Windows":
        return "非 Windows 平台,跳过 DPI 设置"
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "已开启 Per-Monitor DPI Awareness"
    except Exception:
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "已开启 System DPI Awareness"
    except Exception:
        pass
    return "未能设置 DPI Awareness(通常无害,但在缩放屏上可能点击偏移)"


@dataclass
class Frame:
    """一帧截图 + 它的坐标系信息。"""

    image: Image.Image  # 送入模型的压缩图
    raw_image: Image.Image  # 原始分辨率
    view_width: int  # 模型所见图片的宽
    view_height: int  # 模型所见图片的高
    offset_x: int = 0  # 该屏左上角在全局坐标系中的位置
    offset_y: int = 0
    monitor_index: int = 1

    # --- 归一化坐标 -> 实际屏幕坐标 -------------------------------------
    def to_screen(self, nx: int | float, ny: int | float) -> tuple[int, int]:
        """把模型输出的 0~999 归一化坐标换算成真实鼠标坐标。

        之所以让模型输出归一化坐标,是因为各家模型内部都会先把图 resize 到自己的
        输入尺寸。给绝对像素,模型其实是在猜自己看到的那张图有多大;
        归一化坐标则与 resize 无关,鲁棒得多。OSWorld / Agent S 也都这么做。
        """
        x = float(nx) / 1000.0 * self.raw_image.width
        y = float(ny) / 1000.0 * self.raw_image.height
        return int(round(x + self.offset_x)), int(round(y + self.offset_y))


class ScreenCaptureError(Exception):
    """截图失败。常见原因写在 raised 消息里,帮使用者自己判断该怎么办。"""


class Screen:
    def __init__(self, monitor: int = 1) -> None:
        self.monitor = monitor
        self.dpi_note = setup_dpi_awareness()
        import mss

        # mss>=10 推荐 mss.MSS,老版本只有小写的 mss.mss,两个都留着
        factory = getattr(mss, "MSS", None) or mss.mss
        self._sct = factory()
        # mss 的 monitors[0] 是所有屏幕拼起来的虚拟桌面,[1:] 才是单屏
        self.monitors = self._sct.monitors
        if monitor >= len(self.monitors):
            raise ValueError(
                f"没有编号为 {monitor} 的显示器,当前只有 {len(self.monitors) - 1} 个。"
                f"用 --list-monitors 查看。"
            )

    def list_monitors(self) -> list[dict]:
        return [dict(m) for m in self.monitors[1:]]

    @property
    def current_box(self) -> dict:
        return dict(self.monitors[self.monitor])

    def capture(self) -> Frame:
        """抓当前帧。返回压缩后的图和原始图(坐标换算要基于原始分辨率)。"""
        box = self.monitors[self.monitor]
        try:
            shot = self._sct.grab(box)
        except Exception as e:
            raise ScreenCaptureError(
                f"截不了图:{e}\n"
                " 常见原因:\n"
                " - 屏幕已锁定,或者正在没有桌面的会话里运行(如某些服务 / 沙箱 / CI)\n"
                " - 远程桌面已断开\n"
                " - 系统禁止本进程访问图形设备\n"
                "请在有图形桌面的普通终端里重试。"
            ) from e
        raw = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        view = raw.copy()
        view.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
        return Frame(
            image=view,
            raw_image=raw,
            view_width=view.width,
            view_height=view.height,
            offset_x=int(box.get("left", 0)),
            offset_y=int(box.get("top", 0)),
            monitor_index=self.monitor,
        )

    @staticmethod
    def to_base64(frame: Frame) -> str:
        import base64

        buf = io.BytesIO()
        frame.image.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception:
            pass
