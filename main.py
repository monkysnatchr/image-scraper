import base64
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile


DEBUG_PORT = 9222
PAGE_WAIT = 4
SCROLL_ROUNDS = 25
SCROLL_DELAY = 0.4

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ZIP_PATH = os.path.join(SCRIPT_DIR, "website_images.zip")


def find_browser():
    system = platform.system()

    path_candidates = [
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "microsoft-edge",
        "microsoft-edge-stable",
        "msedge",
    ]

    for name in path_candidates:
        path = shutil.which(name)
        if path:
            return path

    if system == "Linux":
        candidates = [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            os.path.expanduser("~/.local/bin/chromium"),
            os.path.expanduser("~/.local/bin/google-chrome"),
            os.path.expanduser(
                "~/.local/share/flatpak/exports/bin/"
                "org.chromium.Chromium"
            ),
            "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
            os.path.expanduser(
                "~/.local/share/flatpak/exports/bin/"
                "com.google.Chrome"
            ),
        ]

    elif system == "Windows":
        candidates = [
            os.path.expandvars(
                r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"
            ),
            os.path.expandvars(
                r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"
            ),
            os.path.expandvars(
                r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
            ),
            os.path.expandvars(
                r"%PROGRAMFILES%\Chromium\Application\chrome.exe"
            ),
            os.path.expandvars(
                r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"
            ),
            os.path.expandvars(
                r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"
            ),
            os.path.expandvars(
                r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"
            ),
        ]

    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/"
            "Contents/MacOS/Google Chrome",
            os.path.expanduser(
                "~/Applications/Google Chrome.app/"
                "Contents/MacOS/Google Chrome"
            ),
            "/Applications/Chromium.app/"
            "Contents/MacOS/Chromium",
            os.path.expanduser(
                "~/Applications/Chromium.app/"
                "Contents/MacOS/Chromium"
            ),
            "/Applications/Microsoft Edge.app/"
            "Contents/MacOS/Microsoft Edge",
            os.path.expanduser(
                "~/Applications/Microsoft Edge.app/"
                "Contents/MacOS/Microsoft Edge"
            ),
        ]

    else:
        candidates = []

    for path in candidates:
        if os.path.isfile(path):
            return path

    return None


def port_open(port=DEBUG_PORT):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)

    try:
        sock.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        sock.close()


def start_browser(browser):
    profile = tempfile.mkdtemp(
        prefix="image_scraper_chrome_"
    )

    command = [
        browser,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={profile}",
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-features=Translate",
        "--window-size=1920,1080",
        "about:blank",
    ]

    print("\n[+] Starting browser:")
    print("    " + browser)

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(40):
        if port_open():
            return process, profile

        time.sleep(0.25)

    try:
        process.terminate()
    except Exception:
        pass

    shutil.rmtree(profile, ignore_errors=True)

    raise RuntimeError(
        "Chromium started but the DevTools port did not open."
    )


def get_tabs():
    url = f"http://127.0.0.1:{DEBUG_PORT}/json"

    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read())


