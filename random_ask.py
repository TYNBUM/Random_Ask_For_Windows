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
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageGrab, ImageTk
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


def begin_high_resolution_timer() -> bool:
    if os.name != "nt":
        return False
    try:
        return ctypes.windll.winmm.timeBeginPeriod(1) == 0
    except Exception:
        return False


def end_high_resolution_timer() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.winmm.timeEndPeriod(1)
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
    "panel": "#f7fbff",
    "panel_deep": "#eef4fb",
    "panel_2": "#fbfdff",
    "panel_3": "#e5edf7",
    "glass_underlay": "#dfeaf7",
    "glass_fill": "#f9fcff",
    "glass_inner": "#ffffff",
    "glass_edge": "#ffffff",
    "glass_shadow": "#b8c7d8",
    "glass_shadow_soft": "#d8e2ee",
    "line": "#6aa7ff",
    "line_2": "#b4d5ff",
    "warn": "#f4b642",
    "ink": "#172033",
    "muted": "#657187",
    "grid": "#dce8f5",
    "field": "#edf4f9",
    "field_glass": "#eaf2f8",
    "field_glass_edge": "#f8fbff",
    "field_outer": "#515b66",
    "selection": "#cfe3ff",
    "red": "#ff5f57",
    "teal": "#18b7a6",
    "blue": "#3f8cff",
    "green": "#30c16b",
    "violet": "#8f7cff",
    "orange": "#ff9f0a",
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


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def blend_color(start: str, end: str, amount: float) -> str:
    amount = max(0.0, min(1.0, amount))
    sr, sg, sb = _hex_to_rgb(start)
    er, eg, eb = _hex_to_rgb(end)
    return _rgb_to_hex(
        (
            round(sr + (er - sr) * amount),
            round(sg + (eg - sg) * amount),
            round(sb + (eb - sb) * amount),
        )
    )


def make_rounded_mask(size: tuple[int, int], rect: tuple[int, int, int, int], radius: int) -> Image.Image:
    area = size[0] * size[1]
    scale = 4 if area <= 50000 else 2
    scaled_size = (size[0] * scale, size[1] * scale)
    scaled_rect = tuple(value * scale for value in rect)
    mask = Image.new("L", scaled_size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(scaled_rect, radius=radius * scale, fill=255)
    return mask.resize(size, Image.Resampling.LANCZOS)


def make_edge_mask(mask: Image.Image, thickness: int) -> Image.Image:
    thickness = max(1, min(thickness, 18))
    kernel_size = max(3, thickness * 2 + 1)
    eroded = mask.filter(ImageFilter.MinFilter(kernel_size))
    edge = ImageChops.subtract(mask, eroded)
    return edge.filter(ImageFilter.GaussianBlur(max(0.6, thickness / 3)))


def make_fine_rim_mask(mask: Image.Image, thickness: int = 1, blur: float = 0.45) -> Image.Image:
    thickness = max(1, thickness)
    kernel_size = max(3, thickness * 2 + 1)
    eroded = mask.filter(ImageFilter.MinFilter(kernel_size))
    rim = ImageChops.subtract(mask, eroded)
    if blur > 0:
        rim = rim.filter(ImageFilter.GaussianBlur(blur))
    return rim


def scaled_alpha(mask: Image.Image, scale: float, cap: int = 255) -> Image.Image:
    return mask.point(lambda value: min(cap, int(value * scale)))


@lru_cache(maxsize=32)
def make_liquid_glass_mask_layers(
    width: int,
    height: int,
    shell_rect: tuple[int, int, int, int],
    shell_radius: int,
    clean_highlight: bool,
) -> tuple[Image.Image, Image.Image, Image.Image, Image.Image, Image.Image, Image.Image]:
    """Build immutable mask layers shared by every frame of a fixed-size glass surface."""
    mask = make_rounded_mask((width, height), shell_rect, shell_radius)
    edge_mask = make_edge_mask(mask, max(3, min(width, height) // 6))
    refraction_alpha = scaled_alpha(edge_mask, 0.88, 218)

    top_gradient = Image.new("L", (width, height), 0)
    top_draw = ImageDraw.Draw(top_gradient)
    bottom_gradient = Image.new("L", (width, height), 0)
    bottom_draw = ImageDraw.Draw(bottom_gradient)
    for y_pos in range(height):
        progress = y_pos / max(1, height - 1)
        top_draw.line((0, y_pos, width, y_pos), fill=int(128 * (1 - progress)))
        bottom_draw.line((0, y_pos, width, y_pos), fill=int(76 * progress))

    top_alpha = ImageChops.multiply(edge_mask, top_gradient)
    bottom_alpha = ImageChops.multiply(edge_mask, bottom_gradient)
    rim_mask = make_fine_rim_mask(mask, 1 if clean_highlight else 2, 0.45 if clean_highlight else 0.65)
    rim_alpha = scaled_alpha(
        rim_mask,
        0.54 if clean_highlight else 0.76,
        138 if clean_highlight else 204,
    )
    binary_mask = mask.point(lambda value: 255 if value >= 128 else 0)
    return mask, refraction_alpha, top_alpha, bottom_alpha, rim_alpha, binary_mask


def create_fallback_backdrop(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height), THEME["panel_deep"])
    draw = ImageDraw.Draw(image)
    for y in range(height):
        amount = y / max(1, height - 1)
        color = blend_color("#dce9f7", "#f8fbff", amount)
        draw.line((0, y, width, y), fill=color)
    draw.ellipse((-width // 4, -height, width // 2, height), fill="#d3e6ff")
    draw.ellipse((width // 2, height // 4, width + width // 4, height + height // 2), fill="#f2eaff")
    return image


class BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BitmapInfoHeader),
        ("bmiColors", wintypes.DWORD * 3),
    ]


@lru_cache(maxsize=1)
def get_gdi_capture_api() -> tuple[Any, Any]:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.BitBlt.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(BitmapInfo),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    return user32, gdi32


def grab_screen_region_gdi(left: int, top: int, width: int, height: int) -> Optional[Image.Image]:
    if os.name != "nt" or width <= 0 or height <= 0:
        return None
    user32, gdi32 = get_gdi_capture_api()
    screen_dc = user32.GetDC(None)
    if not screen_dc:
        return None
    memory_dc = None
    bitmap = None
    previous = None
    try:
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not memory_dc or not bitmap:
            return None
        previous = gdi32.SelectObject(memory_dc, bitmap)
        source_copy = 0x00CC0020
        capture_layered_windows = 0x40000000
        copied = gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            screen_dc,
            left,
            top,
            source_copy | capture_layered_windows,
        )
        if not copied:
            return None

        bitmap_info = BitmapInfo()
        bitmap_info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader)
        bitmap_info.bmiHeader.biWidth = width
        bitmap_info.bmiHeader.biHeight = -height
        bitmap_info.bmiHeader.biPlanes = 1
        bitmap_info.bmiHeader.biBitCount = 32
        bitmap_info.bmiHeader.biCompression = 0
        pixel_buffer = (ctypes.c_ubyte * (width * height * 4))()
        copied_rows = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            pixel_buffer,
            ctypes.byref(bitmap_info),
            0,
        )
        if copied_rows != height:
            return None
        return Image.frombuffer(
            "RGB",
            (width, height),
            pixel_buffer,
            "raw",
            "BGRX",
            0,
            1,
        )
    finally:
        if previous:
            gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


def grab_screen_region(x: int, y: int, width: int, height: int) -> Optional[Image.Image]:
    try:
        try:
            return ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True)
        except TypeError:
            return ImageGrab.grab(bbox=(x, y, x + width, y + height))
    except Exception:
        return None


