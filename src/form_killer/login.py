from __future__ import annotations

import locale
import os
import sys
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Callable
from urllib.parse import urljoin

from rich.console import Console

from form_killer.events import LoginTimeoutError, ServiceEvent, ServiceEventHandler
from form_killer.ui import render_input_header

MINI_PROGRAM_CODE_TERMINAL_WIDTH = 80


LoginEvent = ServiceEvent
LoginEventHandler = ServiceEventHandler


class CliServiceEventRenderer:
    def __init__(self, console: Console, *, provider_label: str) -> None:
        self.console = console
        self.provider_label = provider_label
        self._status: Any = None

    def __call__(self, event: ServiceEvent) -> None:
        label = event.provider_label or self.provider_label
        if event.kind == "parse_start":
            self._start_status(event.message or f"[bold cyan]正在解析{label}...[/bold cyan]")
            return
        if event.kind == "parse_retry_start":
            self._start_status(event.message or f"[bold cyan]正在使用已登录会话重新解析{label}...[/bold cyan]")
            return
        if event.kind == "dry_run_start":
            self._start_status(event.message or f"[bold cyan]正在校验{label}...[/bold cyan]")
            return
        if event.kind == "fill_start":
            self._start_status(event.message or f"[bold cyan]正在填写{label}...[/bold cyan]")
            return
        if event.kind == "login_required":
            self._stop_status()
            render_input_header(
                self.console,
                f"{label}登录",
                event.message or "请扫码/输入验证码完成登录。",
            )
            return
        if event.kind in {"checking", "login_checking"}:
            self._start_status(f"[bold cyan]Checking {label} login session...[/bold cyan]")
            return
        if event.kind in {"challenge_fetching", "login_challenge_fetching"}:
            self._start_status(f"[bold cyan]Fetching {label} login code...[/bold cyan]")
            return
        if event.kind in {"challenge", "login_challenge"}:
            self._stop_status()
            if event.image_bytes is None:
                return
            self.console.print(f"[cyan]{label} login challenge:[/cyan]")
            with self.console.status(f"[bold cyan]Rendering {label} login code...[/bold cyan]", spinner="dots"):
                terminal_output = terminal_login_challenge_output(event.image_bytes)
            write_terminal_bytes(terminal_output)
            return
        if event.kind in {"waiting", "login_waiting"}:
            self._start_status(f"[bold cyan]Waiting for {label} login...[/bold cyan]")
            return
        if event.kind in {"success", "login_success", "parse_success", "fill_success", "submit_success", "dry_run_success"}:
            self._stop_status()

    def close(self) -> None:
        self._stop_status()

    def _start_status(self, message: str) -> None:
        self._stop_status()
        self._status = self.console.status(message, spinner="dots")
        self._status.__enter__()

    def _stop_status(self) -> None:
        if self._status is None:
            return
        status = self._status
        self._status = None
        status.__exit__(None, None, None)


CliLoginEventRenderer = CliServiceEventRenderer


def run_cli_login_events(console: Console, *, provider_label: str) -> CliServiceEventRenderer:
    return CliServiceEventRenderer(console, provider_label=provider_label)


def run_cli_service_events(console: Console, *, provider_label: str) -> CliServiceEventRenderer:
    return CliServiceEventRenderer(console, provider_label=provider_label)


def run_cli_login_challenge(
    console: Console,
    page: Any,
    *,
    provider_label: str,
    is_logged_in: Callable[[], bool],
    timeout_seconds: int,
) -> None:
    if is_logged_in():
        return

    with console.status(f"[bold cyan]Fetching {provider_label} login code...[/bold cyan]", spinner="dots"):
        image_bytes = login_challenge_image_bytes(page)

    console.print(f"[cyan]{provider_label} login challenge:[/cyan]")
    with console.status(f"[bold cyan]Rendering {provider_label} login code...[/bold cyan]", spinner="dots"):
        terminal_output = terminal_login_challenge_output(image_bytes)
    write_terminal_bytes(terminal_output)

    deadline = time.monotonic() + timeout_seconds
    with console.status(f"[bold cyan]Waiting for {provider_label} login...[/bold cyan]", spinner="dots"):
        while time.monotonic() < deadline:
            if is_logged_in():
                return
            time.sleep(2)
    raise LoginTimeoutError(f"{provider_label} login timed out. Please run login again.")


