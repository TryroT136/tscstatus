import json
import os
import platform
import subprocess
import sys
import threading
import time
import tkinter as tk
from PIL import Image, ImageTk

# --- OS DETECTION & WINDOWS DPI SETUP ---
IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    from PIL import ImageGrab
    try:
        import ctypes
        # Set Per-Monitor DPI awareness on Windows to prevent scaling mismatch
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# --- OpenRGB CLIENT SETUP ---
try:
    from openrgb import OpenRGBClient
    from openrgb.utils import RGBColor
    HAS_OPENRGB = True
except ImportError:
    HAS_OPENRGB = False
    print("[!] openrgb-python not found. Run: pip install openrgb-python")

# --- CONFIG STORAGE ---
CONFIG_FILE = "scanner_regions_config.json"
TEMP_SCREENSHOT = "/tmp/scanner_full_screen.png"

# --- COLOR DEFINITIONS (EXACT HEX CODES TO RGB) ---
COLOR_PROFILES = {
    "green":   {"rgb": (0, 255, 0),     "tolerance": 45},   # #00ff00
    "blue":    {"rgb": (0, 0, 255),     "tolerance": 45},   # #0000ff
    "red":     {"rgb": (255, 0, 0),     "tolerance": 45},   # #ff0000
    "gamma":   {"rgb": (255, 84, 0),    "tolerance": 35},   # #ff5400
    "epsilon": {"rgb": (85, 0, 127),    "tolerance": 35},   # #55007f
    "delta":   {"rgb": (255, 255, 0),   "tolerance": 45},   # #ffff00
    "amber":   {"rgb": (90, 90, 90),    "tolerance": 20},   # #5a5a5a
    "omega":   {"rgb": (255, 255, 255), "tolerance": 20}    # #ffffff
}

CODES = list(COLOR_PROFILES.keys())
regions = {}  # {'code_name': [left, top, width, height]}
running = False


class OpenRGBManager:
    """Manages the connection, state persistence, and smooth color fades."""
    def __init__(self, host="127.0.0.1", port=6742):
        self.client = None
        self.host = host
        self.port = port
        self.current_rgb = (0, 0, 0)
        self.target_rgb = (0, 0, 0)
        self.fade_thread = None
        self.stop_fade_event = threading.Event()
        self.lock = threading.Lock()
        self.connect()

    def connect(self):
        if not HAS_OPENRGB:
            return
        try:
            self.client = OpenRGBClient(self.host, self.port, name="ColorScanner")
            print("[+] Connected to OpenRGB SDK server.")
        except Exception as e:
            self.client = None
            print(f"[!] Could not connect to OpenRGB ({e}). Will retry on demand.")

    def _apply_hardware_color(self, r, g, b):
        if not HAS_OPENRGB:
            return
        if not self.client:
            self.connect()
            if not self.client:
                return
        try:
            target = RGBColor(int(r), int(g), int(b))
            for device in self.client.devices:
                device.set_color(target)
            self.current_rgb = (int(r), int(g), int(b))
        except Exception as e:
            print(f"[!] OpenRGB write error: {e}")
            self.client = None

    def transition_to(self, target_r, target_g, target_b, duration=1.0):
        """Fades from current RGB to target RGB over `duration` seconds, then holds."""
        new_target = (target_r, target_g, target_b)
        if self.target_rgb == new_target and self.current_rgb == new_target:
            return

        with self.lock:
            self.target_rgb = new_target
            self.stop_fade_event.set()

        # Start fade in a separate thread so screen scanning is never delayed
        self.fade_thread = threading.Thread(
            target=self._fade_worker,
            args=(new_target, duration),
            daemon=True
        )
        self.fade_thread.start()

    def _fade_worker(self, target, duration):
        self.stop_fade_event.clear()
        start_r, start_g, start_b = self.current_rgb
        dest_r, dest_g, dest_b = target

        if duration <= 0.05:
            self._apply_hardware_color(dest_r, dest_g, dest_b)
            return

        fps = 40
        steps = max(1, int(duration * fps))
        step_delay = duration / steps

        for step in range(1, steps + 1):
            if self.stop_fade_event.is_set():
                return
            t = step / steps
            curr_r = int(start_r + (dest_r - start_r) * t)
            curr_g = int(start_g + (dest_g - start_g) * t)
            curr_b = int(start_b + (dest_b - start_b) * t)
            self._apply_hardware_color(curr_r, curr_g, curr_b)
            time.sleep(step_delay)

        # Ensure exact final destination color is locked
        self._apply_hardware_color(dest_r, dest_g, dest_b)


rgb_manager = OpenRGBManager()