class SimpleWebSocket:
    def __init__(self, url):
        self.sock = None

        parsed = urllib.parse.urlparse(url)

        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"

        if parsed.query:
            self.path += "?" + parsed.query

        self.connect()

    def connect(self):
        self.sock = socket.create_connection(
            (self.host, self.port),
            timeout=30
        )

        self.sock.settimeout(30)

        key = base64.b64encode(
            os.urandom(16)
        ).decode()

        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )

        self.sock.sendall(request.encode())

        response = b""

        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)

            if not chunk:
                raise RuntimeError(
                    "WebSocket handshake failed."
                )

            response += chunk

            if len(response) > 65536:
                raise RuntimeError(
                    "WebSocket handshake response too large."
                )

        header = response.split(
            b"\r\n\r\n",
            1
        )[0].decode(
            "latin1",
            errors="ignore"
        )

        if "101" not in header.splitlines()[0]:
            raise RuntimeError(
                "Browser rejected WebSocket connection:\n"
                + header
            )

    def send(self, message):
        if isinstance(message, str):
            data = message.encode()
        else:
            data = message

        mask = os.urandom(4)

        masked = bytes(
            byte ^ mask[i % 4]
            for i, byte in enumerate(data)
        )

        length = len(masked)
        first_byte = 0x81

        if length < 126:
            header = struct.pack(
                "!BB",
                first_byte,
                0x80 | length
            )
        elif length < 65536:
            header = struct.pack(
                "!BBH",
                first_byte,
                0x80 | 126,
                length
            )
        else:
            header = struct.pack(
                "!BBQ",
                first_byte,
                0x80 | 127,
                length
            )

        self.sock.sendall(
            header + mask + masked
        )

    def recv_frame(self):
        first_two = self.recv_exact(2)

        first = first_two[0]
        second = first_two[1]

        opcode = first & 0x0F
        masked = bool(second & 0x80)

        length = second & 0x7F

        if length == 126:
            length = struct.unpack(
                "!H",
                self.recv_exact(2)
            )[0]

        elif length == 127:
            length = struct.unpack(
                "!Q",
                self.recv_exact(8)
            )[0]

        if masked:
            mask = self.recv_exact(4)
        else:
            mask = None

        payload = self.recv_exact(length)

        if mask:
            payload = bytes(
                byte ^ mask[i % 4]
                for i, byte in enumerate(payload)
            )

        return opcode, payload

    def recv_exact(self, amount):
        data = b""

        while len(data) < amount:
            chunk = self.sock.recv(
                amount - len(data)
            )

            if not chunk:
                raise RuntimeError(
                    "WebSocket connection closed."
                )

            data += chunk

        return data

    def recv(self):
        fragments = []

        while True:
            opcode, payload = self.recv_frame()

            if opcode == 0x8:
                return None

            if opcode == 0x9:
                self.send_pong(payload)
                continue

            if opcode == 0xA:
                continue

            if opcode == 0x1:
                return payload.decode(
                    "utf-8",
                    errors="replace"
                )

            if opcode == 0x2:
                return payload

            if opcode == 0x0:
                fragments.append(payload)

                if fragments:
                    return b"".join(
                        fragments
                    ).decode(
                        "utf-8",
                        errors="replace"
                    )

    def send_pong(self, payload):
        length = len(payload)
        mask = os.urandom(4)

        masked = bytes(
            byte ^ mask[i % 4]
            for i, byte in enumerate(payload)
        )

        if length < 126:
            header = struct.pack(
                "!BB",
                0x8A,
                0x80 | length
            )
        elif length < 65536:
            header = struct.pack(
                "!BBH",
                0x8A,
                0x80 | 126,
                length
            )
        else:
            header = struct.pack(
                "!BBQ",
                0x8A,
                0x80 | 127,
                length
            )

        self.sock.sendall(
            header + mask + masked
        )


class CDP:
    def __init__(self, websocket_url):
        self.ws = SimpleWebSocket(
            websocket_url
        )
        self.message_id = 0

    def command(
        self,
        method,
        params=None,
        timeout=30
    ):
        self.message_id += 1

        current_id = self.message_id

        message = {
            "id": current_id,
            "method": method,
            "params": params or {}
        }

        self.ws.send(
            json.dumps(message)
        )

        start = time.time()

        while True:
            if time.time() - start > timeout:
                raise TimeoutError(
                    "CDP command timed out: "
                    + method
                )

            raw = self.ws.recv()

            if raw is None:
                raise RuntimeError(
                    "Browser WebSocket closed."
                )

            if isinstance(raw, bytes):
                raw = raw.decode(
                    "utf-8",
                    errors="ignore"
                )

            try:
                result = json.loads(raw)
            except Exception:
                continue

            if result.get("id") == current_id:
                return result


