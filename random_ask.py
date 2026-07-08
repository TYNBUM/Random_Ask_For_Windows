"""随问 (random ask), a Windows floating screen-region question-answer tool.

Run this script, click the small always-on-top bubble, drag a region on screen,
then send the crop to an OpenAI-compatible vision model.
"""

from __future__ import annotations

import base64
import ctypes
import io
import math
import os
import queue
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageDraw, ImageGrab, ImageTk
from openai import OpenAI

try:
    import pystray
except ImportError:  # pragma: no cover - installed via requirements
    pystray = None

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency guard
    load_dotenv = None


APP_TITLE = "随问 (random ask)"
DEFAULT_IMAGE_QUESTION = (
    "请识别截图中被框选的内容，并直接解答其中的问题或说明关键点。"
    "如果是代码/报错，请给出原因和可执行的修复建议。"
)
DEFAULT_TEXT_QUESTION = (
    "请阅读下面这段文本，并直接解答其中的问题或提炼需要的结论。"
)
SYSTEM_PROMPT = (
    "你是一个桌面批注问答助手。用户会发送一张屏幕截图或一段文本。"
    "优先识别用户框选区域中的题目、代码、报错、网页内容或文档内容，"
    "用中文给出简洁、准确、可执行的回答。"
)


def make_process_dpi_aware() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def is_left_mouse_down() -> bool:
    if os.name != "nt":
        return False
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)


def get_clipboard_sequence_number() -> int:
    if os.name != "nt":
        return 0
    return int(ctypes.windll.user32.GetClipboardSequenceNumber())


def send_ctrl_c() -> None:
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    vk_control = 0x11
    vk_c = 0x43
    keyeventf_keyup = 0x0002
    user32.keybd_event(vk_control, 0, 0, 0)
    user32.keybd_event(vk_c, 0, 0, 0)
    user32.keybd_event(vk_c, 0, keyeventf_keyup, 0)
    user32.keybd_event(vk_control, 0, keyeventf_keyup, 0)


THEME = {
    "transparent": "#ff00ff",
    "panel": "#070914",
    "panel_deep": "#02040a",
    "panel_2": "#111827",
    "panel_3": "#182033",
    "line": "#00f5ff",
    "line_2": "#ff2bd6",
    "warn": "#fff23d",
    "ink": "#edf7ff",
    "muted": "#8d99ae",
    "grid": "#1b2540",
    "red": "#ff3b6b",
    "teal": "#00f5d4",
    "blue": "#2f7dff",
    "green": "#35ff9c",
    "violet": "#a855ff",
    "orange": "#ff8a1f",
}


def apply_transparent_window(window: tk.Toplevel | tk.Tk) -> None:
    window.configure(bg=THEME["transparent"])
    try:
        window.attributes("-transparentcolor", THEME["transparent"])
    except tk.TclError:
        pass


def draw_round_rect(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    **kwargs,
) -> int:
    radius = max(1, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=18, **kwargs)


def draw_capsule(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    fill: str,
    outline: str,
    width: int = 2,
) -> None:
    height = max(1, y2 - y1)
    radius = height // 2
    left = x1 + radius
    right = x2 - radius
    canvas.create_rectangle(left, y1, right, y2, fill=fill, outline="")
    canvas.create_oval(x1, y1, x1 + height, y2, fill=fill, outline="")
    canvas.create_oval(x2 - height, y1, x2, y2, fill=fill, outline="")
    canvas.create_arc(x1, y1, x1 + height, y2, start=90, extent=180, outline=outline, width=width, style="arc")
    canvas.create_arc(x2 - height, y1, x2, y2, start=-90, extent=180, outline=outline, width=width, style="arc")
    canvas.create_line(left, y1, right, y1, fill=outline, width=width)
    canvas.create_line(left, y2, right, y2, fill=outline, width=width)


def draw_neon_round_rect(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    fill: str,
    outline: str,
    glow: str,
    width: int = 2,
) -> None:
    for offset, glow_width in ((5, 1), (3, 1)):
        draw_round_rect(
            canvas,
            x1 - offset,
            y1 - offset,
            x2 + offset,
            y2 + offset,
            radius + offset,
            fill="",
            outline=glow,
            width=glow_width,
        )
    draw_round_rect(canvas, x1, y1, x2, y2, radius, fill=fill, outline=outline, width=width)
    draw_round_rect(
        canvas,
        x1 + 5,
        y1 + 5,
        x2 - 5,
        y2 - 5,
        max(1, radius - 5),
        fill="",
        outline="#16233c",
        width=1,
    )


def draw_scanlines(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    step: int = 7,
    color: str = THEME["grid"],
) -> None:
    for y in range(y1, y2, step):
        canvas.create_line(x1, y, x2, y, fill=color, width=1)


def draw_corner_brackets(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    color: str,
    length: int = 18,
) -> None:
    corners = [
        (x1, y1, 1, 1),
        (x2, y1, -1, 1),
        (x1, y2, 1, -1),
        (x2, y2, -1, -1),
    ]
    for x, y, sx, sy in corners:
        canvas.create_line(x, y, x + sx * length, y, fill=color, width=2, capstyle="round")
        canvas.create_line(x, y, x, y + sy * length, fill=color, width=2, capstyle="round")


def draw_circuit_trace(
    canvas: tk.Canvas,
    points: list[tuple[int, int]],
    color: str,
    node_color: str = THEME["warn"],
    width: int = 2,
) -> None:
    for start, end in zip(points, points[1:]):
        canvas.create_line(*start, *end, fill=color, width=width, capstyle="round")
    for x, y in points:
        canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=node_color, outline="")


def draw_panel_shell(
    canvas: tk.Canvas,
    width: int,
    height: int,
    radius: int,
    accent: str = THEME["line"],
    glow: str = "#07323b",
    fill: str = THEME["panel"],
    scan: bool = True,
) -> None:
    draw_neon_round_rect(canvas, 5, 6, width - 5, height - 6, radius, fill, accent, glow, width=2)
    if scan:
        draw_scanlines(canvas, 16, 14, width - 16, height - 14, step=8)
    draw_corner_brackets(canvas, 17, 18, width - 17, height - 18, THEME["line_2"], length=14)
    canvas.create_line(30, 12, 92, 12, fill=THEME["warn"], width=3, capstyle="round")
    canvas.create_line(width - 106, height - 12, width - 42, height - 12, fill=accent, width=3, capstyle="round")


