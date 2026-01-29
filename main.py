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
    observer = Observer(timeout=1)
    observer.schedule(event_handler, WATCH_FOLDER, recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()


if __name__ == "__main__":
    main()