def login_challenge_image_bytes(page: Any) -> bytes:
    return screenshot_login_candidate(page)


def screenshot_login_candidate(page: Any) -> bytes:
    dom_match = screenshot_dom_login_candidate(page)
    if dom_match is not None:
        return dom_match

    reveal_login_challenge(page)
    dom_match = wait_for_dom_login_candidate(page, timeout_seconds=10)
    if dom_match is not None:
        return dom_match

    image_match = screenshot_image_login_candidate(page)
    if image_match is not None:
        return image_match

    return page.screenshot(type="png", full_page=False)


def reveal_login_challenge(page: Any) -> None:
    if click_login_entry_by_text(page):
        return

    selectors = [
        'button:has-text("登录")',
        'a:has-text("登录")',
        '[role="button"]:has-text("登录")',
        'button:has-text("立即登录")',
        'a:has-text("立即登录")',
        '[role="button"]:has-text("立即登录")',
        'button:has-text("免费使用")',
        'a:has-text("免费使用")',
        '[role="button"]:has-text("免费使用")',
        'button:has-text("立即使用")',
        'a:has-text("立即使用")',
        '[role="button"]:has-text("立即使用")',
        'button:has-text("Login")',
        'a:has-text("Login")',
        '[class*="login"]:visible',
        '[id*="login"]:visible',
    ]
    for selector in selectors:
        try:
            candidates = page.locator(selector)
            count = min(candidates.count(), 8)
            for index in range(count):
                candidate = candidates.nth(index)
                box = candidate.bounding_box()
                if not box:
                    continue
                width = float(box.get("width", 0) or 0)
                height = float(box.get("height", 0) or 0)
                if width < 20 or height < 10:
                    continue
                candidate.click(timeout=3000)
                page.wait_for_timeout(3000)
                return
        except Exception:
            continue


def click_login_entry_by_text(page: Any) -> bool:
    try:
        return bool(page.evaluate(CLICK_LOGIN_ENTRY_SCRIPT))
    except Exception:
        return False


def screenshot_dom_login_candidate(page: Any) -> bytes | None:
    matches: list[tuple[float, Any, dict[str, Any]]] = []
    for frame in _candidate_frames(page):
        try:
            candidates = frame.evaluate(DOM_LOGIN_CANDIDATE_SCRIPT)
        except Exception:
            continue
        if not isinstance(candidates, list):
            continue
        for candidate in candidates[:12]:
            if not isinstance(candidate, dict):
                continue
            selector = str(candidate.get("selector") or "")
            if not selector:
                continue
            matches.append((float(candidate.get("score") or 0), frame, candidate))
    for _score, frame, candidate in sorted(matches, key=lambda item: item[0], reverse=True):
        src = str(candidate.get("src") or "")
        if src:
            image_bytes = download_dom_image(page, frame, src)
            if image_bytes is not None:
                return image_bytes
        try:
            selector = str(candidate.get("selector") or "")
            return frame.locator(selector).screenshot(type="png")
        except Exception:
            continue
    return None


def download_dom_image(page: Any, frame: Any, src: str) -> bytes | None:
    url = urljoin(str(getattr(frame, "url", "") or str(getattr(page, "url", ""))), src)
    if not url.lower().startswith(("http://", "https://", "data:")):
        return None
    if url.lower().startswith("data:"):
        return None
    try:
        response = page.context.request.get(url, timeout=10_000)
        if not response.ok:
            return None
        content_type = str(response.headers.get("content-type") or "").lower()
        body = response.body()
    except Exception:
        return None
    if "image/" not in content_type or not body:
        return None
    return body


