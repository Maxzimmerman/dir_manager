import os
import time
import requests
from datetime import datetime
from watchdog.events import FileSystemEventHandler

WATCH_FOLDER = "/home/node/.n8n-files"
WEBHOOK_URL = "http://n8n:5678/webhook-test/981184cc-f50d-4a42-bd24-4b8e0943f53e"
FILE_EXTENSIONS = ['.txt']
DEBOUNCE_SECONDS = 2

os.makedirs(WATCH_FOLDER, exist_ok=True)

class ScanSnapHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}

    def on_created(self, event):
        self._handle_event(event)

    def send_to_n8n_and_rename(self, filename, filepath, filesize):
        """Send file info to n8n, get new name, and rename"""
        payload = {
            "filename": filename,
            "filepath": filepath,
            "filesize": filesize,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        try:
            print(f"  → Sending to n8n...")
            response = requests.post(WEBHOOK_URL, json=payload, timeout=30)
            
            if response.ok:
                result = response.json()
                print(f"  ✓ n8n processed successfully")
                
                # Get new filename from n8n
                new_filename = result.get('newFilename')
                
                if new_filename and new_filename != filename:
                    # Rename the file
                    directory = os.path.dirname(filepath)
                    new_path = os.path.join(directory, new_filename)
                    
                    if os.path.exists(new_path):
                        print(f"  ⚠ Target exists: {new_filename}")
                        return
                    
                    os.rename(filepath, new_path)
                    print(f"  ✓ Renamed: {filename} → {new_filename}")
                else:
                    print(f"  ℹ No rename needed")
            else:
                print(f"  ✗ n8n error {response.status_code}")
                
        except requests.RequestException as e:
            print(f"  ✗ Request failed: {e}")
        except OSError as e:
            print(f"  ✗ Rename failed: {e}")

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
        time.sleep(2)

        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] New file detected: {filename}")

        try:
            filesize = os.path.getsize(filepath)
        except OSError:
            filesize = 0

        self.send_to_n8n_and_rename(filename, filepath, filesize)
        print()