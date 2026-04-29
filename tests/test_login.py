from __future__ import annotations

import warnings

from form_killer import login


def png_bytes(size: tuple[int, int], painter) -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("L", size, 255)
    painter(ImageDraw.Draw(image))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_run_cli_login_challenge_renders_bytes_and_polls(monkeypatch) -> None:
    events: list[str] = []
    image_bytes = b"png"

    class FakeConsole:
        def print(self, value):
            events.append(str(value))

        def status(self, *args, **kwargs):
            class Status:
                def __enter__(self):
                    events.append("status")

                def __exit__(self, *exc):
                    events.append("done")

            return Status()

    checks = iter([False, False, True])
    monkeypatch.setattr(login, "login_challenge_image_bytes", lambda page: image_bytes)
    monkeypatch.setattr(login, "terminal_login_challenge_output", lambda raw: events.append(f"render:{raw!r}") or b"terminal")
    monkeypatch.setattr(login, "write_terminal_bytes", lambda data: events.append(f"write:{data!r}"))
    monkeypatch.setattr(login.time, "sleep", lambda seconds: events.append(f"sleep:{seconds}"))

    login.run_cli_login_challenge(
        FakeConsole(),
        object(),
        provider_label="测试平台",
        is_logged_in=lambda: next(checks),
        timeout_seconds=10,
    )

    assert events.count("status") == 3
    assert "render:b'png'" in events
    assert "write:b'terminal'" in events
    assert "sleep:2" in events
    assert events[-1] == "done"


def test_run_cli_login_challenge_skips_render_when_already_logged_in(monkeypatch) -> None:
    events: list[str] = []

    class FakeConsole:
        def print(self, value):
            events.append(str(value))

        def status(self, *args, **kwargs):
            raise AssertionError("No status should render for an existing login")

    monkeypatch.setattr(login, "login_challenge_image_bytes", lambda page: events.append("image") or b"png")
    monkeypatch.setattr(login, "terminal_login_challenge_output", lambda raw: events.append("render") or b"terminal")
    monkeypatch.setattr(login, "write_terminal_bytes", lambda data: events.append("write"))

    login.run_cli_login_challenge(
        FakeConsole(),
        object(),
        provider_label="测试平台",
        is_logged_in=lambda: True,
        timeout_seconds=10,
    )

    assert events == []


def test_render_png_as_terminal_qr_uses_chafa_binding(monkeypatch) -> None:
    written: list[bytes] = []
    image_bytes = png_bytes(
        (24, 24),
        lambda draw: draw.rectangle((6, 6, 18, 18), fill=0),
    )

    monkeypatch.setattr(login, "write_terminal_bytes", lambda data: written.append(data))

    class FakeConsole:
        pass

    login.render_png_as_terminal_qr(FakeConsole(), image_bytes, width=24)

    assert written
    assert isinstance(written[0], bytes)


def test_render_png_as_terminal_qr_uses_chafa_for_mini_program_codes(monkeypatch) -> None:
    calls: list[str] = []
    image_bytes = png_bytes((120, 120), lambda draw: draw.rectangle((48, 48, 72, 72), fill=128))
    monkeypatch.setattr(login, "is_mini_program_code", lambda image: True)
    monkeypatch.setattr(login, "terminal_supports_ansi_graphics", lambda: True)
    monkeypatch.setattr(login, "render_chafa_image_bytes", lambda image, *, width: calls.append(f"chafa:{width}") or b"chafa")
    monkeypatch.setattr(login, "write_terminal_bytes", lambda data: calls.append(f"write:{data!r}"))

    class FakeConsole:
        pass

    login.render_png_as_terminal_qr(FakeConsole(), image_bytes, width=42)

    assert calls == ["chafa:80", "write:b'chafa'"]


def test_terminal_login_challenge_output_falls_back_to_ascii_when_blocks_are_not_encodable(monkeypatch) -> None:
    image_bytes = png_bytes(
        (24, 24),
        lambda draw: draw.rectangle((6, 6, 18, 18), fill=0),
    )

    class FakeStdout:
        encoding = "cp1252"

        def isatty(self):
            return True

    monkeypatch.setattr(login.sys, "stdout", FakeStdout())
    monkeypatch.setattr(login, "is_mini_program_code", lambda image: False)

    output = login.terminal_login_challenge_output(image_bytes, width=24)
    text = output.decode("cp1252")

    assert "##" in text
    assert "█" not in text
    assert "▀" not in text
    assert "▄" not in text