def load_regions():
    global regions
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                regions = json.load(f)
        except Exception:
            pass


def save_regions():
    with open(CONFIG_FILE, "w") as f:
        json.dump(regions, f, indent=4)


def take_fullscreen_capture():
    """Cross-platform capture: ImageGrab on Windows, Spectacle on Linux."""
    if IS_WINDOWS:
        try:
            return ImageGrab.grab(all_screens=True).convert("RGB")
        except Exception:
            return ImageGrab.grab().convert("RGB")

    # Linux (Wayland Spectacle)
    try:
        subprocess.run(
            ["spectacle", "-b", "-n", "-o", TEMP_SCREENSHOT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3
        )
        if os.path.exists(TEMP_SCREENSHOT):
            img = Image.open(TEMP_SCREENSHOT).convert("RGB")
            os.remove(TEMP_SCREENSHOT)
            return img
    except Exception as e:
        print(f"[!] Linux capture error: {e}")
    return None


def check_color_match(cropped_img, code):
    """Calculates matching pixel percentage using tobytes() to avoid Pillow deprecation."""
    target_r, target_g, target_b = COLOR_PROFILES[code]["rgb"]
    tol = COLOR_PROFILES[code]["tolerance"]

    raw = cropped_img.tobytes()
    total_pixels = len(raw) // 3
    if total_pixels == 0:
        return 0.0

    matching = 0
    # Process RGB triples directly from raw bytes
    for i in range(0, len(raw), 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        if abs(r - target_r) <= tol and abs(g - target_g) <= tol and abs(b - target_b) <= tol:
            matching += 1

    return (matching / total_pixels) * 100.0


class RegionSelector:
    """Snapshot screen overlay with HiDPI and multi-monitor scaling adjustments."""
    def __init__(self, code_name, callback):
        self.code_name = code_name
        self.callback = callback

        self.raw_screenshot = take_fullscreen_capture()

        self.root = tk.Toplevel()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()

        win_w = self.root.winfo_screenwidth()
        win_h = self.root.winfo_screenheight()

        self.canvas = tk.Canvas(self.root, cursor="cross", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        if self.raw_screenshot:
            raw_w, raw_h = self.raw_screenshot.size
            self.scale_x = raw_w / win_w
            self.scale_y = raw_h / win_h

            fitted = self.raw_screenshot.resize((win_w, win_h), Image.Resampling.BILINEAR)
            self.tk_bg = ImageTk.PhotoImage(fitted)
            self.canvas.create_image(0, 0, anchor="nw", image=self.tk_bg)
        else:
            self.scale_x = 1.0
            self.scale_y = 1.0
            self.canvas.configure(bg="#222222")

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        self.canvas.create_text(
            win_w // 2, 40,
            text=f"Drag a box for: {code_name.upper()} (ESC to cancel)",
            fill="#00ffff",
            font=("Arial", 16, "bold")
        )

        self.start_x = 0
        self.start_y = 0
        self.rect = None
        self.root.wait_window()

    def on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline="#00ff00", width=2
        )

    def on_drag(self, event):
        if self.rect:
            self.canvas.coords(self.rect, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        left = min(self.start_x, event.x)
        top = min(self.start_y, event.y)
        w = abs(self.start_x - event.x)
        h = abs(self.start_y - event.y)

        phys_left = int(left * self.scale_x)
        phys_top = int(top * self.scale_y)
        phys_w = int(w * self.scale_x)
        phys_h = int(h * self.scale_y)

        if phys_w > 5 and phys_h > 5:
            self.callback(self.code_name, [phys_left, phys_top, phys_w, phys_h])

        self.root.destroy()


class ControlPanel:
    def __init__(self):
        self.root = tk.Tk()
        os_label = "Windows" if IS_WINDOWS else "Wayland / Linux"
        self.root.title(f"Screen Color Scanner ({os_label})")
        self.root.geometry("680x750")

        tk.Label(
            self.root,
            text=f"RGB Screen Monitor ({os_label})",
            font=("Arial", 14, "bold")
        ).pack(pady=8)

        # --- Transition Fade Time Slider ---
        slider_frame = tk.Frame(self.root, bd=1, relief="groove", padx=10, pady=5)
        slider_frame.pack(fill="x", padx=10, pady=5)

        tk.Label(slider_frame, text="Color Fade Time (seconds):", font=("Arial", 10, "bold")).pack(side="left")
        self.fade_slider = tk.Scale(
            slider_frame, from_=0.0, to=5.0, resolution=0.1,
            orient="horizontal", length=250
        )
        self.fade_slider.set(1.0)
        self.fade_slider.pack(side="right", padx=10)

        # --- Detection Rows ---
        self.main_frame = tk.Frame(self.root)
        self.main_frame.pack(fill="both", expand=True, padx=10, pady=5)

        self.canvases = {}
        self.labels = {}
        self.imgs = {}

        load_regions()

        for code in CODES:
            row = tk.Frame(self.main_frame, bd=1, relief="groove", pady=3)
            row.pack(fill="x", pady=2)

            # Code title
            tk.Label(
                row, text=f"{code.upper()}:",
                font=("Arial", 10, "bold"), width=9, anchor="w"
            ).pack(side="left", padx=5)

            # Status label
            mapped = code in regions
            txt = "Mapped" if mapped else "Not Set"
            clr = "green" if mapped else "orange"
            self.labels[code] = tk.Label(row, text=txt, fg=clr, width=17, anchor="w")
            self.labels[code].pack(side="left")

            # Mini preview canvas
            self.canvases[code] = tk.Canvas(row, width=80, height=35, bg="black")
            self.canvases[code].pack(side="left", padx=8)

            # Manual override trigger button
            r, g, b = COLOR_PROFILES[code]["rgb"]
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            fg_color = "black" if (code in ["omega", "delta", "green"]) else "white"
            tk.Button(
                row, text="Manual Trigger", bg=hex_color, fg=fg_color,
                font=("Arial", 9, "bold"),
                command=lambda c=code: self.manual_trigger(c)
            ).pack(side="right", padx=4)

            # Region setup button
            tk.Button(
                row, text="Set Region",
                command=lambda c=code: self.select_box(c)
            ).pack(side="right", padx=4)

        self.btn = tk.Button(
            self.root, text="START SCANNING",
            bg="#008800", fg="white", font=("Arial", 12, "bold"),
            command=self.toggle
        )
        self.btn.pack(pady=12)

    def manual_trigger(self, code):
        """Immediately transitions OpenRGB to the chosen color profile and leaves it active."""
        r, g, b = COLOR_PROFILES[code]["rgb"]
        duration = self.fade_slider.get()
        print(f"[*] Manual override to '{code.upper()}' (RGB: {r},{g},{b}) over {duration}s")
        rgb_manager.transition_to(r, g, b, duration=duration)
        self.labels[code].config(text=f"ACTIVE (Manual)", fg="blue")

    def select_box(self, code):
        was_running = running
        if was_running:
            self.toggle()

        self.root.withdraw()
        time.sleep(0.35)

        RegionSelector(code, self.save_box)

        self.root.deiconify()
        if was_running:
            self.toggle()

    def save_box(self, code, coords):
        regions[code] = coords
        self.labels[code].config(text="Mapped", fg="green")
        save_regions()

    def update_tile(self, code, img_slice, percent):
        res = img_slice.resize((80, 35))
        img_tk = ImageTk.PhotoImage(res)
        self.canvases[code].create_image(0, 0, anchor="nw", image=img_tk)
        self.imgs[code] = img_tk

        if percent > 25.0:
            self.labels[code].config(text=f"MATCH ({percent:.0f}%)", fg="blue")
        else:
            self.labels[code].config(text=f"Scanning ({percent:.0f}%)", fg="green")

    def toggle(self):
        global running
        if not running:
            running = True
            self.btn.config(text="STOP SCANNING", bg="red")
            threading.Thread(target=self.loop, daemon=True).start()
        else:
            running = False
            self.btn.config(text="START SCANNING", bg="#008800")

    def loop(self):
        while running:
            if not regions:
                time.sleep(0.5)
                continue

            screen = take_fullscreen_capture()
            if screen is None:
                time.sleep(0.5)
                continue

            scr_w, scr_h = screen.size
            active_match = None
            max_percent = 0.0

            for code, box in list(regions.items()):
                left, top, w, h = box
                right, bottom = min(left + w, scr_w), min(top + h, scr_h)

                if right <= left or bottom <= top:
                    continue

                cropped = screen.crop((left, top, right, bottom))
                percent = check_color_match(cropped, code)

                if percent > 25.0 and percent > max_percent:
                    max_percent = percent
                    active_match = code

                self.root.after(0, self.update_tile, code, cropped, percent)

            # If a rule matched, initiate a smooth fade to that color (and keep it held)
            if active_match:
                r, g, b = COLOR_PROFILES[active_match]["rgb"]
                duration = self.fade_slider.get()
                rgb_manager.transition_to(r, g, b, duration=duration)

            time.sleep(0.1)


if __name__ == "__main__":
    app = ControlPanel()
    app.root.mainloop()