def draw_film_icon(
    canvas: tk.Canvas,
    x: int,
    y: int,
    size: int,
    color: str = THEME["ink"],
    accent: str = THEME["line"],
) -> None:
    w = size
    h = int(size * 0.68)
    draw_round_rect(canvas, x - 1, y - 1, x + w + 1, y + h + 1, 6, fill="", outline="#143347", width=3)
    draw_round_rect(canvas, x, y, x + w, y + h, 5, fill="#09111e", outline=color, width=2)
    hole_w = max(3, size // 9)
    hole_h = max(3, size // 8)
    for i in range(3):
        yy = y + 5 + i * ((h - 10) // 2)
        canvas.create_rectangle(x + 4, yy, x + 4 + hole_w, yy + hole_h, fill=accent, outline="")
        canvas.create_rectangle(
            x + w - 4 - hole_w,
            yy,
            x + w - 4,
            yy + hole_h,
            fill=accent,
            outline="",
        )
    canvas.create_line(x + 12, y + h - 8, x + w - 12, y + 8, fill=THEME["line_2"], width=2)
    canvas.create_line(x + 14, y + 6, x + w - 12, y + h - 6, fill=accent, width=1)


def draw_speaker_icon(
    canvas: tk.Canvas,
    x: int,
    y: int,
    size: int,
    color: str = THEME["ink"],
    accent: str = THEME["teal"],
) -> None:
    canvas.create_oval(x - 2, y - 2, x + size + 2, y + size + 2, outline="#0b3440", width=3)
    canvas.create_oval(x, y, x + size, y + size, fill="#08111f", outline=color, width=2)
    pad = max(4, size // 7)
    canvas.create_oval(x + pad, y + pad, x + size - pad, y + size - pad, outline=accent, width=2)
    canvas.create_oval(
        x + pad * 2,
        y + pad * 2,
        x + size - pad * 2,
        y + size - pad * 2,
        outline=THEME["line_2"],
        width=1,
    )
    pad2 = max(9, size // 3)
    canvas.create_oval(x + pad2, y + pad2, x + size - pad2, y + size - pad2, fill=color, outline="")
    canvas.create_arc(x + 4, y + 4, x + size - 4, y + size - 4, start=35, extent=70, outline=THEME["warn"], width=2)


def draw_tv_icon(
    canvas: tk.Canvas,
    x: int,
    y: int,
    size: int,
    color: str = THEME["ink"],
    accent: str = THEME["blue"],
) -> None:
    body_h = int(size * 0.68)
    draw_round_rect(canvas, x - 1, y + 4, x + size + 1, y + 6 + body_h, 8, fill="#08111f", outline="#18304e", width=3)
    draw_round_rect(canvas, x, y + 5, x + size, y + 5 + body_h, 7, fill="#0d1524", outline=color, width=2)
    draw_round_rect(
        canvas,
        x + 6,
        y + 11,
        x + size - 15,
        y + body_h,
        5,
        fill="",
        outline=accent,
        width=2,
    )
    for yy in range(y + 15, y + body_h - 1, 4):
        canvas.create_line(x + 9, yy, x + size - 18, yy, fill="#1a3553", width=1)
    knob_x = x + size - 10
    canvas.create_oval(knob_x - 3, y + 17, knob_x + 3, y + 23, fill=THEME["warn"], outline="")
    canvas.create_oval(knob_x - 3, y + 29, knob_x + 3, y + 35, fill=color, outline="")
    canvas.create_line(x + 10, y + size - 4, x + 16, y + body_h + 5, fill=color, width=2)
    canvas.create_line(x + size - 10, y + size - 4, x + size - 16, y + body_h + 5, fill=color, width=2)


def draw_gear_icon(
    canvas: tk.Canvas,
    x: int,
    y: int,
    size: int,
    color: str = THEME["ink"],
    accent: str = THEME["red"],
) -> None:
    cx = x + size / 2
    cy = y + size / 2
    outer = size * 0.43
    inner = size * 0.25
    for i in range(10):
        angle = math.tau * i / 10
        x1 = cx + math.cos(angle) * inner
        y1 = cy + math.sin(angle) * inner
        x2 = cx + math.cos(angle) * outer
        y2 = cy + math.sin(angle) * outer
        canvas.create_line(x1, y1, x2, y2, fill="#3b1430", width=5, capstyle="round")
        canvas.create_line(x1, y1, x2, y2, fill=color, width=2, capstyle="round")
    canvas.create_oval(cx - inner, cy - inner, cx + inner, cy + inner, fill="#0a101c", outline=color, width=2)
    canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=accent, outline=THEME["warn"])


def draw_ui_icon(canvas: tk.Canvas, icon: str, x: int, y: int, size: int) -> None:
    if icon == "film":
        draw_film_icon(canvas, x, y, size)
    elif icon == "speaker":
        draw_speaker_icon(canvas, x, y, size)
    elif icon == "tv":
        draw_tv_icon(canvas, x, y, size)
    elif icon == "gear":
        draw_gear_icon(canvas, x, y, size)


def create_drawer_header(master: tk.Misc, title: str, icon: str, width: int) -> tk.Canvas:
    header = tk.Canvas(
        master,
        width=width,
        height=54,
        bg=THEME["panel"],
        highlightthickness=0,
        bd=0,
    )
    draw_scanlines(header, 8, 8, width - 8, 48, step=7, color="#111b31")
    header.create_line(18, 9, width - 18, 9, fill="#11283c", width=1)
    header.create_line(width // 2 - 48, 10, width // 2 + 48, 10, fill=THEME["line"], width=3, capstyle="round")
    header.create_line(width // 2 - 18, 15, width // 2 + 18, 15, fill=THEME["line_2"], width=2, capstyle="round")
    draw_ui_icon(header, icon, 18, 16, 24)
    header.create_text(
        54,
        28,
        anchor="w",
        text=title,
        fill=THEME["ink"],
        font=("Microsoft YaHei UI", 11, "bold"),
    )
    header.create_text(
        width - 72,
        28,
        text="LINK READY",
        fill=THEME["muted"],
        font=("Consolas", 8, "bold"),
    )
    draw_circuit_trace(
        header,
        [(width - 148, 28), (width - 122, 28), (width - 112, 38), (width - 90, 38)],
        THEME["line_2"],
        node_color=THEME["warn"],
        width=1,
    )
    header.create_line(14, 52, width - 14, 52, fill="#24314d", width=1)
    return header


class CapsuleButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        icon: str = "",
        width: int = 96,
        height: int = 36,
        fill: str = THEME["panel_2"],
        active_fill: str = "#2a3244",
        fg: str = THEME["ink"],
        accent: str = THEME["line"],
        canvas_bg: str = THEME["panel"],
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            highlightthickness=0,
            bd=0,
            bg=canvas_bg,
        )
        self.text = text
        self.command = command
        self.icon = icon
        self.fill = fill
        self.active_fill = active_fill
        self.fg = fg
        self.accent = accent
        self.width_value = width
        self.height_value = height
        self.hover = False
        self.pressed = False
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw(self) -> None:
        self.delete("all")
        fill = self.active_fill if self.hover else self.fill
        if self.pressed:
            fill = "#222a42"
        glow = "#082f3a" if self.accent in (THEME["line"], THEME["teal"], THEME["blue"]) else "#3b1033"
        draw_neon_round_rect(
            self,
            4,
            4,
            self.width_value - 4,
            self.height_value - 4,
            self.height_value // 2,
            fill=fill,
            outline=self.accent,
            glow=glow,
            width=2,
        )
        draw_scanlines(self, 12, 10, self.width_value - 12, self.height_value - 10, step=6, color="#202a43")
        self.create_line(20, 9, self.width_value - 22, 9, fill="#e8ffff", width=1)
        self.create_line(18, self.height_value - 9, 42, self.height_value - 9, fill=THEME["warn"], width=2, capstyle="round")
        self.create_oval(self.width_value - 18, 14, self.width_value - 12, 20, fill=self.accent, outline="")
        text_x = self.width_value // 2
        if self.icon:
            icon_size = min(22, self.height_value - 12)
            icon_x = 12
            icon_y = (self.height_value - icon_size) // 2
            draw_ui_icon(self, self.icon, icon_x, icon_y, icon_size)
            text_x = 12 + icon_size + (self.width_value - 12 - icon_size) // 2 + 2
        self.create_text(
            text_x,
            self.height_value // 2 + 1,
            text=self.text,
            fill=self.fg,
            font=("Microsoft YaHei UI", 9, "bold"),
        )

    def _on_enter(self, _event: tk.Event) -> None:
        self.hover = True
        self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self.hover = False
        self.pressed = False
        self._draw()

    def _on_press(self, _event: tk.Event) -> None:
        self.pressed = True
        self._draw()

    def _on_release(self, _event: tk.Event) -> None:
        was_pressed = self.pressed
        self.pressed = False
        self._draw()
        if was_pressed:
            self.command()


class StadiumButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        width: int = 104,
        height: int = 40,
        fill: str = "#101827",
        active_fill: str = "#182235",
        outline: str = THEME["line"],
        fg: str = THEME["ink"],
        canvas_bg: str = THEME["transparent"],
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            highlightthickness=0,
            bd=0,
            bg=canvas_bg,
        )
        self.text = text
        self.command = command
        self.width_value = width
        self.height_value = height
        self.fill = fill
        self.active_fill = active_fill
        self.outline = outline
        self.fg = fg
        self.hover = False
        self.pressed = False
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw(self) -> None:
        self.delete("all")
        fill = self.active_fill if self.hover else self.fill
        if self.pressed:
            fill = "#0a101c"
        draw_capsule(
            self,
            2,
            2,
            self.width_value - 2,
            self.height_value - 2,
            fill=fill,
            outline=self.outline,
            width=2,
        )
        self.create_text(
            self.width_value // 2,
            self.height_value // 2,
            text=self.text,
            fill=self.fg,
            font=("Microsoft YaHei UI", 9, "bold"),
        )

    def _on_enter(self, _event: tk.Event) -> None:
        self.hover = True
        self._draw()

    def _on_leave(self, _event: tk.Event) -> None:
        self.hover = False
        self.pressed = False
        self._draw()

    def _on_press(self, _event: tk.Event) -> None:
        self.pressed = True
        self._draw()

    def _on_release(self, _event: tk.Event) -> None:
        was_pressed = self.pressed
        self.pressed = False
        self._draw()
        if was_pressed:
            self.command()


class CyberScrollBar(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        command: Callable[[float], None],
        width: int = 18,
        canvas_bg: str = "#050a14",
    ) -> None:
        super().__init__(master, width=width, highlightthickness=0, bd=0, bg=canvas_bg)
        self.command = command
        self.width_value = width
        self.first = 0.0
        self.last = 1.0
        self.drag_offset = 0
        self.dragging = False
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set(self, first: str, last: str) -> None:
        self.first = max(0.0, min(1.0, float(first)))
        self.last = max(self.first, min(1.0, float(last)))
        self._draw()

    def _track_bounds(self) -> tuple[int, int]:
        height = max(1, self.winfo_height())
        return 12, max(13, height - 12)

    def _thumb_bounds(self) -> tuple[int, int]:
        track_top, track_bottom = self._track_bounds()
        track_height = max(1, track_bottom - track_top)
        visible = max(0.04, self.last - self.first)
        thumb_height = max(30, int(track_height * visible))
        thumb_top = track_top + int(track_height * self.first)
        thumb_top = min(thumb_top, track_bottom - thumb_height)
        return thumb_top, thumb_top + thumb_height

    def _draw(self) -> None:
        self.delete("all")
        height = max(1, self.winfo_height())
        w = self.width_value
        track_top, track_bottom = self._track_bounds()
        self.create_line(w // 2, track_top, w // 2, track_bottom, fill="#12213a", width=8, capstyle="round")
        self.create_line(w // 2, track_top, w // 2, track_bottom, fill=THEME["line"], width=2, capstyle="round")
        for y in range(track_top + 8, track_bottom, 18):
            self.create_line(4, y, w - 4, y, fill=THEME["line_2"], width=1)
        self.create_oval(5, 3, w - 5, 11, fill=THEME["warn"], outline="")
        self.create_oval(5, height - 11, w - 5, height - 3, fill=THEME["line_2"], outline="")

        top, bottom = self._thumb_bounds()
        draw_neon_round_rect(
            self,
            2,
            top,
            w - 2,
            bottom,
            8,
            fill="#061b25",
            outline=THEME["teal"],
            glow="#063a35",
            width=2,
        )
        self.create_line(w // 2, top + 8, w // 2, bottom - 8, fill=THEME["warn"], width=2, capstyle="round")
        for y in range(top + 10, bottom - 5, 9):
            self.create_line(6, y, w - 6, y, fill="#163b4d", width=1)

    def _fraction_for_y(self, y: int) -> float:
        track_top, track_bottom = self._track_bounds()
        track_height = max(1, track_bottom - track_top)
        thumb_top, thumb_bottom = self._thumb_bounds()
        thumb_height = thumb_bottom - thumb_top
        usable = max(1, track_height - thumb_height)
        return max(0.0, min(1.0, (y - track_top - self.drag_offset) / usable))

    def _on_press(self, event: tk.Event) -> None:
        top, bottom = self._thumb_bounds()
        if top <= event.y <= bottom:
            self.dragging = True
            self.drag_offset = event.y - top
        else:
            self.dragging = True
            self.drag_offset = (bottom - top) // 2
            self.command(self._fraction_for_y(event.y))

    def _on_drag(self, event: tk.Event) -> None:
        if self.dragging:
            self.command(self._fraction_for_y(event.y))

    def _on_release(self, _event: tk.Event) -> None:
        self.dragging = False


class CyberScrollText(tk.Frame):
    def __init__(self, master: tk.Misc, **text_kwargs) -> None:
        bg = text_kwargs.pop("bg", "#050a14")
        fg = text_kwargs.pop("fg", THEME["ink"])
        insertbackground = text_kwargs.pop("insertbackground", THEME["ink"])
        selectbackground = text_kwargs.pop("selectbackground", THEME["line_2"])
        highlightbackground = text_kwargs.pop("highlightbackground", THEME["line"])
        text_kwargs.pop("relief", None)
        text_kwargs.pop("bd", None)
        text_kwargs.pop("highlightthickness", None)
        super().__init__(master, bg=bg, highlightthickness=1, highlightbackground=highlightbackground, bd=0)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.text = tk.Text(
            self,
            bg=bg,
            fg=fg,
            insertbackground=insertbackground,
            selectbackground=selectbackground,
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            **text_kwargs,
        )
        self.scrollbar = CyberScrollBar(self, command=self.text.yview_moveto, canvas_bg=bg)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(6, 4), pady=4)
        self.text.bind("<MouseWheel>", self._on_mousewheel, add="+")
        self.text.bind("<Button-4>", self._on_linux_scroll_up, add="+")
        self.text.bind("<Button-5>", self._on_linux_scroll_down, add="+")

    def _on_mousewheel(self, event: tk.Event) -> str:
        self.text.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _on_linux_scroll_up(self, _event: tk.Event) -> str:
        self.text.yview_scroll(-3, "units")
        return "break"

    def _on_linux_scroll_down(self, _event: tk.Event) -> str:
        self.text.yview_scroll(3, "units")
        return "break"

    def insert(self, *args, **kwargs):
        return self.text.insert(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return self.text.delete(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self.text.get(*args, **kwargs)

    def focus_set(self) -> None:
        self.text.focus_set()

    def configure(self, cnf=None, **kwargs):
        if cnf is not None:
            return self.text.configure(cnf)
        if kwargs:
            return self.text.configure(**kwargs)
        return self.text.configure()

    config = configure


class DrawerToplevel(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        drawer_width: int,
        drawer_height: int,
    ) -> None:
        super().__init__(parent)
        self.anchor_window = parent
        self.drawer_width = drawer_width
        self.drawer_height = drawer_height
        self._target_height = drawer_height
        self._target_width = drawer_width
        self._folded = False
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        drawers = getattr(parent, "_drawer_windows", None)
        if drawers is None:
            drawers = []
            setattr(parent, "_drawer_windows", drawers)
        drawers.append(self)
        self.bind("<Destroy>", self._unregister_drawer, add="+")

    def open_from_anchor(self) -> None:
        self.anchor_window.update_idletasks()
        self.update_idletasks()
        self._target_width = self._compute_target_width()
        self._target_height = self._compute_target_height()
        self._folded = False
        self.deiconify()
        self.lift()
        self._animate_open(58)

    def close_drawer(self, after_close: Optional[Callable[[], None]] = None) -> None:
        if not self.winfo_exists():
            if after_close is not None:
                after_close()
            return
        current_height = max(58, self.winfo_height())
        self._animate_close(current_height, after_close)

    def fold_drawer(self) -> None:
        if not self.winfo_exists():
            return
        self._folded = True
        self.withdraw()

    def restore_folded_drawer(self) -> None:
        if not self.winfo_exists():
            return
        self.open_from_anchor()

    def toggle_folded_drawer(self) -> None:
        if not self.winfo_exists():
            return
        if self.winfo_viewable():
            self.fold_drawer()
        else:
            self.restore_folded_drawer()

    def reposition_to_anchor(self) -> None:
        if not self.winfo_exists() or not self.winfo_viewable():
            return
        self._target_width = self._compute_target_width()
        self._target_height = self._compute_target_height()
        height = min(max(58, self.winfo_height()), self._target_height)
        self.geometry(self._drawer_geometry(height))

    def _unregister_drawer(self, _event: tk.Event) -> None:
        drawers = getattr(self.anchor_window, "_drawer_windows", None)
        if drawers is None:
            return
        try:
            drawers.remove(self)
        except ValueError:
            pass

    def _compute_target_width(self) -> int:
        screen_width = self.anchor_window.winfo_screenwidth()
        return min(self.drawer_width, max(320, screen_width - 24))

    def _compute_target_height(self) -> int:
        screen_height = self.anchor_window.winfo_screenheight()
        anchor_y = self.anchor_window.winfo_rooty()
        top_margin = 12
        gap = 8
        space_above = anchor_y - top_margin - gap
        if space_above >= 260:
            return min(self.drawer_height, space_above)
        return min(self.drawer_height, max(260, int(screen_height * 0.54)))

    def _drawer_geometry(self, height: int) -> str:
        self.anchor_window.update_idletasks()
        screen_width = self.anchor_window.winfo_screenwidth()
        anchor_x = self.anchor_window.winfo_rootx()
        anchor_y = self.anchor_window.winfo_rooty()
        anchor_w = max(1, self.anchor_window.winfo_width())
        width = self._target_width
        x = anchor_x + anchor_w // 2 - width // 2
        x = min(max(12, x), max(12, screen_width - width - 12))
        y = max(12, anchor_y - height - 8)
        return f"{width}x{height}+{x}+{y}"

    def _animate_open(self, height: int) -> None:
        if not self.winfo_exists():
            return
        height = min(height, self._target_height)
        self.geometry(self._drawer_geometry(height))
        if height >= self._target_height:
            self.focus_force()
            focus_widget = getattr(self, "drawer_focus_widget", None)
            if focus_widget is not None:
                focus_widget.focus_set()
            return
        next_height = min(self._target_height, height + max(22, (self._target_height - height) // 4))
        self.after(12, lambda: self._animate_open(next_height))

    def _animate_close(
        self,
        height: int,
        after_close: Optional[Callable[[], None]],
    ) -> None:
        if not self.winfo_exists():
            if after_close is not None:
                after_close()
            return
        if height <= 62:
            self.destroy()
            if after_close is not None:
                after_close()
            return
        self.geometry(self._drawer_geometry(height))
        next_height = max(58, height - max(24, height // 4))
        self.after(10, lambda: self._animate_close(next_height, after_close))


@dataclass(frozen=True)
class ModelAccess:
    api_key: str
    base_url: Optional[str]
    model: str


@dataclass(frozen=True)
class AppConfig:
    vision: ModelAccess
    text: ModelAccess
    max_tokens: int
    temperature: float

    @classmethod
    def from_env(cls) -> "AppConfig":
        script_env = Path(__file__).with_name(".env")
        if load_dotenv:
            load_dotenv(script_env)
            load_dotenv()

        global_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        global_base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        vision_model = os.getenv("VISION_MODEL", os.getenv("LLM_MODEL", "qwen3-vl-flash")).strip()
        text_model = os.getenv("TEXT_MODEL", os.getenv("LLM_MODEL", "qwen3.6-flash")).strip()

        return cls(
            vision=ModelAccess(
                api_key=os.getenv("VISION_OPENAI_API_KEY", "").strip() or global_api_key,
                base_url=os.getenv("VISION_OPENAI_BASE_URL", "").strip() or global_base_url,
                model=vision_model,
            ),
            text=ModelAccess(
                api_key=os.getenv("TEXT_OPENAI_API_KEY", "").strip() or global_api_key,
                base_url=os.getenv("TEXT_OPENAI_BASE_URL", "").strip() or global_base_url,
                model=text_model,
            ),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1200")),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        )


class LLMClient:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._vision_client = self._create_client(
            config.vision,
            "框选问",
            "VISION_OPENAI_API_KEY 或 OPENAI_API_KEY",
        )
        self._text_client = self._create_client(
            config.text,
            "拖文本/剪贴板",
            "TEXT_OPENAI_API_KEY 或 OPENAI_API_KEY",
        )

    @staticmethod
    def _create_client(access: ModelAccess, label: str, key_hint: str) -> OpenAI:
        if not access.api_key:
            raise RuntimeError(f"{label} 缺少 {key_hint}。请在环境变量或脚本同目录 .env 中配置。")
        kwargs = {"api_key": access.api_key}
        if access.base_url:
            kwargs["base_url"] = access.base_url
        return OpenAI(**kwargs)

    def ask_image(self, image: Image.Image, question: str) -> str:
        data_url = self._image_to_data_url(image)
        prompt = question.strip() or DEFAULT_IMAGE_QUESTION
        response = self._vision_client.chat.completions.create(
            model=self._config.vision.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
        )
        return self._extract_text(response)

    def ask_text(self, text: str, question: str) -> str:
        prompt = question.strip() or DEFAULT_TEXT_QUESTION
        response = self._text_client.chat.completions.create(
            model=self._config.text.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"{prompt}\n\n---\n{text.strip()}",
                },
            ],
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
        )
        return self._extract_text(response)

    @staticmethod
    def _image_to_data_url(image: Image.Image) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def _extract_text(response) -> str:
        message = response.choices[0].message
        content = message.content if message else ""
        return (content or "").strip() or "模型没有返回文本内容。"


class SelectionOverlay:
    def __init__(
        self,
        root: tk.Tk,
        on_capture: Callable[[Image.Image], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self.root = root
        self.on_capture = on_capture
        self.on_cancel = on_cancel
        self.window: Optional[tk.Toplevel] = None
        self.canvas: Optional[tk.Canvas] = None
        self.start_x = 0
        self.start_y = 0
        self.rect_id: Optional[int] = None

    def open(self) -> None:
        self.root.withdraw()
        self.root.after(180, self._show_overlay)

    def _show_overlay(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("框选区域")
        win.attributes("-fullscreen", True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.28)
        win.configure(cursor="crosshair", bg="#000000")
        win.bind("<Escape>", self._cancel)

        canvas = tk.Canvas(win, bg="#000000", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        canvas.bind("<ButtonPress-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)

        label = canvas.create_text(
            20,
            20,
            anchor="nw",
            fill="#ffffff",
            font=("Microsoft YaHei UI", 13),
            text="拖拽框选要提问的屏幕区域，Esc 取消",
        )
        canvas.tag_raise(label)

        self.window = win
        self.canvas = canvas
        win.focus_force()

    def _on_press(self, event: tk.Event) -> None:
        self.start_x = event.x
        self.start_y = event.y
        if self.canvas is None:
            return
        self.rect_id = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            event.x,
            event.y,
            outline="#ff4d4f",
            width=3,
        )

    def _on_drag(self, event: tk.Event) -> None:
        if self.canvas is None or self.rect_id is None:
            return
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def _on_release(self, event: tk.Event) -> None:
        if self.canvas is None or self.window is None:
            self._cancel()
            return

        root_x = self.window.winfo_rootx()
        root_y = self.window.winfo_rooty()
        left = min(self.start_x, event.x) + root_x
        top = min(self.start_y, event.y) + root_y
        right = max(self.start_x, event.x) + root_x
        bottom = max(self.start_y, event.y) + root_y

        self.window.destroy()
        self.window = None

        if right - left < 8 or bottom - top < 8:
            self.on_cancel()
            return

        self.root.after(80, lambda: self._grab_region(left, top, right, bottom))

    def _grab_region(self, left: int, top: int, right: int, bottom: int) -> None:
        try:
            image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        except TypeError:
            image = ImageGrab.grab(bbox=(left, top, right, bottom))
        self.on_capture(image)

    def _cancel(self, event: Optional[tk.Event] = None) -> None:
        if self.window is not None:
            self.window.destroy()
            self.window = None
        self.on_cancel()


class PromptDialog(DrawerToplevel):
    def __init__(
        self,
        parent: tk.Tk,
        title: str,
        default_question: str,
        on_submit: Callable[[str], None],
        image: Optional[Image.Image] = None,
        body_text: Optional[str] = None,
    ) -> None:
        super().__init__(parent, drawer_width=620, drawer_height=520)
        self.title(title)
        self.transient(parent)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.on_submit = on_submit
        self.preview_ref: Optional[ImageTk.PhotoImage] = None

        self.configure(bg=THEME["panel_deep"])
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = create_drawer_header(self, title, "tv", 620)
        header.grid(row=0, column=0, sticky="ew")

        if image is not None:
            preview = self._make_preview(image)
            preview_label = tk.Label(self, image=preview, bg=THEME["panel_deep"])
            preview_label.grid(row=1, column=0, sticky="ew", padx=14, pady=(10, 8))
            self.preview_ref = preview
        elif body_text is not None:
            snippet = body_text.strip()
            if len(snippet) > 900:
                snippet = snippet[:900] + "\n..."
            text_preview = CyberScrollText(
                self,
                height=8,
                wrap="word",
                font=("Microsoft YaHei UI", 10),
                bg="#050a14",
                fg=THEME["ink"],
                insertbackground=THEME["ink"],
                selectbackground=THEME["line_2"],
                highlightthickness=1,
                highlightbackground=THEME["line"],
                relief="solid",
                bd=1,
            )
            text_preview.insert("1.0", snippet)
            text_preview.configure(state="disabled")
            text_preview.grid(row=1, column=0, sticky="nsew", padx=14, pady=(10, 8))

        prompt_frame = tk.Frame(self, bg=THEME["panel_deep"])
        prompt_frame.grid(row=2, column=0, sticky="nsew", padx=14, pady=8)
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(1, weight=1)

        tk.Label(
            prompt_frame,
            text="问题 / 指令",
            bg=THEME["panel_deep"],
            fg=THEME["line"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.text = CyberScrollText(
            prompt_frame,
            height=5,
            width=64,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            bg="#050a14",
            fg=THEME["ink"],
            insertbackground=THEME["ink"],
            selectbackground=THEME["line_2"],
            highlightthickness=1,
            highlightbackground=THEME["line"],
            relief="solid",
            bd=1,
        )
        self.text.insert("1.0", default_question)
        self.text.grid(row=1, column=0, sticky="nsew")
        self.drawer_focus_widget = self.text

        buttons = tk.Frame(self, bg=THEME["panel_deep"])
        buttons.grid(row=3, column=0, sticky="e", padx=14, pady=(8, 14))
        StadiumButton(
            buttons,
            text="取消",
            command=self.close_drawer,
            width=88,
            height=36,
            fill="#2d1e28",
            active_fill="#442737",
            outline=THEME["red"],
            canvas_bg=THEME["panel_deep"],
        ).pack(side="right", padx=(8, 0))
        StadiumButton(
            buttons,
            text="发送",
            command=self._submit,
            width=88,
            height=36,
            fill="#0d2a2a",
            active_fill="#123f3e",
            outline=THEME["teal"],
            canvas_bg=THEME["panel_deep"],
        ).pack(side="right")

        self.bind("<Control-Return>", lambda _event: self._submit())
        self.protocol("WM_DELETE_WINDOW", self.close_drawer)
        self.geometry("620x58+0+0")
        self.focus_force()
        self.text.focus_set()

    @staticmethod
    def _make_preview(image: Image.Image) -> ImageTk.PhotoImage:
        preview = image.copy()
        preview.thumbnail((560, 260))
        return ImageTk.PhotoImage(preview)

    def _submit(self) -> None:
        question = self.text.get("1.0", "end").strip()
        self.close_drawer(lambda: self.on_submit(question))


class ResultWindow(DrawerToplevel):
    def __init__(self, parent: tk.Tk, initial_text: str) -> None:
        super().__init__(parent, drawer_width=720, drawer_height=560)
        self.title("LLM 回答")
        self.attributes("-topmost", True)
        self.configure(bg=THEME["panel_deep"])
        self.geometry("720x58+0+0")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = create_drawer_header(self, "LLM 回答", "speaker", 720)
        header.grid(row=0, column=0, sticky="ew")

        self.text = CyberScrollText(
            self,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            bg="#050a14",
            fg=THEME["ink"],
            insertbackground=THEME["ink"],
            selectbackground=THEME["line_2"],
            highlightthickness=1,
            highlightbackground=THEME["line"],
            relief="solid",
            bd=1,
        )
        self.text.insert("1.0", initial_text)
        self.text.configure(state="disabled")
        self.text.grid(row=1, column=0, sticky="nsew", padx=14, pady=14)

        buttons = tk.Frame(self, bg=THEME["panel_deep"])
        buttons.grid(row=2, column=0, sticky="e", padx=14, pady=(0, 14))
        StadiumButton(
            buttons,
            text="复制",
            command=self.copy_answer,
            width=88,
            height=36,
            fill="#302417",
            active_fill="#49351d",
            outline=THEME["warn"],
            canvas_bg=THEME["panel_deep"],
        ).pack(side="right", padx=(8, 0))
        StadiumButton(
            buttons,
            text="关闭",
            command=self.close_drawer,
            width=88,
            height=36,
            fill="#2d1e28",
            active_fill="#442737",
            outline=THEME["red"],
            canvas_bg=THEME["panel_deep"],
        ).pack(side="right")

    def set_text(self, value: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        self.text.configure(state="disabled")

    def copy_answer(self) -> None:
        value = self.text.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(value)


class RandomAskApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.98)
        apply_transparent_window(self.root)
        screen_height = self.root.winfo_screenheight()
        initial_y = max(80, screen_height - 168)
        self.root.geometry(f"356x54+120+{initial_y}")

        self.config = AppConfig.from_env()
        self.llm: Optional[LLMClient] = None
        self.response_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.result_window: Optional[ResultWindow] = None
        self.image_prompt_dialog: Optional[PromptDialog] = None
        self.clipboard_prompt_dialog: Optional[PromptDialog] = None
        self.text_drag_tip: Optional[tk.Toplevel] = None
        self.text_confirm_badge: Optional[tk.Toplevel] = None
        self.text_drag_active = False
        self.clipboard_seq_before: Optional[int] = None
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.gear_angle = 0.0
        self.tray_icon = None

        self._build_bubble()
        self._setup_tray_icon()

    def run(self) -> None:
        self.root.mainloop()

    def _setup_tray_icon(self) -> None:
        if pystray is None:
            return

        menu = pystray.Menu(
            pystray.MenuItem("显示浮窗", lambda _icon, _item: self.root.after(0, self._restore_bubble)),
            pystray.MenuItem("退出", lambda _icon, _item: self.root.after(0, self.quit_app)),
        )
        self.tray_icon = pystray.Icon(
            "random_ask",
            self._create_tray_image(),
            APP_TITLE,
            menu,
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def _create_tray_image(self) -> Image.Image:
        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((5, 5, 59, 59), fill=(7, 9, 20, 255), outline=(0, 245, 255, 255), width=3)
        cx = cy = size // 2
        teeth = 10
        points: list[tuple[float, float]] = []
        for i in range(teeth * 2):
            angle = -math.pi / 2 + math.tau * i / (teeth * 2)
            radius = 22 if i % 2 == 0 else 17
            points.append((cx + math.cos(angle) * radius, cy + math.sin(angle) * radius))
        draw.polygon(points, fill=(237, 247, 255, 255), outline=(7, 9, 20, 255))
        draw.ellipse((23, 23, 41, 41), fill=(7, 9, 20, 255), outline=(0, 245, 212, 255), width=3)
        draw.ellipse((29, 29, 35, 35), fill=(255, 242, 61, 255))
        return image

    def quit_app(self) -> None:
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.root.destroy()

    def _build_bubble(self) -> None:
        width = 356
        height = 54
        canvas = tk.Canvas(
            self.root,
            width=width,
            height=height,
            bg=THEME["transparent"],
            highlightthickness=0,
            bd=0,
        )
        canvas.pack(fill="both", expand=True)
        self.bubble_canvas = canvas

        for widget in (canvas, self.root):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._on_move)

        draw_capsule(
            canvas,
            1,
            1,
            width - 1,
            height - 1,
            fill=THEME["panel"],
            outline=THEME["line"],
            width=2,
        )

        self._draw_drag_gear()
        self._animate_drag_gear()

        ask_btn = StadiumButton(
            canvas,
            text="框选问",
            command=self.capture_region,
            width=96,
            height=40,
            fill="#101827",
            active_fill="#182235",
            outline=THEME["blue"],
        )
        text_btn = StadiumButton(
            canvas,
            text="拖文本",
            command=self.select_text_then_ask,
            width=96,
            height=40,
            fill="#0d1f21",
            active_fill="#123033",
            outline=THEME["teal"],
        )
        clip_btn = StadiumButton(
            canvas,
            text="剪贴板",
            command=self.ask_clipboard,
            width=96,
            height=40,
            fill="#302417",
            active_fill="#49351d",
            outline=THEME["warn"],
        )
        canvas.create_window(94, height // 2, window=ask_btn)
        canvas.create_window(196, height // 2, window=text_btn)
        canvas.create_window(298, height // 2, window=clip_btn)

    def _draw_drag_gear(self) -> None:
        canvas = self.bubble_canvas
        canvas.delete("drag_gear")
        cx = 26
        cy = 27
        tooth_outer = 16
        tooth_inner = 13
        hub_outer = 8
        bore = 3
        canvas.create_oval(
            cx - 20,
            cy - 20,
            cx + 20,
            cy + 20,
            fill="#0a111d",
            outline=THEME["line"],
            width=2,
            tags=("drag_gear",),
        )
        points: list[float] = []
        teeth = 10
        for i in range(teeth * 2):
            angle = self.gear_angle + math.tau * i / (teeth * 2)
            radius = tooth_outer if i % 2 == 0 else tooth_inner
            points.extend([cx + math.cos(angle) * radius, cy + math.sin(angle) * radius])
        canvas.create_polygon(
            points,
            fill=THEME["ink"],
            outline="#0a111d",
            width=1,
            smooth=False,
            tags=("drag_gear",),
        )
        canvas.create_oval(
            cx - hub_outer,
            cy - hub_outer,
            cx + hub_outer,
            cy + hub_outer,
            fill="#111827",
            outline=THEME["teal"],
            width=2,
            tags=("drag_gear",),
        )
        canvas.create_oval(
            cx - bore,
            cy - bore,
            cx + bore,
            cy + bore,
            fill=THEME["warn"],
            outline="#0a111d",
            tags=("drag_gear",),
        )
        canvas.tag_bind("drag_gear", "<ButtonPress-1>", self._start_move)
        canvas.tag_bind("drag_gear", "<B1-Motion>", self._on_move)

    def _animate_drag_gear(self) -> None:
        if not hasattr(self, "bubble_canvas") or not self.root.winfo_exists():
            return
        self.gear_angle = (self.gear_angle + 0.08) % math.tau
        self._draw_drag_gear()
        self.root.after(80, self._animate_drag_gear)

    def _start_move(self, event: tk.Event) -> None:
        self.drag_start_x = event.x
        self.drag_start_y = event.y

    def _on_move(self, event: tk.Event) -> None:
        x = self.root.winfo_pointerx() - self.drag_start_x
        y = self.root.winfo_pointery() - self.drag_start_y
        self.root.geometry(f"+{x}+{y}")
        self._reposition_open_drawers()

    def _reposition_open_drawers(self) -> None:
        drawers = list(getattr(self.root, "_drawer_windows", []))
        for drawer in drawers:
            if isinstance(drawer, DrawerToplevel):
                drawer.reposition_to_anchor()

    def capture_region(self) -> None:
        if self._toggle_existing_drawer(self.image_prompt_dialog):
            return
        overlay = SelectionOverlay(
            self.root,
            on_capture=self._on_image_captured,
            on_cancel=self._restore_bubble,
        )
        overlay.open()

    def select_text_then_ask(self) -> None:
        if os.name != "nt":
            messagebox.showinfo("暂不支持", "拖选文本模式目前只支持 Windows。")
            return
        if self.text_confirm_badge is not None and self.text_confirm_badge.winfo_exists():
            self.text_confirm_badge.destroy()
        self.text_confirm_badge = None
        self.text_drag_active = True
        self.clipboard_seq_before = None
        self.root.withdraw()
        self._show_text_drag_tip()
        self.root.after(80, self._wait_for_button_release_before_text_drag)

    def ask_clipboard(self) -> None:
        if self._toggle_existing_drawer(self.clipboard_prompt_dialog):
            return
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showwarning("剪贴板为空", "没有读到剪贴板文本。")
            return
        if not text.strip():
            messagebox.showwarning("剪贴板为空", "没有读到剪贴板文本。")
            return

        dialog = PromptDialog(
            self.root,
            "询问剪贴板文本",
            DEFAULT_TEXT_QUESTION,
            lambda question: self._ask_text_async(text, question),
            body_text=text,
        )
        self.clipboard_prompt_dialog = dialog
        dialog.bind("<Destroy>", lambda _event, d=dialog: self._clear_prompt_dialog("clipboard", d), add="+")
        self._center_near_bubble(dialog)

    def _toggle_existing_drawer(self, drawer: Optional[DrawerToplevel]) -> bool:
        if drawer is None:
            return False
        try:
            if not drawer.winfo_exists():
                return False
            drawer.toggle_folded_drawer()
            return True
        except tk.TclError:
            return False

    def _clear_prompt_dialog(self, kind: str, dialog: PromptDialog) -> None:
        if kind == "image" and self.image_prompt_dialog is dialog:
            self.image_prompt_dialog = None
        elif kind == "clipboard" and self.clipboard_prompt_dialog is dialog:
            self.clipboard_prompt_dialog = None

    def _show_text_drag_tip(self) -> None:
        tip = tk.Toplevel(self.root)
        tip.title("拖选文本")
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.resizable(False, False)
        apply_transparent_window(tip)
        tip.protocol("WM_DELETE_WINDOW", self._cancel_text_drag)

        width = 360
        height = 104
        canvas = tk.Canvas(
            tip,
            width=width,
            height=height,
            bg=THEME["transparent"],
            highlightthickness=0,
            bd=0,
        )
        canvas.pack(fill="both", expand=True)
        draw_round_rect(
            canvas,
            2,
            2,
            width - 2,
            height - 2,
            24,
            fill=THEME["panel"],
            outline=THEME["teal"],
            width=2,
        )
        canvas.create_text(
            24,
            16,
            anchor="nw",
            text="拖选文本监听中",
            fill=THEME["ink"],
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        canvas.create_text(
            24,
            42,
            anchor="nw",
            text="在目标应用中拖选文本，松开后自动复制。",
            fill=THEME["muted"],
            width=230,
            font=("Microsoft YaHei UI", 9),
        )
        cancel_btn = StadiumButton(
            canvas,
            text="取消",
            command=self._cancel_text_drag,
            width=78,
            height=34,
            fill="#2d1e28",
            active_fill="#442737",
            outline=THEME["red"],
            canvas_bg=THEME["panel"],
        )
        canvas.create_window(width - 54, height - 34, window=cancel_btn)
        self.text_drag_tip = tip
        self._center_near_bubble(tip)

    def _wait_for_button_release_before_text_drag(self) -> None:
        if not self.text_drag_active:
            return
        if is_left_mouse_down():
            self.root.after(40, self._wait_for_button_release_before_text_drag)
            return
        self.root.after(40, self._wait_for_text_drag_start)

    def _wait_for_text_drag_start(self) -> None:
        if not self.text_drag_active:
            return
        if is_left_mouse_down():
            self.clipboard_seq_before = get_clipboard_sequence_number()
            self.root.after(80, self._wait_for_text_drag_end)
            return
        self.root.after(50, self._wait_for_text_drag_start)

    def _wait_for_text_drag_end(self) -> None:
        if not self.text_drag_active:
            return
        if is_left_mouse_down():
            self.root.after(60, self._wait_for_text_drag_end)
            return
        self.root.after(160, self._copy_selected_text_after_drag)

    def _copy_selected_text_after_drag(self) -> None:
        if not self.text_drag_active:
            return
        send_ctrl_c()
        self.root.after(180, lambda: self._read_clipboard_after_copy(12))

    def _read_clipboard_after_copy(self, retries: int) -> None:
        if not self.text_drag_active:
            return

        seq_after = get_clipboard_sequence_number()
        if self.clipboard_seq_before == seq_after and retries > 0:
            self.root.after(100, lambda: self._read_clipboard_after_copy(retries - 1))
            return

        if self.clipboard_seq_before == seq_after:
            self._finish_text_drag_mode()
            messagebox.showwarning(
                "没有复制到文本",
                "没有检测到新的剪贴板文本。请确认目标应用允许复制，或改用框选问。",
            )
            return

        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            text = ""

        self._finish_text_drag_mode(restore_bubble=False)
        if not text.strip():
            self._restore_bubble()
            messagebox.showwarning(
                "没有复制到文本",
                "剪贴板已更新，但没有读到文本内容。请改用框选问。",
            )
            return

        self._show_text_confirm_badge(text)

    def _show_text_confirm_badge(self, text: str) -> None:
        if self.text_confirm_badge is not None and self.text_confirm_badge.winfo_exists():
            self.text_confirm_badge.destroy()

        badge = tk.Toplevel(self.root)
        badge.title("确认提问")
        badge.overrideredirect(True)
        badge.attributes("-topmost", True)
        apply_transparent_window(badge)

        width = 420
        height = 76
        canvas = tk.Canvas(
            badge,
            width=width,
            height=height,
            bg=THEME["transparent"],
            highlightthickness=0,
            bd=0,
        )
        canvas.pack(fill="both", expand=True)
        draw_panel_shell(canvas, width, height, 31, accent=THEME["line"], glow="#063044", fill=THEME["panel"], scan=True)
        draw_speaker_icon(canvas, 20, 20, 36, color=THEME["ink"], accent=THEME["teal"])
        text_len = len(text.strip())
        canvas.create_text(
            68,
            31,
            anchor="w",
            text=f"已选 {text_len} 字",
            fill=THEME["ink"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        canvas.create_text(
            68,
            49,
            anchor="w",
            text="BUFFER READY",
            fill=THEME["muted"],
            font=("Consolas", 8, "bold"),
        )

        confirm_btn = StadiumButton(
            canvas,
            text="确认",
            command=lambda: self._confirm_drag_text(text),
            width=76,
            height=34,
            fill="#16331f",
            active_fill="#1f4a2c",
            outline=THEME["green"],
            canvas_bg=THEME["panel"],
        )
        cancel_btn = StadiumButton(
            canvas,
            text="取消",
            command=self._cancel_text_confirm_badge,
            width=76,
            height=34,
            fill="#2d1e28",
            active_fill="#442737",
            outline=THEME["red"],
            canvas_bg=THEME["panel"],
        )
        canvas.create_window(284, height // 2, window=confirm_btn)
        canvas.create_window(364, height // 2, window=cancel_btn)

        badge.bind("<Escape>", lambda _event: self._cancel_text_confirm_badge())
        self.text_confirm_badge = badge
        self._position_badge_near_pointer(badge)

    def _position_badge_near_pointer(self, badge: tk.Toplevel) -> None:
        badge.update_idletasks()
        x = self.root.winfo_pointerx() + 12
        y = self.root.winfo_pointery() + 12
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = badge.winfo_width()
        height = badge.winfo_height()
        x = min(max(8, x), max(8, screen_width - width - 8))
        y = min(max(8, y), max(8, screen_height - height - 8))
        badge.geometry(f"+{x}+{y}")

    def _confirm_drag_text(self, text: str) -> None:
        if self.text_confirm_badge is not None and self.text_confirm_badge.winfo_exists():
            self.text_confirm_badge.destroy()
        self.text_confirm_badge = None
        self._restore_bubble()
        self._ask_text_async(text, DEFAULT_TEXT_QUESTION)

    def _cancel_text_confirm_badge(self) -> None:
        if self.text_confirm_badge is not None and self.text_confirm_badge.winfo_exists():
            self.text_confirm_badge.destroy()
        self.text_confirm_badge = None
        self._restore_bubble()

    def _cancel_text_drag(self) -> None:
        self._finish_text_drag_mode()

    def _finish_text_drag_mode(self, restore_bubble: bool = True) -> None:
        self.text_drag_active = False
        self.clipboard_seq_before = None
        if self.text_drag_tip is not None and self.text_drag_tip.winfo_exists():
            self.text_drag_tip.destroy()
        self.text_drag_tip = None
        if restore_bubble:
            self._restore_bubble()

    def _on_image_captured(self, image: Image.Image) -> None:
        self._restore_bubble()
        dialog = PromptDialog(
            self.root,
            "询问框选截图",
            "",
            lambda question: self._ask_image_async(image, question),
            image=image,
        )
        self.image_prompt_dialog = dialog
        dialog.bind("<Destroy>", lambda _event, d=dialog: self._clear_prompt_dialog("image", d), add="+")
        self._center_near_bubble(dialog)

    def _restore_bubble(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def _get_llm(self) -> Optional[LLMClient]:
        if self.llm is not None:
            return self.llm
        try:
            self.config = AppConfig.from_env()
            self.llm = LLMClient(self.config)
        except Exception as exc:  # noqa: BLE001 - surfaced to user
            messagebox.showerror("LLM 配置错误", str(exc))
            return None
        return self.llm

    def _ask_image_async(self, image: Image.Image, question: str) -> None:
        llm = self._get_llm()
        if llm is None:
            return
        self._start_request(lambda: llm.ask_image(image, question))

    def _ask_text_async(self, text: str, question: str) -> None:
        llm = self._get_llm()
        if llm is None:
            return
        self._start_request(lambda: llm.ask_text(text, question))

    def _start_request(self, work: Callable[[], str]) -> None:
        self.result_window = ResultWindow(self.root, "正在请求模型，请稍候...")
        self._center_near_bubble(self.result_window)

        def worker() -> None:
            try:
                answer = work()
                self.response_queue.put(("ok", answer))
            except Exception:
                self.response_queue.put(("error", traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(120, self._poll_response)

    def _poll_response(self) -> None:
        try:
            status, payload = self.response_queue.get_nowait()
        except queue.Empty:
            self.root.after(120, self._poll_response)
            return

        if self.result_window is None or not self.result_window.winfo_exists():
            return
        if status == "ok":
            self.result_window.set_text(payload)
        else:
            self.result_window.set_text(
                "请求失败。请检查网络、对应模式的 API Key、Base URL 和模型名。\n\n"
                + payload
            )

    def _center_near_bubble(self, window: tk.Toplevel) -> None:
        if isinstance(window, DrawerToplevel):
            window.open_from_anchor()
            return
        self.root.update_idletasks()
        window.update_idletasks()
        x = max(20, self.root.winfo_x() + 20)
        y = max(20, self.root.winfo_y() + self.root.winfo_height() + 12)
        window.geometry(f"+{x}+{y}")


def main() -> None:
    make_process_dpi_aware()
    app = RandomAskApp()
    app.run()


if __name__ == "__main__":
    main()
