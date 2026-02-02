import time
from watchdog.observers.polling import PollingObserver as Observer
from watch_folder import (
    WATCH_FOLDER,
    WEBHOOK_URL,
    FILE_EXTENSIONS,
    ScanSnapHandler,
)

def main():
    print("=" * 50)
    print("ScanSnap Folder Watcher Started (Python)")
    print("=" * 50)
    print(f"Watching folder: {WATCH_FOLDER}")
    print(f"Webhook URL: {WEBHOOK_URL}")
    print(f"Watching for files: {', '.join(FILE_EXTENSIONS)}")
    print("Press Ctrl+C to stop")
    print("=" * 50)
    print()

    event_handler = ScanSnapHandler()
    
    # Increase timeout for more aggressive polling
    observer = Observer(timeout=5)  # Poll every 5 seconds
    observer.schedule(event_handler, WATCH_FOLDER, recursive=False)
    observer.start()

    print(f"✓ Observer started, polling every 5 seconds")
    print(f"✓ Checking if folder exists: {WATCH_FOLDER}")
    
    import os
    if os.path.exists(WATCH_FOLDER):
        print(f"✓ Folder accessible")
        # List current files
        files = os.listdir(WATCH_FOLDER)
        print(f"✓ Current files in folder: {len(files)}")
        for f in files[:5]:  # Show first 5
            print(f"  - {f}")
    else:
        print(f"✗ ERROR: Folder not accessible!")
    
    print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()