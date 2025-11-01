import asyncio
import io
import os
import re
import sys
from datetime import datetime
from pathlib import Path
import inspect
import traceback


try:
    import asyncvnc2 as av  
    LIB_NAME = "asyncvnc2"
except Exception:
    try:
        import asyncvnc as av  
        LIB_NAME = "asyncvnc"
    except Exception:
        av = None  
        LIB_NAME = None

from PIL import Image

try:
    import numpy as np  
    HAVE_NUMPY = True
except Exception:
    np = None
    HAVE_NUMPY = False


OUTPUT_DIR = Path("pictures")
OUTPUT_DIR.mkdir(exist_ok=True)

COLORS = {
    'green': '\033[92m',
    'red': '\033[91m',
    'yellow': '\033[93m',
    'blue': '\033[94m',
    'reset': '\033[0m'
}


def cprint(color: str, msg: str):
    print(f"{COLORS.get(color, '')}{msg}{COLORS['reset']}")


def parse_results_file(filename="results.txt"):
    servers = []
    if not os.path.exists(filename):
        cprint('red', f"File {filename} not found.")
        return servers
    with open(filename, "r", encoding="utf-8") as f:
        for ln in f:
            line = ln.strip()
            if not line:
                continue

            if "--[" in line:
                match = re.match(r"^(.+?):(\d+)--\[(.+)\]$", line)
                if match:
                    ip, port, desktop = match.groups()
                    servers.append({'ip': ip, 'port': port, 'password': None, 'desktop_name': desktop})
                    cprint('blue', f"Parsed noauth server: {ip}:{port} desktop:{desktop}")
                    continue

            parts = line.split('-')
            if len(parts) < 3:
                cprint('red', f"Skipping invalid line: {line}")
                continue
            ip_port = parts[0]
            ip_port_parts = ip_port.split(':')
            if len(ip_port_parts) < 2:
                cprint('red', f"Skipping invalid line (no port): {line}")
                continue
            ip, port = ip_port_parts[0], ip_port_parts[1]
            password = parts[1]
            if password in ('null', '--', ''):
                password = None
            desktop = parts[2].strip("[]")
            servers.append({'ip': ip, 'port': port, 'password': password, 'desktop_name': desktop})
            cprint('blue', f"Parsed server: {ip}:{port} pass:{password or 'noauth'} desktop:{desktop}")
    return servers


def make_filename(server):
    ip = server['ip']
    port = server['port']
    password = server['password'] or 'noauth'
    if password != 'noauth' and len(password) > 10:
        password = password[:10]
    desktop = re.sub(r'[<>:"/\\|?*]', '_', server['desktop_name'] or '')[:20] or 'desktop'
    name = f"{ip}_{port}_{password}_{desktop}.png"
    path = OUTPUT_DIR / name
    if path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = OUTPUT_DIR / f"{ip}_{port}_{password}_{desktop}_{ts}.png"
    return path


async def save_image_from_obj(img_obj, client=None, server=None):
    if isinstance(img_obj, Image.Image):
        return img_obj
    if isinstance(img_obj, memoryview):
        img_obj = img_obj.tobytes()
    if isinstance(img_obj, (bytes, bytearray)):
        bio = io.BytesIO(img_obj)
        try:
            im = Image.open(bio)
            im.load()
            return im.convert("RGB")
        except Exception:
            if client is not None and hasattr(client, 'framebuffer'):
                fb = client.framebuffer
                w = getattr(fb, 'width', None)
                h = getattr(fb, 'height', None)
                if w and h:
                    return Image.frombytes("RGB", (int(w), int(h)), bytes(img_obj))
            raise RuntimeError("Received bytes but couldn't decode as image.")
    if isinstance(img_obj, (tuple, list)) and len(img_obj) == 3:
        w, h, raw = img_obj
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        if isinstance(raw, (bytes, bytearray)):
            return Image.frombytes("RGB", (int(w), int(h)), bytes(raw))
    if HAVE_NUMPY and (hasattr(img_obj, 'shape') and isinstance(img_obj, np.ndarray)):
        return Image.fromarray(img_obj)
    raise RuntimeError(f"Unsupported screenshot() return type: {type(img_obj)}")