def normalize_image_bytes(image_bytes: bytes) -> bytes:
    try:
        from PIL import Image

        image = prepare_terminal_qr_image(Image.open(BytesIO(image_bytes)))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return image_bytes


def wait_for_dom_login_candidate(page: Any, *, timeout_seconds: int) -> bytes | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        dom_match = screenshot_dom_login_candidate(page)
        if dom_match is not None:
            return dom_match
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    return None


def screenshot_image_login_candidate(page: Any) -> bytes | None:
    selectors = [
        "canvas[aria-label*='\\u4e8c\\u7ef4\\u7801']",
        "img[aria-label*='\\u4e8c\\u7ef4\\u7801']",
        "svg[aria-label*='\\u4e8c\\u7ef4\\u7801']",
        "[class*='qrCode'] canvas",
        "[class*='qrCode'] img",
        "[class*='QRCode'] canvas",
        "[class*='QRCode'] img",
        "[class*='qrcode'] canvas",
        "[class*='qrcode'] img",
        "[class*='qr'] canvas",
        "[class*='qr'] img",
        "[class*='qr'] svg",
        "[id*='qrcode'] canvas",
        "[id*='qrcode'] img",
        "[id*='qr'] canvas",
        "[id*='qr'] img",
        "[class*='captcha'] canvas",
        "[class*='captcha'] img",
        "canvas",
        "img",
        "svg",
    ]

    best: tuple[float, bytes] | None = None
    for frame in _candidate_frames(page):
        for selector in selectors:
            try:
                candidates = frame.locator(selector)
                count = min(candidates.count(), 20)
                for index in range(count):
                    candidate = candidates.nth(index)
                    box = candidate.bounding_box()
                    if not box:
                        continue
                    width = float(box.get("width", 0) or 0)
                    height = float(box.get("height", 0) or 0)
                    if width < 80 or height < 80:
                        continue
                    image_bytes = candidate.screenshot(type="png")
                    score = score_login_challenge_candidate(selector, width, height, image_bytes)
                    if best is None or score > best[0]:
                        best = (score, image_bytes)
            except Exception:
                continue

    if best and best[0] >= 35:
        return best[1]
    return None


def _candidate_frames(page: Any) -> list[Any]:
    frames = [page]
    try:
        frames.extend(frame for frame in getattr(page, "frames", []) if frame is not page)
    except Exception:
        pass
    return frames


def score_login_challenge_candidate(selector: str, width: float, height: float, image_bytes: bytes) -> float:
    score = 0.0
    lower_selector = selector.lower()
    if "qr" in lower_selector or "qrcode" in lower_selector or "\\u4e8c\\u7ef4\\u7801" in selector:
        score += 35
    if "captcha" in lower_selector:
        score += 15

    min_side = min(width, height)
    max_side = max(width, height)
    if min_side >= 140:
        score += 12
    if min_side >= 180:
        score += 8

    square_delta = abs(width - height) / max(max_side, 1)
    if square_delta <= 0.12:
        score += 30
    elif square_delta <= 0.25:
        score += 12
    else:
        score -= 35

    try:
        from PIL import Image

        image = Image.open(BytesIO(image_bytes)).convert("L").resize((64, 64))
    except Exception:
        return score

    pixels = list(_flattened_image_data(image))
    dark = [pixel < 150 for pixel in pixels]
    density = sum(dark) / len(dark)
    if 0.18 <= density <= 0.65:
        score += 20
    else:
        score -= 20

    transitions = 0
    for y in range(64):
        row = dark[y * 64 : (y + 1) * 64]
        transitions += sum(1 for left, right in zip(row, row[1:]) if left != right)
    for x in range(64):
        column = [dark[y * 64 + x] for y in range(64)]
        transitions += sum(1 for top, bottom in zip(column, column[1:]) if top != bottom)
    transition_rate = transitions / (64 * 63 * 2)
    if transition_rate >= 0.12:
        score += 35
    elif transition_rate >= 0.07:
        score += 18
    else:
        score -= 30

    return score