def test_terminal_login_challenge_output_uses_ascii_image_when_ansi_graphics_are_disabled(monkeypatch) -> None:
    image_bytes = png_bytes((120, 120), lambda draw: draw.rectangle((48, 48, 72, 72), fill=0))
    monkeypatch.setattr(login, "is_mini_program_code", lambda image: True)
    monkeypatch.setattr(login, "terminal_supports_ansi_graphics", lambda: False)
    monkeypatch.setattr(
        login,
        "render_chafa_image_bytes",
        lambda image, *, width: (_ for _ in ()).throw(AssertionError("chafa should not be used")),
    )

    output = login.terminal_login_challenge_output(image_bytes, width=42)

    assert output
    assert b"\x1b[" not in output


def test_login_challenge_image_bytes_preserves_original_image(monkeypatch) -> None:
    raw = b"raw-image"

    monkeypatch.setattr(login, "screenshot_login_candidate", lambda page: raw)
    monkeypatch.setattr(login, "normalize_image_bytes", lambda image: b"normalized")

    assert login.login_challenge_image_bytes(object()) == raw


def test_mini_program_detection_handles_gray_round_codes() -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (160, 160), "white")
    draw = ImageDraw.Draw(image)
    for offset in range(0, 56, 12):
        draw.arc((20 + offset, 20, 140 - offset, 140), 30, 300, fill="black", width=4)
    draw.ellipse((66, 66, 94, 94), fill=(120, 120, 120))

    assert login.is_mini_program_code(image) is True


def test_mini_program_detection_keeps_standard_qr_on_utf_path() -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (160, 160), "white")
    draw = ImageDraw.Draw(image)
    for x, y in ((8, 8), (112, 8), (8, 112)):
        draw.rectangle((x, y, x + 40, y + 40), outline="black", width=8)
        draw.rectangle((x + 14, y + 14, x + 26, y + 26), fill="black")
    for y in range(20, 140, 16):
        for x in range(20, 140, 16):
            if (x + y) % 32 == 0:
                draw.rectangle((x, y, x + 8, y + 8), fill="black")

    assert login.is_mini_program_code(image) is False


def test_image_scoring_avoids_pillow_getdata_deprecation_warning() -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (160, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 8, 52, 52), outline="black", width=8)
    draw.rectangle((108, 8, 152, 52), outline="black", width=8)
    draw.rectangle((8, 108, 52, 152), outline="black", width=8)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)

        assert login.score_login_challenge_candidate("img.qr", 160, 160, png_bytes((160, 160), lambda d: None)) >= 0
        assert login.is_mini_program_code(image) is False


def test_screenshot_login_candidate_prefers_qr_like_image_over_logo() -> None:
    logo_bytes = png_bytes(
        (320, 180),
        lambda draw: draw.rectangle((70, 50, 250, 120), fill=0),
    )

    def draw_qr(draw):
        for y in range(9):
            for x in range(9):
                if (x + y) % 2 == 0:
                    draw.rectangle((x * 20, y * 20, x * 20 + 12, y * 20 + 12), fill=0)
        draw.rectangle((8, 8, 52, 52), outline=0, width=8)
        draw.rectangle((128, 8, 172, 52), outline=0, width=8)
        draw.rectangle((8, 128, 52, 172), outline=0, width=8)

    qr_bytes = png_bytes((180, 180), draw_qr)

    class FakeCandidate:
        def __init__(self, image_bytes: bytes, width: int, height: int) -> None:
            self.image_bytes = image_bytes
            self.width = width
            self.height = height

        def bounding_box(self):
            return {"width": self.width, "height": self.height}

        def screenshot(self, **kwargs):
            return self.image_bytes

    class FakeLocator:
        def __init__(self, candidates):
            self.candidates = candidates

        def count(self):
            return len(self.candidates)

        def nth(self, index):
            return self.candidates[index]

    class FakePage:
        frames = []

        def locator(self, selector):
            if selector == "img":
                return FakeLocator(
                    [
                        FakeCandidate(logo_bytes, 320, 180),
                        FakeCandidate(qr_bytes, 180, 180),
                    ]
                )
            return FakeLocator([])

        def screenshot(self, **kwargs):
            return logo_bytes

    assert login.screenshot_login_candidate(FakePage()) == qr_bytes