def get_page_websocket():
    time.sleep(0.5)

    tabs = get_tabs()

    for tab in tabs:
        if tab.get("type") == "page":
            websocket_url = tab.get(
                "webSocketDebuggerUrl"
            )

            if websocket_url:
                return websocket_url

    raise RuntimeError(
        "No browser page was found."
    )


IMAGE_SCRIPT = r"""
(() => {
    const found = new Set();

    function add(value) {
        if (!value)
            return;

        value = value.trim();

        if (!value)
            return;

        if (
            value.startsWith("data:") ||
            value.startsWith("blob:") ||
            value.startsWith("javascript:")
        )
            return;

        try {
            const absolute = new URL(
                value,
                location.href
            ).href;

            if (
                absolute.startsWith("http://") ||
                absolute.startsWith("https://")
            ) {
                found.add(absolute);
            }
        } catch (_) {}
    }

    function srcset(value) {
        if (!value)
            return;

        value.split(",").forEach(item => {
            const parts =
                item.trim().split(/\s+/);

            if (parts[0])
                add(parts[0]);
        });
    }

    document.querySelectorAll("img").forEach(img => {
        [
            "src",
            "data-src",
            "data-original",
            "data-lazy-src",
            "data-url",
            "data-image",
            "data-image-url",
            "data-fallback-src"
        ].forEach(attribute => {
            add(img.getAttribute(attribute));
        });

        srcset(img.getAttribute("srcset"));
        srcset(img.getAttribute("data-srcset"));
        srcset(
            img.getAttribute(
                "data-lazy-srcset"
            )
        );
    });

    document.querySelectorAll("source").forEach(source => {
        add(source.getAttribute("src"));

        srcset(
            source.getAttribute("srcset")
        );

        srcset(
            source.getAttribute("data-srcset")
        );
    });

    document.querySelectorAll("*").forEach(element => {
        const style =
            getComputedStyle(element);

        const background =
            style.backgroundImage;

        if (
            background &&
            background !== "none"
        ) {
            const matches =
                background.matchAll(
                    /url\(["']?([^"')]+)["']?\)/g
                );

            for (const match of matches) {
                add(match[1]);
            }
        }

        [
            "data-background-image",
            "data-bg",
            "data-background"
        ].forEach(attribute => {
            add(
                element.getAttribute(attribute)
            );
        });
    });

    return Array.from(found);
})()
"""


def scrape_page(cdp, url):
    print()
    print("[+] Opening:")
    print("    " + url)

    cdp.command("Page.enable")
    cdp.command("Runtime.enable")

    cdp.command(
        "Page.navigate",
        {"url": url}
    )

    print(
        "[+] Waiting for JavaScript..."
    )

    time.sleep(PAGE_WAIT)

    print(
        "[+] Triggering lazy loading..."
    )

    for _ in range(SCROLL_ROUNDS):
        cdp.command(
            "Runtime.evaluate",
            {
                "expression":
                    """
                    window.scrollTo(
                        0,
                        document.body.scrollHeight
                    );
                    """
            }
        )

        time.sleep(
            SCROLL_DELAY
        )

    time.sleep(1)

    cdp.command(
        "Runtime.evaluate",
        {
            "expression":
                "window.scrollTo(0, 0);"
        }
    )

    result = cdp.command(
        "Runtime.evaluate",
        {
            "expression": IMAGE_SCRIPT,
            "returnByValue": True
        }
    )

    try:
        images = (
            result
            ["result"]
            ["result"]
            ["value"]
        )

        if isinstance(images, list):
            return images

    except Exception:
        pass

    return []


def filename_from_url(url, number):
    parsed = urllib.parse.urlparse(url)

    filename = os.path.basename(
        parsed.path
    )

    if not filename:
        filename = f"image_{number}.jpg"

    filename = filename.split("?")[0]
    filename = filename.split("#")[0]

    filename = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        filename
    )

    filename = filename[:180]

    if not filename:
        filename = f"image_{number}.jpg"

    return filename