def render_png_as_terminal_qr(console: Console, image_bytes: bytes, *, width: int = 46) -> None:
    write_terminal_bytes(terminal_login_challenge_output(image_bytes, width=width))


def terminal_login_challenge_output(image_bytes: bytes, *, width: int = 46) -> bytes:
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("Pillow is required to render the login challenge in CLI.") from exc

    try:
        source_image = Image.open(BytesIO(image_bytes))
    except Exception as exc:
        raise RuntimeError("Failed to decode login challenge image.") from exc

    if is_mini_program_code(source_image):
        if terminal_supports_ansi_graphics():
            try:
                return render_chafa_image_bytes(source_image, width=max(width, MINI_PROGRAM_CODE_TERMINAL_WIDTH))
            except RuntimeError:
                pass
        return encode_terminal_text(render_ascii_image(source_image, width=max(width, MINI_PROGRAM_CODE_TERMINAL_WIDTH)))

    image = prepare_terminal_qr_image(source_image).convert("1")
    original_width, original_height = image.size
    if original_width <= 0 or original_height <= 0:
        raise RuntimeError("Login challenge image is empty.")

    target_width = max(24, min(width, original_width))
    target_height = max(12, int(original_height * target_width / original_width))
    if target_height % 2:
        target_height += 1
    image = image.resize((target_width, target_height), resample=0)
    if terminal_supports_unicode_blocks():
        return encode_terminal_text(render_utf_half_blocks(image))
    return encode_terminal_text(render_ascii_blocks(image))


def render_utf_half_blocks(image: Any) -> str:
    pixels = image.load()
    lines: list[str] = []
    for y in range(0, image.height, 2):
        parts: list[str] = []
        for x in range(image.width):
            top_black = pixels[x, y] == 0
            bottom_black = pixels[x, y + 1] == 0
            if top_black and bottom_black:
                parts.append("█")
            elif top_black:
                parts.append("▀")
            elif bottom_black:
                parts.append("▄")
            else:
                parts.append(" ")
        lines.append("".join(parts).rstrip())
    return "\n".join(lines)


def render_ascii_blocks(image: Any) -> str:
    pixels = image.load()
    lines: list[str] = []
    for y in range(image.height):
        parts: list[str] = []
        for x in range(image.width):
            parts.append("##" if pixels[x, y] == 0 else "  ")
        lines.append("".join(parts).rstrip())
    return "\n".join(lines)


def is_mini_program_code(image: Any) -> bool:
    size = max(image.size)
    if size <= 0:
        return False
    rgb = image.convert("RGB").resize((96, 96), resample=0)
    non_gray = 0
    dark = 0
    corner_dark = [0, 0, 0]
    corner_total = 24 * 24
    total = rgb.width * rgb.height
    for index, (red, green, blue) in enumerate(_flattened_image_data(rgb)):
        x = index % rgb.width
        y = index // rgb.width
        if max(red, green, blue) - min(red, green, blue) > 40:
            non_gray += 1
        is_dark = red < 80 and green < 80 and blue < 80
        if is_dark:
            dark += 1
            if x < 24 and y < 24:
                corner_dark[0] += 1
            elif x >= 72 and y < 24:
                corner_dark[1] += 1
            elif x < 24 and y >= 72:
                corner_dark[2] += 1
    color_ratio = non_gray / total
    dark_ratio = dark / total
    qr_corner_count = sum(1 for count in corner_dark if count / corner_total > 0.25)
    has_qr_finders = qr_corner_count >= 2
    return (color_ratio > 0.01 and dark_ratio < 0.35) or (
        0.04 <= dark_ratio <= 0.32 and not has_qr_finders
    )


def render_chafa_image(image: Any, *, width: int) -> None:
    write_terminal_bytes(render_chafa_image_bytes(image, width=width))


