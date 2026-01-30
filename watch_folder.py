import os
import time
import requests
from datetime import datetime
from watchdog.events import FileSystemEventHandler

WATCH_FOLDER = "/home/node/.n8n-files"
WEBHOOK_URL = "http://n8n:5678/webhook-test/981184cc-f50d-4a42-bd24-4b8e0943f53e"
FILE_EXTENSIONS = ['.pdf', '.jpg', '.jpeg', '.png', '.tiff', '.txt']
DEBOUNCE_SECONDS = 2

os.makedirs(WATCH_FOLDER, exist_ok=True)


class ScanSnapHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}

    def on_created(self, event):
        self._handle_event(event)

    def on_modified(self, event):
        self._handle_event(event)

    def send_request(self, filename, filepath, filesize):
        payload = {
            "filename": filename,
            "filepath": filepath,
            "filesize": filesize,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }

        try:
            response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
            print("  ✓ Webhook triggered" if response.ok else f"  ✗ Webhook error {response.status_code}")
        except requests.RequestException as e:
            print(f"  ✗ Webhook failed: {e}")

    def _handle_event(self, event):
        if event.is_directory:
            return

        filepath = event.src_path
        filename = os.path.basename(filepath)
        _, ext = os.path.splitext(filename)

        if ext.lower() not in FILE_EXTENSIONS:
            return

        now = time.time()
        if filepath in self.last_triggered:
            if now - self.last_triggered[filepath] < DEBOUNCE_SECONDS:
                return

        self.last_triggered[filepath] = now
        time.sleep(1)

        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] New file detected: {filename}")

        try:
            filesize = os.path.getsize(filepath)
        except OSError:
            filesize = 0

        self.send_request(filename, filepath, filesize)

        print()