def download_image(
    url,
    number,
    referer=None
):
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "image/avif,image/webp,"
                "image/apng,image/svg+xml,"
                "image/*,*/*;q=0.8"
            ),
        }

        if referer:
            headers["Referer"] = referer

        request = urllib.request.Request(
            url,
            headers=headers
        )

        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            content_type = response.headers.get(
                "Content-Type",
                ""
            )

            data = response.read()

        if not content_type.lower().startswith(
            "image/"
        ):
            return None

        filename = filename_from_url(
            url,
            number
        )

        return filename, data

    except Exception as error:
        print(
            "    [FAILED]",
            str(error)
        )

        return None


def create_zip(
    image_urls,
    referer
):
    image_urls = list(
        dict.fromkeys(image_urls)
    )

    print()
    print(
        "[+] Found",
        len(image_urls),
        "unique image URLs."
    )

    if not image_urls:
        print(
            "[!] No images found."
        )
        return

    used_names = set()
    downloaded = 0

    print()
    print("[+] Creating ZIP:")
    print("    " + ZIP_PATH)

    with zipfile.ZipFile(
        ZIP_PATH,
        "w",
        compression=zipfile.ZIP_DEFLATED
    ) as archive:

        for index, url in enumerate(
            image_urls,
            1
        ):
            print(
                f"[{index}/{len(image_urls)}]"
            )
            print(
                "    " + url
            )

            result = download_image(
                url,
                index,
                referer
            )

            if not result:
                continue

            filename, data = result

            original = filename
            counter = 2

            while filename in used_names:
                name, extension = (
                    os.path.splitext(
                        original
                    )
                )

                filename = (
                    f"{name}_{counter}"
                    f"{extension}"
                )

                counter += 1

            used_names.add(filename)

            archive.writestr(
                filename,
                data
            )

            downloaded += 1

            print(
                "    -> " + filename
            )

    print()
    print("=" * 60)
    print("DONE")
    print("=" * 60)
    print(
        "Downloaded:",
        downloaded,
        "images"
    )
    print(
        "ZIP:",
        ZIP_PATH
    )


def main():
    print("=" * 60)
    print("IMAGE SCRAPER")
    print("Made by monkysnatchr on GitHub")
    print("=" * 60)

    url = input(
        "\nEnter page URL: "
    ).strip()

    markdown_match = re.match(
        r"^\[.*?\]\((https?://.*?)\)$",
        url
    )

    if markdown_match:
        url = markdown_match.group(1)

    if not url:
        print(
            "[ERROR] No URL entered."
        )
        return

    if not url.startswith(
        ("http://", "https://")
    ):
        url = "https://" + url

    browser = find_browser()

    if not browser:
        print()
        print(
            "[ERROR] No Chromium-based browser found."
        )
        print()
        print(
            "Looked for Chromium, Chrome and Edge."
        )
        return

    print()
    print("[+] Browser found:")
    print("    " + browser)

    browser_process = None
    profile = None
    cdp = None

    try:
        browser_process, profile = (
            start_browser(browser)
        )

        websocket_url = (
            get_page_websocket()
        )

        print(
            "[+] Connecting to browser..."
        )

        cdp = CDP(
            websocket_url
        )

        images = scrape_page(
            cdp,
            url
        )

        create_zip(
            images,
            url
        )

    except KeyboardInterrupt:
        print()
        print("[!] Stopped.")

    except Exception as error:
        print()
        print(
            "[ERROR]",
            str(error)
        )

    finally:
        if cdp:
            try:
                cdp.ws.close()
            except Exception:
                pass

        if browser_process:
            try:
                browser_process.terminate()
            except Exception:
                pass

        if profile:
            shutil.rmtree(
                profile,
                ignore_errors=True
            )


if __name__ == "__main__":
    main()