def render_chafa_image_bytes(image: Any, *, width: int) -> bytes:
    try:
        import chafa
    except Exception as exc:
        raise RuntimeError("chafa.py is required to render mini program login codes.") from exc

    image = image.convert("RGB")
    target_width = max(24, min(width, image.width))
    target_height = max(12, int(image.height * target_width / image.width * 0.5))
    config = chafa.CanvasConfig()
    config.width = target_width
    config.height = target_height
    config.canvas_mode = chafa.CanvasMode.CHAFA_CANVAS_MODE_TRUECOLOR
    config.pixel_mode = chafa.PixelMode.CHAFA_PIXEL_MODE_SYMBOLS
    config.dither_mode = chafa.DitherMode.CHAFA_DITHER_MODE_DIFFUSION
    symbol_map = chafa.SymbolMap()
    symbol_map.add_by_tags(
        chafa.SymbolTags.CHAFA_SYMBOL_TAG_BRAILLE
        | chafa.SymbolTags.CHAFA_SYMBOL_TAG_SEXTANT
        | chafa.SymbolTags.CHAFA_SYMBOL_TAG_STIPPLE
        | chafa.SymbolTags.CHAFA_SYMBOL_TAG_SPACE
    )
    config.set_symbol_map(symbol_map)

    canvas = chafa.Canvas(config)
    pixels = bytearray(image.tobytes())
    canvas.draw_all_pixels(
        chafa.PixelType.CHAFA_PIXEL_RGB8,
        pixels,
        image.width,
        image.height,
        image.width * 3,
    )
    return canvas.print()


def write_terminal_text(text: str) -> None:
    write_terminal_bytes(encode_terminal_text(text))


def encode_terminal_text(text: str) -> bytes:
    return text.encode(terminal_text_encoding(), errors="replace")


def terminal_text_encoding() -> str:
    return getattr(sys.stdout, "encoding", None) or locale.getpreferredencoding(False) or "utf-8"


def terminal_supports_unicode_blocks() -> bool:
    if os.getenv("FORM_KILLER_PLAIN_TERMINAL"):
        return False
    return terminal_can_encode("█▀▄")


def terminal_supports_ansi_graphics() -> bool:
    if os.getenv("NO_COLOR") or os.getenv("FORM_KILLER_PLAIN_TERMINAL"):
        return False
    if os.getenv("TERM", "").lower() == "dumb":
        return False
    isatty = getattr(sys.stdout, "isatty", None)
    return callable(isatty) and isatty() and terminal_supports_unicode_blocks()


def terminal_can_encode(text: str) -> bool:
    try:
        text.encode(terminal_text_encoding())
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def render_ascii_image(image: Any, *, width: int) -> str:
    grayscale = image.convert("L")
    target_width = max(24, min(width, grayscale.width))
    target_height = max(12, int(grayscale.height * target_width / grayscale.width * 0.5))
    grayscale = grayscale.resize((target_width, target_height), resample=0)
    ramp = " .:-=+*#%@"
    lines: list[str] = []
    for y in range(grayscale.height):
        parts: list[str] = []
        for x in range(grayscale.width):
            value = grayscale.getpixel((x, y))
            index = min(len(ramp) - 1, int((255 - value) / 255 * (len(ramp) - 1)))
            parts.append(ramp[index])
        lines.append("".join(parts).rstrip())
    return "\n".join(lines)


def write_terminal_bytes(data: bytes) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(data)
        stream.write(b"\n")
        stream.flush()
        return
    sys.stdout.write(data.decode("utf-8", errors="replace"))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _flattened_image_data(image: Any) -> Any:
    get_flattened_data = getattr(image, "get_flattened_data", None)
    if callable(get_flattened_data):
        return get_flattened_data()
    return image.getdata()