def get_virtual_screen_bounds(root: tk.Tk) -> tuple[int, int, int, int]:
    if os.name == "nt":
        user32 = ctypes.windll.user32
        left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        top = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        if width > 0 and height > 0:
            return left, top, width, height
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def crop_from_screen_snapshot(
    snapshot: Image.Image,
    origin: tuple[int, int],
    x: int,
    y: int,
    width: int,
    height: int,
) -> Image.Image:
    left = x - origin[0]
    top = y - origin[1]
    right = left + width
    bottom = top + height
    if left >= 0 and top >= 0 and right <= snapshot.width and bottom <= snapshot.height:
        return snapshot.crop((left, top, right, bottom))

    fallback = create_fallback_backdrop(width, height)
    src_left = max(0, left)
    src_top = max(0, top)
    src_right = min(snapshot.width, right)
    src_bottom = min(snapshot.height, bottom)
    if src_right > src_left and src_bottom > src_top:
        patch = snapshot.crop((src_left, src_top, src_right, src_bottom))
        fallback.paste(patch, (src_left - left, src_top - top))
    return fallback


def make_liquid_glass_bitmap(
    width: int,
    height: int,
    screen_image: Optional[Image.Image],
    accent: str = THEME["line"],
    transparent_outside: bool = False,
    radius: Optional[int] = None,
    blur_radius: int = 18,
    frost_alpha: int = 42,
    tint_alpha: int = 18,
    brightness: float = 1.14,
    compact_highlight: bool = False,
    clean_highlight: bool = False,
    shell_rect_override: Optional[tuple[int, int, int, int]] = None,
) -> Image.Image:
    transparent_rgb = _hex_to_rgb(THEME["transparent"])
    shell_rect = shell_rect_override or (5, 5, width - 5, height - 7)
    shell_radius = radius if radius is not None else max(1, (shell_rect[3] - shell_rect[1]) // 2)
    shell_radius = max(1, min(shell_radius, (shell_rect[2] - shell_rect[0]) // 2, (shell_rect[3] - shell_rect[1]) // 2))

    if width * height > 160000:
        render_scale = 0.42 if width * height > 250000 else 0.55
        small_width = max(1, round(width * render_scale))
        small_height = max(1, round(height * render_scale))
        small_rect = tuple(max(0, round(value * render_scale)) for value in shell_rect)
        small_radius = max(1, round(shell_radius * render_scale))
        small_screen = None
        if screen_image is not None:
            small_screen = screen_image.convert("RGB").resize((small_width, small_height), Image.Resampling.BILINEAR)
        small = make_liquid_glass_bitmap(
            small_width,
            small_height,
            small_screen,
            accent=accent,
            transparent_outside=True,
            radius=small_radius,
            blur_radius=max(1, round(blur_radius * render_scale)),
            frost_alpha=frost_alpha,
            tint_alpha=tint_alpha,
            brightness=brightness,
            compact_highlight=compact_highlight,
            clean_highlight=clean_highlight,
            shell_rect_override=small_rect,
        )
        scaled = small.resize((width, height), Image.Resampling.BICUBIC)
        if transparent_outside:
            return scaled
        alpha = scaled.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
        scaled.putalpha(alpha)
        output = Image.new("RGBA", (width, height), (*transparent_rgb, 255))
        output.alpha_composite(scaled)
        return output.convert("RGB")

    mask, refraction_alpha, top_alpha, bottom_alpha, rim_alpha, binary_mask = make_liquid_glass_mask_layers(
        width,
        height,
        shell_rect,
        shell_radius,
        clean_highlight,
    )

    if screen_image is None:
        backdrop = create_fallback_backdrop(width, height)
    else:
        backdrop = screen_image if screen_image.mode == "RGB" else screen_image.convert("RGB")
        if backdrop.size != (width, height):
            backdrop = backdrop.resize((width, height), Image.Resampling.LANCZOS)

    # Layer 1: blurred backdrop seen through the capsule.
    glass = backdrop.filter(ImageFilter.GaussianBlur(blur_radius))
    glass = ImageEnhance.Color(glass).enhance(0.82)
    glass = ImageEnhance.Contrast(glass).enhance(0.94)
    glass = ImageEnhance.Brightness(glass).enhance(brightness).convert("RGBA")

    # Liquid-glass refraction: magnify the source only through the thick edge band.
    pad_x = min(max(3, width // 20), 24)
    pad_y = min(max(4, height // 4), 18)
    refracted = backdrop.resize((width + pad_x * 2, height + pad_y * 2), Image.Resampling.BICUBIC)
    refracted = refracted.crop((pad_x, pad_y, pad_x + width, pad_y + height))
    refracted = ImageEnhance.Contrast(refracted).enhance(1.16)
    refracted = ImageEnhance.Brightness(refracted).enhance(1.05)
    refracted = refracted.filter(ImageFilter.GaussianBlur(max(1, blur_radius // 5))).convert("RGBA")
    refracted.putalpha(refraction_alpha)
    glass.alpha_composite(refracted)

    # Layer 2: frosted material tint. Kept deliberately light so the backdrop still reads through it.
    if frost_alpha > 0:
        body = Image.new("RGBA", (width, height), (255, 255, 255, frost_alpha))
        glass.alpha_composite(body)
    tint_rgb = _hex_to_rgb(accent)
    if tint_alpha > 0:
        tint = Image.new("RGBA", (width, height), (*tint_rgb, tint_alpha))
        glass.alpha_composite(tint)

    edge_light = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    edge_light.putalpha(top_alpha)
    glass.alpha_composite(edge_light)

    edge_shadow = Image.new("RGBA", (width, height), (35, 45, 62, 0))
    edge_shadow.putalpha(bottom_alpha)
    glass.alpha_composite(edge_shadow)

    # Layer 3: specular highlights and refractive edge catches.
    rim = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    rim.putalpha(rim_alpha)
    glass.alpha_composite(rim)

    if not clean_highlight:
        shine = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(shine)
        if compact_highlight:
            draw.line(
                (
                    shell_rect[0] + 12,
                    shell_rect[1] + 9,
                    shell_rect[2] - 12,
                    shell_rect[1] + 7,
                ),
                fill=(255, 255, 255, 150),
                width=2,
            )
            draw.line(
                (
                    shell_rect[0] + 16,
                    shell_rect[3] - 8,
                    shell_rect[2] - 16,
                    shell_rect[3] - 9,
                ),
                fill=(*tint_rgb, 70),
                width=1,
            )
        else:
            draw.rounded_rectangle(
                (shell_rect[0] + 3, shell_rect[1] + 3, shell_rect[2] - 3, shell_rect[3] - 3),
                radius=max(1, shell_radius - 3),
                outline=(255, 255, 255, 118),
                width=1,
            )
            draw.arc(
                (shell_rect[0] + 7, shell_rect[1] + 7, shell_rect[0] + 118, shell_rect[3] - 5),
                start=102,
                end=248,
                fill=(255, 255, 255, 210),
                width=3,
            )
            draw.arc(
                (shell_rect[2] - 132, shell_rect[1] + 8, shell_rect[2] - 8, shell_rect[3] - 4),
                start=292,
                end=24,
                fill=(*tint_rgb, 136),
                width=2,
            )
            draw.line(
                (shell_rect[0] + 38, shell_rect[1] + 12, shell_rect[2] - 44, shell_rect[1] + 9),
                fill=(255, 255, 255, 170),
                width=2,
            )
            draw.line(
                (shell_rect[0] + 54, shell_rect[3] - 10, shell_rect[2] - 62, shell_rect[3] - 13),
                fill=(*tint_rgb, 92),
                width=2,
            )
        glass.alpha_composite(shine)

    glass.putalpha(mask)
    if transparent_outside:
        return glass
    glass.putalpha(binary_mask)
    output = Image.new("RGBA", (width, height), (*transparent_rgb, 255))
    output.alpha_composite(glass)
    return output.convert("RGB")


@dataclass(slots=True)
class BubbleGlassRenderRequest:
    generation: int
    backdrop: Image.Image
    width: int
    height: int
    button_specs: tuple[tuple[int, int, int, int], ...]
    handle_spec: tuple[int, int, int]


@dataclass(slots=True)
class BubbleGlassFrame:
    generation: int
    backdrop: Image.Image
    bubble_bitmap: Image.Image
    button_bitmaps: tuple[Image.Image, ...]
    handle_bitmap: Image.Image


@dataclass(slots=True)
class BubbleGlassRenderResult:
    generation: int
    frame: Optional[BubbleGlassFrame] = None
    error: Optional[str] = None


@dataclass(slots=True)
class DragScreenSnapshotResult:
    session: int
    snapshot: Optional[Image.Image]
    origin: tuple[int, int]


def render_bubble_glass_frame(request: BubbleGlassRenderRequest) -> BubbleGlassFrame:
    backdrop = request.backdrop if request.backdrop.mode == "RGB" else request.backdrop.convert("RGB")
    if backdrop.size != (request.width, request.height):
        backdrop = backdrop.resize((request.width, request.height), Image.Resampling.LANCZOS)

    bubble_bitmap = make_liquid_glass_bitmap(
        request.width,
        request.height,
        backdrop,
        accent="#ffffff",
        tint_alpha=0,
        clean_highlight=True,
    )
    button_bitmaps = tuple(
        make_liquid_glass_bitmap(
            button_width,
            button_height,
            backdrop.crop((x, y, x + button_width, y + button_height)),
            accent="#ffffff",
            transparent_outside=True,
            radius=button_height // 2,
            blur_radius=20,
            frost_alpha=72,
            tint_alpha=0,
            brightness=1.2,
            compact_highlight=True,
            clean_highlight=True,
        )
        for x, y, button_width, button_height in request.button_specs
    )

    handle_left, handle_top, handle_size = request.handle_spec
    handle_bitmap = make_liquid_glass_bitmap(
        handle_size,
        handle_size,
        backdrop.crop(
            (
                handle_left,
                handle_top,
                handle_left + handle_size,
                handle_top + handle_size,
            )
        ),
        accent="#ffffff",
        transparent_outside=True,
        radius=handle_size // 2 - 3,
        blur_radius=18,
        frost_alpha=34,
        tint_alpha=0,
        brightness=1.12,
        compact_highlight=True,
        clean_highlight=True,
        shell_rect_override=(3, 3, handle_size - 3, handle_size - 3),
    )
    return BubbleGlassFrame(
        generation=request.generation,
        backdrop=backdrop,
        bubble_bitmap=bubble_bitmap,
        button_bitmaps=button_bitmaps,
        handle_bitmap=handle_bitmap,
    )


def draw_liquid_glass_round_rect(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    fill: str = THEME["glass_fill"],
    outline: str = THEME["glass_edge"],
    tint: str = THEME["line"],
    width: int = 1,
) -> None:
    height = max(1, y2 - y1)
    radius = max(1, min(radius, (x2 - x1) // 2, height // 2))

    # Layer 1: depth and blurred backdrop color, approximated with soft stacked shapes.
    draw_round_rect(
        canvas,
        x1 + 1,
        y1 + 6,
        x2 - 1,
        y2 + 4,
        radius,
        fill=THEME["glass_shadow_soft"],
        outline="",
    )
    draw_round_rect(
        canvas,
        x1 + 3,
        y1 + 3,
        x2 - 3,
        y2 + 2,
        radius,
        fill=THEME["glass_underlay"],
        outline="",
    )

    # Layer 2: frosted body with a neutral rim only.
    rim = blend_color(THEME["glass_shadow"], fill, 0.62)
    draw_round_rect(canvas, x1, y1, x2, y2, radius, fill=fill, outline=rim, width=width)


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
    draw_liquid_glass_round_rect(
        canvas,
        x1,
        y1,
        x2,
        y2,
        max(1, (y2 - y1) // 2),
        fill=fill,
        outline=outline,
        tint=outline,
        width=width,
    )


def draw_glass_panel(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    fill: str,
    outline: str,
    tint: str,
    width: int = 2,
) -> None:
    draw_liquid_glass_round_rect(canvas, x1, y1, x2, y2, radius, fill, outline, tint, width)


def create_drawer_header(master: tk.Misc, title: str, icon: str, width: int) -> tk.Canvas:
    header = tk.Canvas(
        master,
        width=width,
        height=54,
        bg=THEME["glass_shadow_soft"],
        highlightthickness=0,
        bd=0,
    )
    header.create_text(
        24,
        28,
        anchor="w",
        text=title,
        fill=THEME["ink"],
        font=("Microsoft YaHei UI", 11, "bold"),
    )
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
        fill: str = THEME["glass_underlay"],
        active_fill: str = THEME["panel_3"],
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
            fill = blend_color(THEME["glass_underlay"], self.accent, 0.08)
        draw_glass_panel(
            self,
            4,
            4,
            self.width_value - 4,
            self.height_value - 4,
            self.height_value // 2,
            fill=fill,
            outline=THEME["glass_edge"],
            tint=self.accent,
            width=1,
        )
        text_x = self.width_value // 2
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
            self.after(1, self.command)


class StadiumButton(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Callable[[], None],
        width: int = 104,
        height: int = 40,
        fill: str = THEME["glass_underlay"],
        active_fill: str = THEME["panel_3"],
        outline: str = THEME["line"],
        fg: str = THEME["ink"],
        canvas_bg: str = THEME["glass_shadow_soft"],
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
            fill = blend_color(THEME["glass_underlay"], "#ffffff", 0.08)
        draw_capsule(
            self,
            0,
            0,
            self.width_value,
            self.height_value - 1,
            fill=fill,
            outline=THEME["glass_edge"],
            width=1,
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
            self.after(1, self.command)


class GlassCanvasButton:
    def __init__(
        self,
        canvas: tk.Canvas,
        x: int,
        y: int,
        width: int,
        height: int,
        text: str,
        command: Callable[[], None],
        accent: str,
        fill: str,
        active_fill: str,
        backdrop: Optional[Image.Image] = None,
    ) -> None:
        self.canvas = canvas
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.text = text
        self.command = command
        self.accent = accent
        self.fill = fill
        self.active_fill = active_fill
        self.backdrop = backdrop
        self.image_ref: Optional[ImageTk.PhotoImage] = None
        self.tag = f"glass_button_{id(self)}"
        self.image_tag = f"{self.tag}_image"
        self.hover = False
        self.pressed = False
        self._draw()
        self.canvas.tag_bind(self.tag, "<Enter>", self._on_enter)
        self.canvas.tag_bind(self.tag, "<Leave>", self._on_leave)
        self.canvas.tag_bind(self.tag, "<ButtonPress-1>", self._on_press)
        self.canvas.tag_bind(self.tag, "<ButtonRelease-1>", self._on_release)

    def _tag_new_items(self, before: set[int]) -> None:
        after = set(self.canvas.find_all())
        for item in after - before:
            self.canvas.addtag_withtag(self.tag, item)

    def _draw(self, rendered_bitmap: Optional[Image.Image] = None) -> None:
        self.canvas.delete(self.tag)
        before = set(self.canvas.find_all())
        if self.backdrop is not None:
            bitmap = rendered_bitmap
            if bitmap is None:
                crop = self.backdrop.crop((self.x, self.y, self.x + self.width, self.y + self.height))
                bitmap = make_liquid_glass_bitmap(
                    self.width,
                    self.height,
                    crop,
                    accent="#ffffff",
                    transparent_outside=True,
                    radius=self.height // 2,
                    blur_radius=20,
                    frost_alpha=72,
                    tint_alpha=0,
                    brightness=1.2,
                    compact_highlight=True,
                    clean_highlight=True,
                )
            self.image_ref = ImageTk.PhotoImage(bitmap)
            self.canvas.create_image(
                self.x,
                self.y,
                anchor="nw",
                image=self.image_ref,
                tags=(self.tag, self.image_tag),
            )
        else:
            fill = self.active_fill if self.hover else self.fill
            if self.pressed:
                fill = blend_color(self.active_fill, self.accent, 0.12)
            draw_capsule(
                self.canvas,
                self.x,
                self.y,
                self.x + self.width,
                self.y + self.height,
                fill=fill,
                outline=THEME["glass_edge"],
                width=1,
            )
        self._tag_new_items(before)
        self.canvas.create_text(
            self.x + self.width // 2,
            self.y + self.height // 2,
            text=self.text,
            fill=THEME["ink"],
            font=("Microsoft YaHei UI", 9, "bold"),
            tags=(self.tag,),
        )

    def _on_enter(self, _event: tk.Event) -> str:
        self.hover = True
        return "break"

    def _on_leave(self, _event: tk.Event) -> str:
        self.hover = False
        self.pressed = False
        return "break"

    def _on_press(self, _event: tk.Event) -> str:
        self.pressed = True
        return "break"

    def _on_release(self, _event: tk.Event) -> str:
        was_pressed = self.pressed
        self.pressed = False
        if was_pressed:
            self.canvas.after(1, self.command)
        return "break"

    def set_backdrop(self, backdrop: Image.Image) -> None:
        self.backdrop = backdrop
        self._draw()

    def set_rendered_backdrop(self, backdrop: Image.Image, bitmap: Image.Image) -> None:
        self.backdrop = backdrop
        image_items = self.canvas.find_withtag(self.image_tag)
        if not image_items:
            self._draw(bitmap)
            return
        self.image_ref = ImageTk.PhotoImage(bitmap)
        for image_item in image_items:
            self.canvas.itemconfigure(image_item, image=self.image_ref)


class GlassScrollBar(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        command: Callable[[float], None],
        width: int = 18,
        canvas_bg: str = THEME["field"],
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
        self.create_line(
            w // 2,
            track_top,
            w // 2,
            track_bottom,
            fill=THEME["glass_shadow_soft"],
            width=6,
            capstyle="round",
        )

        top, bottom = self._thumb_bounds()
        draw_glass_panel(
            self,
            2,
            top,
            w - 2,
            bottom,
            8,
            fill=THEME["panel_2"],
            outline=THEME["glass_edge"],
            tint=THEME["line"],
            width=1,
        )

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


class GlassScrollText(tk.Frame):
    def __init__(self, master: tk.Misc, **text_kwargs) -> None:
        bg = text_kwargs.pop("bg", THEME["field_glass"])
        fg = text_kwargs.pop("fg", THEME["ink"])
        insertbackground = text_kwargs.pop("insertbackground", THEME["ink"])
        selectbackground = text_kwargs.pop("selectbackground", THEME["selection"])
        text_kwargs.pop("highlightbackground", None)
        text_kwargs.pop("relief", None)
        text_kwargs.pop("bd", None)
        text_kwargs.pop("highlightthickness", None)
        super().__init__(master, bg=THEME["field_outer"], highlightthickness=0, bd=0)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._panel_canvas = tk.Canvas(self, bg=THEME["field_outer"], highlightthickness=0, bd=0)
        self._panel_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.bind("<Configure>", self._draw_panel, add="+")

        self.text = tk.Text(
            self,
            bg=bg,
            fg=fg,
            insertbackground=insertbackground,
            selectbackground=selectbackground,
            relief="flat",
            bd=0,
            borderwidth=0,
            highlightthickness=0,
            highlightbackground=bg,
            highlightcolor=bg,
            padx=10,
            pady=8,
            **text_kwargs,
        )
        self.scrollbar = GlassScrollBar(self, command=self.text.yview_moveto, canvas_bg=bg)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew", padx=(18, 2), pady=14)
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(2, 14), pady=14)
        self.text.bind("<MouseWheel>", self._on_mousewheel, add="+")
        self.text.bind("<Button-4>", self._on_linux_scroll_up, add="+")
        self.text.bind("<Button-5>", self._on_linux_scroll_down, add="+")

    def _draw_panel(self, _event: Optional[tk.Event] = None) -> None:
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        self._panel_canvas.delete("all")
        draw_round_rect(
            self._panel_canvas,
            1,
            1,
            width - 2,
            height - 2,
            18,
            fill=THEME["field_glass"],
            outline=THEME["field_glass_edge"],
            width=1,
        )
        draw_round_rect(
            self._panel_canvas,
            3,
            3,
            width - 4,
            height - 4,
            16,
            fill="",
            outline=blend_color(THEME["field_glass"], "#ffffff", 0.68),
            width=1,
        )
        self.tk.call("lower", self._panel_canvas._w)

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
        apply_transparent_window(self)
        self._glass_backdrop_ref: Optional[ImageTk.PhotoImage] = None
        self._glass_backdrop_bitmap: Optional[Image.Image] = None
        self._glass_button_backdrop: Optional[Image.Image] = None
        self._glass_backdrop_canvas = tk.Canvas(
            self,
            bg=THEME["transparent"],
            highlightthickness=0,
            bd=0,
        )
        self._glass_backdrop_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._button_overlay_ref: Optional[ImageTk.PhotoImage] = None
        self._button_overlay_buttons: list[GlassCanvasButton] = []
        self._button_overlay_canvas = tk.Canvas(
            self,
            bg=THEME["transparent"],
            highlightthickness=0,
            bd=0,
        )
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
        self._refresh_glass_backdrop(self._target_height, capture=False)
        self.deiconify()
        self.lift()
        self._animate_open(58)
        self.after(80, lambda: self._refresh_glass_backdrop(self._target_height, capture=True))

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
        x, y, width, height = self._drawer_bbox(height)
        return f"{width}x{height}+{x}+{y}"

    def _drawer_bbox(self, height: int) -> tuple[int, int, int, int]:
        self.anchor_window.update_idletasks()
        screen_width = self.anchor_window.winfo_screenwidth()
        anchor_x = self.anchor_window.winfo_rootx()
        anchor_y = self.anchor_window.winfo_rooty()
        anchor_w = max(1, self.anchor_window.winfo_width())
        width = self._target_width
        x = anchor_x + anchor_w // 2 - width // 2
        x = min(max(12, x), max(12, screen_width - width - 12))
        y = max(12, anchor_y - height - 8)
        return x, y, width, height

    def _refresh_glass_backdrop(self, height: int, capture: bool = True) -> None:
        width = self._target_width
        height = max(58, min(height, self._target_height))
        x, y, width, height = self._drawer_bbox(height)
        was_viewable = bool(self.winfo_viewable())
        if was_viewable:
            self.withdraw()
            self.update_idletasks()
            self.update()
        screen_image = None
        if capture:
            try:
                screen_image = grab_screen_region(x, y, width, height)
            finally:
                if was_viewable:
                    self.deiconify()
                    self.lift()
        elif was_viewable:
            self.deiconify()
            self.lift()
        if screen_image is None:
            self._glass_button_backdrop = create_fallback_backdrop(width, height)
        else:
            self._glass_button_backdrop = screen_image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
        bitmap = make_liquid_glass_bitmap(
            width,
            height,
            screen_image,
            accent="#ffffff",
            radius=28,
            blur_radius=22,
            frost_alpha=48,
            tint_alpha=0,
            brightness=1.12,
            clean_highlight=True,
        )
        self._glass_backdrop_bitmap = bitmap.convert("RGB")
        self._glass_backdrop_ref = ImageTk.PhotoImage(bitmap)
        self._glass_backdrop_canvas.configure(width=width, height=height)
        self._glass_backdrop_canvas.delete("all")
        self._glass_backdrop_canvas.create_image(
            0,
            0,
            anchor="nw",
            image=self._glass_backdrop_ref,
        )
        self.update_idletasks()
        self._draw_background_overlays(self._glass_backdrop_canvas, width, height)
        self.tk.call("lower", self._glass_backdrop_canvas._w)
        self._refresh_button_overlay(width, height)

    def _draw_background_overlays(self, _canvas: tk.Canvas, _width: int, _height: int) -> None:
        return

    def _drawer_button_specs(self, _width: int, _height: int) -> list[tuple[int, int, int, int, str, Callable[[], None]]]:
        return []

    def _refresh_button_overlay(self, width: int, height: int) -> None:
        specs = self._drawer_button_specs(width, height)
        if not specs:
            self._button_overlay_canvas.place_forget()
            self._button_overlay_buttons = []
            self._button_overlay_ref = None
            return

        left = min(spec[0] for spec in specs)
        top = min(spec[1] for spec in specs)
        right = max(spec[0] + spec[2] for spec in specs)
        bottom = max(spec[1] + spec[3] for spec in specs)
        overlay_width = right - left
        overlay_height = bottom - top
        self._button_overlay_canvas.configure(width=overlay_width, height=overlay_height)
        self._button_overlay_canvas.place(x=left, y=top, width=overlay_width, height=overlay_height)
        self._button_overlay_canvas.delete("all")

        if self._glass_backdrop_bitmap is not None:
            background = self._glass_backdrop_bitmap.crop((left, top, right, bottom))
        else:
            background = create_fallback_backdrop(overlay_width, overlay_height)
        self._button_overlay_ref = ImageTk.PhotoImage(background)
        self._button_overlay_canvas.create_image(0, 0, anchor="nw", image=self._button_overlay_ref)

        if self._glass_button_backdrop is not None:
            button_backdrop = self._glass_button_backdrop.crop((left, top, right, bottom))
        else:
            button_backdrop = create_fallback_backdrop(overlay_width, overlay_height)

        self._button_overlay_buttons = [
            GlassCanvasButton(
                self._button_overlay_canvas,
                x - left,
                y - top,
                button_width,
                button_height,
                text=text,
                command=command,
                accent="#ffffff",
                fill=THEME["glass_underlay"],
                active_fill=THEME["panel_3"],
                backdrop=button_backdrop,
            )
            for x, y, button_width, button_height, text, command in specs
        ]
        self.tk.call("raise", self._button_overlay_canvas._w)

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
        self.after(8, lambda: self._animate_open(next_height))

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
        self.after(8, lambda: self._animate_close(next_height, after_close))


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
        self.root.after(50, self._show_overlay)

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
            outline=THEME["line"],
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

        self.root.after(20, lambda: self._grab_region(left, top, right, bottom))

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
        self._question_label_y = 78

        self.configure(bg=THEME["transparent"])
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, minsize=54)
        self.rowconfigure(2, minsize=28)
        self.rowconfigure(3, weight=1)
        self.rowconfigure(4, minsize=56)
        self._drawer_title = title

        if image is not None:
            preview = self._make_preview(image)
            preview_panel = self._make_preview_panel(preview)
            preview_panel.grid(row=1, column=0, sticky="ew", padx=14, pady=(10, 8))
            self.preview_ref = preview
            self._question_label_y = 54 + 10 + preview.height() + 24 + 8 + 18
        elif body_text is not None:
            snippet = body_text.strip()
            if len(snippet) > 900:
                snippet = snippet[:900] + "\n..."
            text_preview = GlassScrollText(
                self,
                height=8,
                wrap="word",
                font=("Microsoft YaHei UI", 10),
                bg=THEME["field"],
                fg=THEME["ink"],
                insertbackground=THEME["ink"],
                selectbackground=THEME["selection"],
                highlightthickness=1,
                highlightbackground=THEME["glass_edge"],
                relief="solid",
                bd=1,
            )
            text_preview.insert("1.0", snippet)
            text_preview.configure(state="disabled")
            text_preview.update_idletasks()
            preview_height = max(160, int(text_preview.winfo_reqheight() * 0.9))
            tk.Frame.configure(text_preview, height=preview_height)
            text_preview.grid_propagate(False)
            text_preview.grid(row=1, column=0, sticky="nsew", padx=14, pady=(10, 8))
            self._question_label_y = 54 + 10 + preview_height + 8 + 18

        self.text = GlassScrollText(
            self,
            height=5,
            width=64,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            bg=THEME["field"],
            fg=THEME["ink"],
            insertbackground=THEME["ink"],
            selectbackground=THEME["selection"],
            highlightthickness=1,
            highlightbackground=THEME["glass_edge"],
            relief="solid",
            bd=1,
        )
        self.text.insert("1.0", default_question)
        self.text.grid(row=3, column=0, sticky="nsew", padx=14, pady=(0, 8))
        self.drawer_focus_widget = self.text

        self.bind("<Control-Return>", lambda _event: self._submit())
        self.protocol("WM_DELETE_WINDOW", self.close_drawer)
        self.geometry("620x58+0+0")
        self.focus_force()
        self.text.focus_set()

    def _draw_background_overlays(self, canvas: tk.Canvas, width: int, height: int) -> None:
        canvas.create_text(
            24,
            28,
            anchor="w",
            text=self._drawer_title,
            fill=THEME["ink"],
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        question_y = self._question_label_y
        canvas.create_text(
            16,
            question_y,
            anchor="w",
            text="问题 / 指令",
            fill=THEME["ink"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )

    def _drawer_button_specs(self, width: int, height: int) -> list[tuple[int, int, int, int, str, Callable[[], None]]]:
        button_width = 92
        button_height = 40
        button_gap = 10
        button_y = height - 54
        cancel_x = width - 14 - button_width
        send_x = cancel_x - button_gap - button_width
        return [
            (send_x, button_y, button_width, button_height, "发送", self._submit),
            (cancel_x, button_y, button_width, button_height, "取消", self.close_drawer),
        ]

    @staticmethod
    def _make_preview(image: Image.Image) -> ImageTk.PhotoImage:
        preview = image.copy()
        preview.thumbnail((560, 260))
        return ImageTk.PhotoImage(preview)

    def _make_preview_panel(self, preview: ImageTk.PhotoImage) -> tk.Canvas:
        panel_height = preview.height() + 24
        panel = tk.Canvas(
            self,
            height=panel_height,
            bg=THEME["field_outer"],
            highlightthickness=0,
            bd=0,
        )
        panel.preview_ref = preview  # type: ignore[attr-defined]

        def draw_preview_panel(_event: Optional[tk.Event] = None) -> None:
            width = max(1, panel.winfo_width())
            panel.delete("all")
            draw_round_rect(
                panel,
                0,
                0,
                width,
                panel_height,
                18,
                fill=THEME["field_glass"],
                outline=THEME["field_glass_edge"],
                width=1,
            )
            panel.create_image(width // 2, panel_height // 2, image=preview)

        panel.bind("<Configure>", draw_preview_panel, add="+")
        panel.after_idle(draw_preview_panel)
        return panel

    def _submit(self) -> None:
        question = self.text.get("1.0", "end").strip()
        self.close_drawer(lambda: self.on_submit(question))


class ResultWindow(DrawerToplevel):
    def __init__(self, parent: tk.Tk, initial_text: str) -> None:
        super().__init__(parent, drawer_width=720, drawer_height=560)
        self.title("LLM 回答")
        self.attributes("-topmost", True)
        self.configure(bg=THEME["transparent"])
        self.geometry("720x58+0+0")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, minsize=54)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(2, minsize=56)
        self._drawer_title = "LLM 回答"

        self.text = GlassScrollText(
            self,
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            bg=THEME["field"],
            fg=THEME["ink"],
            insertbackground=THEME["ink"],
            selectbackground=THEME["selection"],
            highlightthickness=1,
            highlightbackground=THEME["glass_edge"],
            relief="solid",
            bd=1,
        )
        self.text.insert("1.0", initial_text)
        self.text.configure(state="disabled")
        self.text.grid(row=1, column=0, sticky="nsew", padx=14, pady=14)

    def _draw_background_overlays(self, canvas: tk.Canvas, width: int, height: int) -> None:
        canvas.create_text(
            24,
            28,
            anchor="w",
            text=self._drawer_title,
            fill=THEME["ink"],
            font=("Microsoft YaHei UI", 11, "bold"),
        )

    def _drawer_button_specs(self, width: int, height: int) -> list[tuple[int, int, int, int, str, Callable[[], None]]]:
        button_width = 92
        button_height = 40
        button_gap = 10
        button_y = height - 54
        copy_x = width - 14 - button_width
        close_x = copy_x - button_gap - button_width
        return [
            (close_x, button_y, button_width, button_height, "关闭", self.close_drawer),
            (copy_x, button_y, button_width, button_height, "复制", self.copy_answer),
        ]

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
        self.root.attributes("-alpha", 0.97)
        apply_transparent_window(self.root)
        screen_height = self.root.winfo_screenheight()
        initial_y = max(80, screen_height - 168)
        self.root.geometry(f"372x62+120+{initial_y}")

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
        self._bubble_drag_position = (120, initial_y)
        self._bubble_moved = False
        self._drag_screen_snapshot: Optional[Image.Image] = None
        self._drag_screen_origin = (0, 0)
        self._drag_snapshot_session = 0
        self._drag_snapshot_capture_pending = False
        self._drag_snapshot_final_render_pending = False
        self._drag_in_progress = False
        self._drag_snapshot_results: queue.Queue[DragScreenSnapshotResult] = queue.Queue()
        self._drag_snapshot_poll_after_id: Optional[str] = None
        self._drag_timer_resolution_active = False
        self._drawer_reposition_after_id: Optional[str] = None
        self._bubble_render_requests: queue.Queue[Optional[BubbleGlassRenderRequest]] = queue.Queue(maxsize=1)
        self._bubble_render_results: queue.Queue[BubbleGlassRenderResult] = queue.Queue(maxsize=2)
        self._bubble_render_generation = 0
        self._bubble_render_latest_requested = 0
        self._bubble_render_latest_completed = 0
        self._bubble_render_latest_applied = 0
        self._bubble_render_poll_after_id: Optional[str] = None
        self._bubble_renderer_closed = False
        self._bubble_render_error_reported = False
        self._bubble_render_thread: Optional[threading.Thread] = None
        self.tray_icon = None

        self._build_bubble()
        self._start_bubble_renderer()
        self._setup_tray_icon()

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self._stop_bubble_renderer()

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
        draw.ellipse((8, 10, 58, 60), fill=(177, 195, 218, 96))
        draw.ellipse((5, 5, 57, 57), fill=(249, 252, 255, 238), outline=(255, 255, 255, 255), width=2)
        draw.arc((10, 9, 54, 53), start=205, end=318, fill=(95, 151, 240, 210), width=3)
        draw.arc((12, 10, 50, 47), start=28, end=138, fill=(255, 255, 255, 245), width=3)
        draw.text((27, 20), "?", fill=(23, 32, 51, 255))
        return image

    def quit_app(self) -> None:
        self._stop_bubble_renderer()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.root.destroy()

    def _start_bubble_renderer(self) -> None:
        self._bubble_render_thread = threading.Thread(
            target=self._bubble_render_worker,
            name="bubble-glass-renderer",
            daemon=True,
        )
        self._bubble_render_thread.start()

    def _bubble_render_worker(self) -> None:
        while True:
            request = self._bubble_render_requests.get()
            if request is None:
                return

            # A drag can produce hundreds of positions per second. Render only the newest one.
            while True:
                try:
                    newer_request = self._bubble_render_requests.get_nowait()
                except queue.Empty:
                    break
                if newer_request is None:
                    return
                request = newer_request

            try:
                result = BubbleGlassRenderResult(
                    generation=request.generation,
                    frame=render_bubble_glass_frame(request),
                )
            except Exception:
                result = BubbleGlassRenderResult(
                    generation=request.generation,
                    error=traceback.format_exc(),
                )
            self._put_latest_bubble_render_result(result)

    def _put_latest_bubble_render_result(self, result: BubbleGlassRenderResult) -> None:
        while True:
            try:
                self._bubble_render_results.put_nowait(result)
                return
            except queue.Full:
                try:
                    self._bubble_render_results.get_nowait()
                except queue.Empty:
                    pass

    def _stop_bubble_renderer(self) -> None:
        if self._bubble_renderer_closed:
            return
        self._bubble_renderer_closed = True
        if self._drag_timer_resolution_active:
            end_high_resolution_timer()
            self._drag_timer_resolution_active = False
        self._drag_snapshot_session += 1
        self._drag_snapshot_capture_pending = False
        for after_id_name in (
            "_bubble_render_poll_after_id",
            "_drag_snapshot_poll_after_id",
            "_drawer_reposition_after_id",
        ):
            after_id = getattr(self, after_id_name, None)
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
                setattr(self, after_id_name, None)

        while True:
            try:
                self._bubble_render_requests.get_nowait()
            except queue.Empty:
                break
        try:
            self._bubble_render_requests.put_nowait(None)
        except queue.Full:
            pass
        render_thread = self._bubble_render_thread
        if render_thread is not None and render_thread.is_alive():
            render_thread.join(timeout=0.2)
        self._bubble_render_thread = None

    def _capture_bubble_backdrop(self, width: int, height: int, hide_window: bool = False) -> Optional[Image.Image]:
        self.root.update_idletasks()
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        was_viewable = bool(self.root.winfo_viewable())
        if hide_window and was_viewable:
            self.root.withdraw()
            self.root.update_idletasks()
            self.root.update()
        try:
            try:
                return ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True)
            except TypeError:
                return ImageGrab.grab(bbox=(x, y, x + width, y + height))
            except Exception:
                return None
        finally:
            if hide_window and was_viewable:
                self.root.deiconify()
                self.root.lift()
                self.root.update_idletasks()

    def _refresh_bubble_glass(self, hide_window: bool = False) -> None:
        if not hasattr(self, "bubble_canvas"):
            return
        width = self.bubble_width
        height = self.bubble_height
        screen_image = self._capture_bubble_backdrop(width, height, hide_window=hide_window)
        self._set_bubble_glass_from_image(screen_image)

    def _set_bubble_glass_from_image(self, screen_image: Optional[Image.Image]) -> None:
        if not hasattr(self, "bubble_canvas"):
            return
        width = self.bubble_width
        height = self.bubble_height
        if screen_image is None:
            self.bubble_screen_image = create_fallback_backdrop(width, height)
        else:
            self.bubble_screen_image = screen_image if screen_image.mode == "RGB" else screen_image.convert("RGB")
            if self.bubble_screen_image.size != (width, height):
                self.bubble_screen_image = self.bubble_screen_image.resize(
                    (width, height),
                    Image.Resampling.LANCZOS,
                )
        bitmap = make_liquid_glass_bitmap(
            width,
            height,
            self.bubble_screen_image,
            accent="#ffffff",
            tint_alpha=0,
            clean_highlight=True,
        )
        self._set_bubble_canvas_bitmap(bitmap)
        for button in getattr(self, "main_buttons", []):
            button.set_backdrop(self.bubble_screen_image)
        if hasattr(self, "drag_handle_ref"):
            self._draw_drag_handle()

    def _set_bubble_canvas_bitmap(self, bitmap: Image.Image) -> None:
        self.bubble_glass_ref = ImageTk.PhotoImage(bitmap)
        glass_items = self.bubble_canvas.find_withtag("bubble_glass")
        if glass_items:
            for glass_item in glass_items:
                self.bubble_canvas.itemconfigure(glass_item, image=self.bubble_glass_ref)
            return
        self.bubble_canvas.create_image(
            0,
            0,
            anchor="nw",
            image=self.bubble_glass_ref,
            tags=("bubble_glass",),
        )
        self.bubble_canvas.tag_lower("bubble_glass")

    def _apply_bubble_glass_frame(self, frame: BubbleGlassFrame) -> None:
        self.bubble_screen_image = frame.backdrop
        self._set_bubble_canvas_bitmap(frame.bubble_bitmap)
        for button, bitmap in zip(getattr(self, "main_buttons", []), frame.button_bitmaps):
            button.set_rendered_backdrop(frame.backdrop, bitmap)
        self._draw_drag_handle(frame.handle_bitmap)

    def _apply_sampled_glass_canvas(
        self,
        window: tk.Toplevel,
        canvas: tk.Canvas,
        width: int,
        height: int,
        accent: str,
        radius: int,
    ) -> None:
        window.update_idletasks()
        x = window.winfo_rootx()
        y = window.winfo_rooty()
        was_viewable = bool(window.winfo_viewable())
        if was_viewable:
            window.withdraw()
            window.update_idletasks()
            window.update()
        try:
            screen_image = grab_screen_region(x, y, width, height)
        finally:
            if was_viewable:
                window.deiconify()
                window.lift()
        if screen_image is None:
            canvas.sampled_glass_screen_image = create_fallback_backdrop(width, height)  # type: ignore[attr-defined]
        else:
            canvas.sampled_glass_screen_image = screen_image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)  # type: ignore[attr-defined]
        bitmap = make_liquid_glass_bitmap(
            width,
            height,
            screen_image,
            accent="#ffffff",
            radius=radius,
            blur_radius=22,
            frost_alpha=48,
            tint_alpha=0,
            brightness=1.12,
            clean_highlight=True,
        )
        canvas.glass_backdrop_ref = ImageTk.PhotoImage(bitmap)  # type: ignore[attr-defined]
        canvas.delete("sampled_glass")
        canvas.create_image(0, 0, anchor="nw", image=canvas.glass_backdrop_ref, tags=("sampled_glass",))
        canvas.tag_lower("sampled_glass")
        for button in getattr(canvas, "inline_buttons", []):
            button.set_backdrop(canvas.sampled_glass_screen_image)  # type: ignore[attr-defined]

    def _start_drag_screen_snapshot_capture(self) -> None:
        left, top, width, height = get_virtual_screen_bounds(self.root)
        self._drag_snapshot_session += 1
        session = self._drag_snapshot_session
        self._drag_snapshot_capture_pending = True
        self._drag_screen_snapshot = None
        self._drag_screen_origin = (left, top)
        threading.Thread(
            target=self._capture_drag_screen_snapshot_worker,
            args=(session, left, top, width, height),
            name="drag-screen-capture",
            daemon=True,
        ).start()
        self._ensure_drag_snapshot_poll()

    def _capture_drag_screen_snapshot_worker(
        self,
        session: int,
        left: int,
        top: int,
        width: int,
        height: int,
    ) -> None:
        snapshot = None
        try:
            snapshot = grab_screen_region_gdi(left, top, width, height)
            if snapshot is None:
                try:
                    snapshot = ImageGrab.grab(
                        bbox=(left, top, left + width, top + height),
                        all_screens=True,
                    )
                except TypeError:
                    snapshot = ImageGrab.grab(bbox=(left, top, left + width, top + height))
        except Exception:
            pass
        if snapshot is not None and snapshot.mode != "RGB":
            snapshot = snapshot.convert("RGB")
        self._drag_snapshot_results.put(
            DragScreenSnapshotResult(
                session=session,
                snapshot=snapshot,
                origin=(left, top),
            )
        )

    def _ensure_drag_snapshot_poll(self) -> None:
        if self._bubble_renderer_closed or self._drag_snapshot_poll_after_id is not None:
            return
        self._drag_snapshot_poll_after_id = self.root.after(4, self._poll_drag_snapshot_results)

    def _poll_drag_snapshot_results(self) -> None:
        self._drag_snapshot_poll_after_id = None
        newest_result: Optional[DragScreenSnapshotResult] = None
        while True:
            try:
                result = self._drag_snapshot_results.get_nowait()
            except queue.Empty:
                break
            if result.session == self._drag_snapshot_session:
                newest_result = result

        if newest_result is not None:
            self._drag_snapshot_capture_pending = False
            self._drag_screen_snapshot = newest_result.snapshot
            self._drag_screen_origin = newest_result.origin
            if self._drag_screen_snapshot is not None and self._drag_in_progress and self._bubble_moved:
                self._refresh_bubble_glass_from_drag_snapshot(position=self._bubble_drag_position)
            elif self._drag_screen_snapshot is not None and self._drag_snapshot_final_render_pending:
                self._refresh_bubble_glass_from_drag_snapshot(
                    position=self._bubble_drag_position,
                )
                self._drag_snapshot_final_render_pending = False
                self._drag_screen_snapshot = None
            elif not self._drag_in_progress:
                self._drag_screen_snapshot = None
                self._drag_snapshot_final_render_pending = False

        if not self._bubble_renderer_closed and self._drag_snapshot_capture_pending:
            self._ensure_drag_snapshot_poll()

    def _refresh_bubble_glass_from_drag_snapshot(
        self,
        position: Optional[tuple[int, int]] = None,
    ) -> None:
        snapshot = self._drag_screen_snapshot
        if snapshot is None or self._bubble_renderer_closed:
            return
        if position is None:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
        else:
            x, y = position
        crop = crop_from_screen_snapshot(
            snapshot,
            self._drag_screen_origin,
            x,
            y,
            self.bubble_width,
            self.bubble_height,
        )
        self._bubble_render_generation += 1
        generation = self._bubble_render_generation
        request = BubbleGlassRenderRequest(
            generation=generation,
            backdrop=crop,
            width=self.bubble_width,
            height=self.bubble_height,
            button_specs=tuple(
                (button.x, button.y, button.width, button.height)
                for button in getattr(self, "main_buttons", [])
            ),
            handle_spec=(8, 9, 44),
        )
        self._bubble_render_latest_requested = generation
        self._put_latest_bubble_render_request(request)
        self._ensure_bubble_render_poll()

    def _put_latest_bubble_render_request(self, request: BubbleGlassRenderRequest) -> None:
        while not self._bubble_renderer_closed:
            try:
                self._bubble_render_requests.put_nowait(request)
                return
            except queue.Full:
                try:
                    self._bubble_render_requests.get_nowait()
                except queue.Empty:
                    pass

    def _ensure_bubble_render_poll(self) -> None:
        if self._bubble_renderer_closed or self._bubble_render_poll_after_id is not None:
            return
        self._bubble_render_poll_after_id = self.root.after(8, self._poll_bubble_render_results)

    def _poll_bubble_render_results(self) -> None:
        self._bubble_render_poll_after_id = None
        newest_frame: Optional[BubbleGlassFrame] = None
        while True:
            try:
                result = self._bubble_render_results.get_nowait()
            except queue.Empty:
                break
            self._bubble_render_latest_completed = max(
                self._bubble_render_latest_completed,
                result.generation,
            )
            if result.error is not None:
                if not self._bubble_render_error_reported:
                    print("Bubble glass background rendering failed:\n" + result.error)
                    self._bubble_render_error_reported = True
                continue
            if result.frame is not None and (
                newest_frame is None or result.frame.generation > newest_frame.generation
            ):
                newest_frame = result.frame

        if newest_frame is not None and newest_frame.generation > self._bubble_render_latest_applied:
            self._apply_bubble_glass_frame(newest_frame)
            self._bubble_render_latest_applied = newest_frame.generation

        if (
            not self._bubble_renderer_closed
            and self._bubble_render_latest_completed < self._bubble_render_latest_requested
        ):
            self._ensure_bubble_render_poll()

    def _build_bubble(self) -> None:
        width = 372
        height = 62
        self.bubble_width = width
        self.bubble_height = height
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
            widget.bind("<ButtonRelease-1>", self._finish_move, add="+")

        self._refresh_bubble_glass()

        self._draw_drag_handle()

        self.main_buttons = [
            GlassCanvasButton(
                canvas,
                64,
                11,
                92,
                40,
                text="框选问",
                command=self.capture_region,
                accent="#ffffff",
                fill=THEME["panel_2"],
                active_fill=THEME["glass_underlay"],
                backdrop=self.bubble_screen_image,
            ),
            GlassCanvasButton(
                canvas,
                166,
                11,
                92,
                40,
                text="拖文本",
                command=self.select_text_then_ask,
                accent="#ffffff",
                fill=THEME["panel_2"],
                active_fill=THEME["glass_underlay"],
                backdrop=self.bubble_screen_image,
            ),
            GlassCanvasButton(
                canvas,
                268,
                11,
                92,
                40,
                text="剪贴板",
                command=self.ask_clipboard,
                accent="#ffffff",
                fill=THEME["panel_2"],
                active_fill=THEME["glass_underlay"],
                backdrop=self.bubble_screen_image,
            ),
        ]

    def _draw_drag_handle(self, rendered_bitmap: Optional[Image.Image] = None) -> None:
        canvas = self.bubble_canvas
        cx = 30
        cy = 31
        size = 44
        left = cx - size // 2
        top = cy - size // 2
        if rendered_bitmap is not None:
            self.drag_handle_ref = ImageTk.PhotoImage(rendered_bitmap)
            handle_items = canvas.find_withtag("drag_handle")
            if handle_items:
                for handle_item in handle_items:
                    canvas.itemconfigure(handle_item, image=self.drag_handle_ref)
                return

        canvas.delete("drag_handle")
        bitmap = rendered_bitmap
        if bitmap is None:
            backdrop = getattr(self, "bubble_screen_image", None)
            if backdrop is not None:
                crop = backdrop.crop((left, top, left + size, top + size))
            else:
                crop = create_fallback_backdrop(size, size)
            bitmap = make_liquid_glass_bitmap(
                size,
                size,
                crop,
                accent="#ffffff",
                transparent_outside=True,
                radius=size // 2 - 3,
                blur_radius=18,
                frost_alpha=34,
                tint_alpha=0,
                brightness=1.12,
                compact_highlight=True,
                clean_highlight=True,
                shell_rect_override=(3, 3, size - 3, size - 3),
            )
        self.drag_handle_ref = ImageTk.PhotoImage(bitmap)
        canvas.create_image(left, top, anchor="nw", image=self.drag_handle_ref, tags=("drag_handle",))
        canvas.tag_bind("drag_handle", "<ButtonPress-1>", self._start_move)
        canvas.tag_bind("drag_handle", "<B1-Motion>", self._on_move)

    def _start_move(self, event: tk.Event) -> str:
        self.drag_start_x = event.x
        self.drag_start_y = event.y
        self._bubble_moved = False
        self._drag_in_progress = True
        self._drag_snapshot_final_render_pending = False
        self._bubble_drag_position = (self.root.winfo_x(), self.root.winfo_y())
        if not self._drag_timer_resolution_active:
            self._drag_timer_resolution_active = begin_high_resolution_timer()
        self._start_drag_screen_snapshot_capture()
        return "break"

    def _on_move(self, event: tk.Event) -> str:
        x = event.x_root - self.drag_start_x
        y = event.y_root - self.drag_start_y
        if (x, y) == self._bubble_drag_position:
            return "break"
        self.root.geometry(f"+{x}+{y}")
        self._bubble_drag_position = (x, y)
        self._bubble_moved = True
        self._refresh_bubble_glass_from_drag_snapshot(position=(x, y))
        self._schedule_reposition_open_drawers()
        return "break"

    def _finish_move(self, _event: tk.Event) -> str:
        if self._drag_timer_resolution_active:
            end_high_resolution_timer()
            self._drag_timer_resolution_active = False
        self._drag_in_progress = False
        if not self._bubble_moved:
            self._drag_screen_snapshot = None
            self._drag_snapshot_final_render_pending = False
            return "break"
        if self._drag_screen_snapshot is not None:
            self._refresh_bubble_glass_from_drag_snapshot(
                position=self._bubble_drag_position,
            )
            self._drag_screen_snapshot = None
        else:
            self._drag_snapshot_final_render_pending = self._drag_snapshot_capture_pending
        self._bubble_moved = False
        if self._drawer_reposition_after_id is not None:
            self.root.after_cancel(self._drawer_reposition_after_id)
            self._drawer_reposition_after_id = None
        self._reposition_open_drawers()
        self.root.after(160, self._refresh_open_drawer_glass)
        return "break"

    def _schedule_reposition_open_drawers(self) -> None:
        if self._drawer_reposition_after_id is not None:
            return
        if not getattr(self.root, "_drawer_windows", []):
            return
        self._drawer_reposition_after_id = self.root.after(8, self._run_scheduled_drawer_reposition)

    def _run_scheduled_drawer_reposition(self) -> None:
        self._drawer_reposition_after_id = None
        self._reposition_open_drawers()

    def _reposition_open_drawers(self) -> None:
        drawers = list(getattr(self.root, "_drawer_windows", []))
        for drawer in drawers:
            if isinstance(drawer, DrawerToplevel):
                drawer.reposition_to_anchor()

    def _refresh_open_drawer_glass(self) -> None:
        drawers = list(getattr(self.root, "_drawer_windows", []))
        for drawer in drawers:
            if isinstance(drawer, DrawerToplevel) and drawer.winfo_exists() and drawer.winfo_viewable():
                drawer._refresh_glass_backdrop(drawer.winfo_height(), capture=True)

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
        self.root.after(20, self._wait_for_button_release_before_text_drag)

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
        canvas.inline_buttons = [  # type: ignore[attr-defined]
            GlassCanvasButton(
                canvas,
                width - 106,
                height - 55,
                92,
                40,
                text="取消",
                command=self._cancel_text_drag,
                accent="#ffffff",
                fill=THEME["glass_underlay"],
                active_fill=THEME["panel_3"],
            )
        ]
        self.text_drag_tip = tip
        self._center_near_bubble(tip)
        self._apply_sampled_glass_canvas(tip, canvas, width, height, THEME["teal"], 26)

    def _wait_for_button_release_before_text_drag(self) -> None:
        if not self.text_drag_active:
            return
        if is_left_mouse_down():
            self.root.after(20, self._wait_for_button_release_before_text_drag)
            return
        self.root.after(20, self._wait_for_text_drag_start)

    def _wait_for_text_drag_start(self) -> None:
        if not self.text_drag_active:
            return
        if is_left_mouse_down():
            self.clipboard_seq_before = get_clipboard_sequence_number()
            self.root.after(35, self._wait_for_text_drag_end)
            return
        self.root.after(35, self._wait_for_text_drag_start)

    def _wait_for_text_drag_end(self) -> None:
        if not self.text_drag_active:
            return
        if is_left_mouse_down():
            self.root.after(35, self._wait_for_text_drag_end)
            return
        self.root.after(70, self._copy_selected_text_after_drag)

    def _copy_selected_text_after_drag(self) -> None:
        if not self.text_drag_active:
            return
        send_ctrl_c()
        self.root.after(70, lambda: self._read_clipboard_after_copy(12))

    def _read_clipboard_after_copy(self, retries: int) -> None:
        if not self.text_drag_active:
            return

        seq_after = get_clipboard_sequence_number()
        if self.clipboard_seq_before == seq_after and retries > 0:
            self.root.after(60, lambda: self._read_clipboard_after_copy(retries - 1))
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
        text_len = len(text.strip())
        canvas.create_text(
            24,
            38,
            anchor="w",
            text=f"已选 {text_len} 字",
            fill=THEME["ink"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )

        canvas.inline_buttons = [  # type: ignore[attr-defined]
            GlassCanvasButton(
                canvas,
                220,
                height // 2 - 20,
                92,
                40,
                text="确认",
                command=lambda: self._confirm_drag_text(text),
                accent="#ffffff",
                fill=THEME["glass_underlay"],
                active_fill=THEME["panel_3"],
            ),
            GlassCanvasButton(
                canvas,
                316,
                height // 2 - 20,
                92,
                40,
                text="取消",
                command=self._cancel_text_confirm_badge,
                accent="#ffffff",
                fill=THEME["glass_underlay"],
                active_fill=THEME["panel_3"],
            ),
        ]

        badge.bind("<Escape>", lambda _event: self._cancel_text_confirm_badge())
        self.text_confirm_badge = badge
        self._position_badge_near_pointer(badge)
        self._apply_sampled_glass_canvas(badge, canvas, width, height, THEME["line"], 31)

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
        def worker() -> None:
            try:
                answer = work()
                self.response_queue.put(("ok", answer))
            except Exception:
                self.response_queue.put(("error", traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()
        self.result_window = ResultWindow(self.root, "正在请求模型，请稍候...")
        self._center_near_bubble(self.result_window)
        self.root.after(60, self._poll_response)

    def _poll_response(self) -> None:
        try:
            status, payload = self.response_queue.get_nowait()
        except queue.Empty:
            self.root.after(60, self._poll_response)
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