def test_screenshot_login_candidate_uses_dom_candidate_first() -> None:
    qr_bytes = png_bytes(
        (120, 120),
        lambda draw: draw.rectangle((10, 10, 110, 110), outline=0, width=8),
    )
    fallback_bytes = png_bytes(
        (120, 120),
        lambda draw: draw.rectangle((20, 20, 100, 100), fill=0),
    )

    class FakeCandidate:
        def screenshot(self, **kwargs):
            return qr_bytes

    class FakeLocator:
        def screenshot(self, **kwargs):
            return qr_bytes

        def count(self):
            return 0

    class FakePage:
        frames = []

        def evaluate(self, script):
            assert "ptqrshow" in script
            return [{"selector": "#login-qrcode", "score": 100}]

        def locator(self, selector):
            assert selector == "#login-qrcode"
            return FakeLocator()

        def screenshot(self, **kwargs):
            return fallback_bytes

    assert login.screenshot_login_candidate(FakePage()) == qr_bytes


def test_screenshot_login_candidate_reveals_login_challenge_before_dom_retry() -> None:
    qr_bytes = png_bytes(
        (120, 120),
        lambda draw: draw.rectangle((10, 10, 110, 110), outline=0, width=8),
    )
    fallback_bytes = png_bytes(
        (120, 120),
        lambda draw: draw.rectangle((20, 20, 100, 100), fill=0),
    )

    class FakeLoginButton:
        def __init__(self, page) -> None:
            self.page = page

        def bounding_box(self):
            return {"width": 96, "height": 36}

        def click(self, **kwargs):
            self.page.clicked = True

    class EmptyLocator:
        def count(self):
            return 0

    class ButtonLocator:
        def __init__(self, page) -> None:
            self.page = page

        def count(self):
            return 1

        def nth(self, index):
            return FakeLoginButton(self.page)

    class QrLocator:
        def screenshot(self, **kwargs):
            return qr_bytes

    class FakePage:
        frames = []

        def __init__(self) -> None:
            self.clicked = False

        def evaluate(self, script):
            if self.clicked:
                return [{"selector": "#login-qrcode", "score": 100}]
            return []

        def locator(self, selector):
            if selector == "#login-qrcode":
                return QrLocator()
            if selector == 'button:has-text("登录")':
                return ButtonLocator(self)
            return EmptyLocator()

        def wait_for_timeout(self, timeout):
            pass

        def screenshot(self, **kwargs):
            return fallback_bytes

    page = FakePage()

    assert login.screenshot_login_candidate(page) == qr_bytes
    assert page.clicked is True


def test_screenshot_dom_login_candidate_prefers_best_candidate_across_frames() -> None:
    weak_bytes = png_bytes(
        (120, 120),
        lambda draw: draw.rectangle((10, 10, 110, 110), fill=0),
    )
    strong_bytes = png_bytes(
        (120, 120),
        lambda draw: draw.rectangle((10, 10, 110, 110), outline=0, width=8),
    )

    class FakeLocator:
        def __init__(self, image_bytes: bytes) -> None:
            self.image_bytes = image_bytes

        def screenshot(self, **kwargs):
            return self.image_bytes

    class FakeFrame:
        def __init__(self, selector: str, score: int, image_bytes: bytes) -> None:
            self.selector = selector
            self.score = score
            self.image_bytes = image_bytes

        def evaluate(self, script):
            return [{"selector": self.selector, "score": self.score}]

        def locator(self, selector):
            assert selector == self.selector
            return FakeLocator(self.image_bytes)

    page = FakeFrame("#weak", 55, weak_bytes)
    iframe = FakeFrame("#strong", 105, strong_bytes)
    page.frames = [iframe]

    assert login.screenshot_dom_login_candidate(page) == strong_bytes