async def take_screenshot_for(server, retries=1, timeout=12):  
    ip = server['ip']
    port = server['port']
    password = server['password']
    desktop = server['desktop_name']
    cprint('yellow', f"Attempting screenshot for {ip}:{port} ... (lib={LIB_NAME})")

    if av is None:
        cprint('red', "asyncvnc2/asyncvnc not installed.")
        return False

    connect_func = getattr(av, 'connect', None)
    if connect_func is None:
        cprint('red', "Loaded asyncvnc package has no 'connect' function.")
        return False

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            kwargs = {}
            try:
                sig = inspect.signature(connect_func)
                if 'timeout' in sig.parameters:
                    kwargs['timeout'] = timeout
            except Exception:
                pass

            async with connect_func(host=ip, port=int(port), password=password, **kwargs) as client:
                screenshot_attr = getattr(client, 'screenshot', None)
                img_obj = None

                if screenshot_attr is not None:
                    try:
                        if inspect.iscoroutinefunction(screenshot_attr):
                            img_obj = await screenshot_attr()
                        else:
                            maybe = screenshot_attr()
                            if inspect.isawaitable(maybe):
                                img_obj = await maybe
                            else:
                                img_obj = maybe
                    except Exception as e:
                        cprint('yellow', f"debug: screenshot() call raised: {e}")
                        img_obj = None

                if img_obj is not None:
                    try:
                        pil = await save_image_from_obj(img_obj, client=client, server=server)
                        filename = make_filename(server)
                        pil.save(filename, "PNG")
                        cprint('green', f"Saved screenshot: {filename} ✅")
                        return True
                    except Exception as e:
                        last_exc = e
                        cprint('red', f"Attempt {attempt}/{retries} failed for {ip}:{port}: {e}")
                        continue

                if hasattr(client, 'framebuffer'):
                    fb = client.framebuffer
                    raw = getattr(fb, 'raw', None) or getattr(fb, 'pixels', None)
                    if raw and hasattr(fb, 'width') and hasattr(fb, 'height'):
                        if isinstance(raw, memoryview):
                            raw = raw.tobytes()
                        pil = Image.frombytes("RGB", (int(fb.width), int(fb.height)), bytes(raw))
                        filename = make_filename(server)
                        pil.save(filename, "PNG")
                        cprint('green', f"Saved screenshot from framebuffer: {filename} ✅")
                        return True

        except Exception as e:
            last_exc = e
            msg = str(e)
            if 'auth' in msg.lower():
                cprint('red', f"Auth failed for {ip}:{port}")
                return False
            if '0 bytes read' in msg:
                cprint('red', f"Connection dropped for {ip}:{port}: {msg}")
            else:
                cprint('red', f"Attempt {attempt}/{retries} failed for {ip}:{port}: {msg}")

    cprint('red', f"All attempts failed for {ip}:{port}. Last error: {last_exc}")
    return False


async def process_servers(servers, concurrency=3):
    sem = asyncio.Semaphore(concurrency)
    success = 0

    async def worker(srv):
        nonlocal success
        async with sem:
            ok = await take_screenshot_for(srv, retries=1) 
            if ok:
                success += 1
            await asyncio.sleep(0.6)

    tasks = [worker(s) for s in servers]
    await asyncio.gather(*tasks, return_exceptions=False)
    return success


async def main():
    cprint('blue', "VNC Screenshotter started ▶️")
    if av is None:
        cprint('red', "Library asyncvnc2/asyncvnc not found.")
        return

    servers = parse_results_file('results.txt')
    if not servers:
        cprint('red', "No servers found in results.txt")
        return

    cprint('green', f"Found {len(servers)} servers. Starting...")
    suc = await process_servers(servers, concurrency=4)
    cprint('green', f"Done. Success: {suc}/{len(servers)}")
    cprint('blue', f"Screenshots saved to {OUTPUT_DIR}")


if __name__ == "__main__":
    if not os.path.exists('results.txt'):
        print("Error: create results.txt in same folder. Format: IP:PORT-PASS-[DESKTOP NAME]")
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        cprint('yellow', "Interrupted by user. Exiting... ✋")