def prepare_terminal_qr_image(image: Any) -> Any:
    image = image.convert("L")
    bbox = image.point(lambda value: 0 if value > 245 else 255).getbbox()
    if bbox:
        image = image.crop(bbox)
    image = image.point(lambda value: 255 if value >= 180 else 0, mode="1").convert("RGB")
    scale = max(1, 480 // max(image.size))
    if scale > 1:
        image = image.resize((image.width * scale, image.height * scale), resample=0)
    return image


DOM_LOGIN_CANDIDATE_SCRIPT = r"""
() => {
  const esc = (value) => {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  };
  const cssPath = (node) => {
    if (node.id) return `#${esc(node.id)}`;
    const parts = [];
    let current = node;
    while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
      let part = current.localName.toLowerCase();
      if (current.classList && current.classList.length) {
        part += "." + Array.from(current.classList).slice(0, 2).map(esc).join(".");
      }
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.localName === current.localName);
        if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
      }
      parts.unshift(part);
      current = parent;
    }
    return parts.join(" > ");
  };
  const textOf = (node) => (node && (node.innerText || node.textContent || "")).replace(/\s+/g, " ").slice(0, 300);
  const attr = (node, name) => node.getAttribute(name) || "";
  const tokens = ["qr", "qrcode", "qr-code", "qr_code", "ptqr", "ptqrshow", "qrsig", "qrlogin", "scanlogin", "\u4e8c\u7ef4\u7801", "\u626b\u7801", "\u9a8c\u8bc1\u7801"];
  const negativeTokens = ["logo", "banner", "avatar", "icon", "sprite", "ad-", "loading", "apng", "feature"];
    const nodes = Array.from(document.querySelectorAll("img, canvas, svg, [style*='background'], [class*='qr'], [id*='qr'], [class*='captcha'], [id*='captcha']"));
  const candidates = [];
  for (const node of nodes) {
    const rect = node.getBoundingClientRect();
    if (rect.width < 70 || rect.height < 70) continue;
    const style = window.getComputedStyle(node);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity || "1") < 0.05) continue;
    const parent = node.parentElement;
    const grandParent = parent && parent.parentElement;
    const haystack = [
      node.localName, node.id, node.className, attr(node, "src"), attr(node, "alt"),
      attr(node, "title"), attr(node, "aria-label"), attr(node, "data-src"),
      attr(node, "data-url"), style.backgroundImage,
      parent && parent.id, parent && parent.className, grandParent && grandParent.className,
      textOf(parent)
    ].join(" ").toLowerCase();
    let score = 0;
    let tokenHits = 0;
    for (const token of tokens) {
      if (haystack.includes(token.toLowerCase())) {
        score += 25;
        tokenHits += 1;
      }
    }
    for (const token of negativeTokens) if (haystack.includes(token)) score -= 25;
    if (tokenHits === 0) continue;
    const minSide = Math.min(rect.width, rect.height);
    const maxSide = Math.max(rect.width, rect.height);
    const squareDelta = Math.abs(rect.width - rect.height) / Math.max(maxSide, 1);
    if (squareDelta <= 0.12) score += 35;
    else if (squareDelta <= 0.25) score += 15;
    else score -= 35;
    if (minSide >= 140) score += 10;
    if (minSide >= 180) score += 10;
    if (haystack.includes("ptqrshow") || haystack.includes("qrsig")) score += 60;
    if (node.localName.toLowerCase() === "img" && attr(node, "src")) score += 20;
    if (score >= 45) candidates.push({ selector: cssPath(node), score, src: attr(node, "src") || attr(node, "data-src") || "" });
  }
  return candidates.sort((a, b) => b.score - a.score);
}
"""


CLICK_LOGIN_ENTRY_SCRIPT = r"""
() => {
  const terms = ["\u767b\u5f55", "\u7acb\u5373\u767b\u5f55", "\u514d\u8d39\u4f7f\u7528", "\u7acb\u5373\u4f7f\u7528", "Login"];
  const nodes = Array.from(document.querySelectorAll("button, a, [role='button'], div, span"));
  const candidates = [];
  for (const node of nodes) {
    const text = (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
    if (!terms.includes(text)) continue;
    const rect = node.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 10) continue;
    const style = window.getComputedStyle(node);
    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity || "1") < 0.05) continue;
    candidates.push({ node, area: rect.width * rect.height });
  }
  candidates.sort((a, b) => a.area - b.area);
  const target = candidates[0] && candidates[0].node;
  if (!target) return false;
  target.click();
  return true;
}
"""
