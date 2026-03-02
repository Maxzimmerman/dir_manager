import os
import time
import requests
from datetime import datetime
from watchdog.events import FileSystemEventHandler

WATCH_FOLDER = "/home/node/.n8n-files"
WEBHOOK_URL = "http://n8n:5678/webhook/981184cc-f50d-4a42-bd24-4b8e0943f53e"
WEBHOOK_TEST_URL = "http://n8n-n8n-1:5678/webhook-test/981184cc-f50d-4a42-bd24-4b8e0943f53e"
                
FILE_EXTENSIONS = ['.txt', '.pdf']
DEBOUNCE_SECONDS = 2

os.makedirs(WATCH_FOLDER, exist_ok=True)


# ---------------------------------------------------------------------------
# Extraction — Python's only job is to get text out of files.
# All naming logic lives in n8n.
# ---------------------------------------------------------------------------

def extract_pdf_form_fields(filepath: str) -> dict:
    """Extract AcroForm field values from digitally-filled PDFs."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(filepath)
        fields = reader.get_form_text_fields()
        if fields:
            return {k: v for k, v in fields.items() if v and str(v).strip()}
        return {}
    except Exception as e:
        print(f"  WARNING: Form field extraction failed: {e}")
        return {}


def extract_pdf_text(filepath: str) -> str:
    """Extract embedded text from a non-scanned PDF."""
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            pages = [page.extract_text() for page in pdf.pages]
        return "\n".join(p.strip() for p in pages if p)
    except Exception as e:
        print(f"  WARNING: Text extraction failed: {e}")
        return ""


def extract_pdf_ocr(filepath: str) -> str:
    """
    OCR for scanned PDFs. Requires ENABLE_OCR=true env var.
    Uses pypdfium2 (already installed) to render pages, then tesseract.

    Dockerfile requirements:
        RUN apt-get install -y tesseract-ocr tesseract-ocr-deu
        RUN pip install pytesseract
    """
    if os.environ.get("ENABLE_OCR", "").lower() != "true":
        return ""
    try:
        import pypdfium2 as pdfium
        import pytesseract
        from PIL import ImageFilter, ImageEnhance, ImageOps

        print(f"  Running OCR...")
        doc = pdfium.PdfDocument(filepath)
        texts = []

        for i in range(len(doc)):
            page = doc[i]
            bitmap = page.render(scale=300 / 72)  # 300 DPI
            img = bitmap.to_pil()

            # Preprocess: greyscale + contrast boost improves accuracy on
            # coloured form backgrounds (pink, blue) and low-quality scans
            img = img.convert("L")
            img = ImageOps.autocontrast(img, cutoff=2)
            img = ImageEnhance.Contrast(img).enhance(1.8)
            img = img.filter(ImageFilter.SHARPEN)

            text = pytesseract.image_to_string(img, lang="deu+eng",
                                               config="--psm 6")
            if text.strip():
                texts.append(text.strip())
            print(f"  Page {i + 1}: {len(text)} chars extracted")

        doc.close()
        return "\n".join(texts)

    except ImportError as e:
        print(f"  WARNING: OCR dependency missing: {e}")
        return ""
    except Exception as e:
        print(f"  WARNING: OCR failed: {e}")
        return ""


def build_payload(filename: str, filepath: str, filesize: int) -> dict:
    """
    Extract all available content from a file and bundle it for n8n.
    Tries strategies in order: form fields -> embedded text -> OCR.
    n8n receives everything and decides on the filename.
    """
    _, ext = os.path.splitext(filename)

    payload = {
        "filename": filename,
        "filepath": filepath,
        "filesize": filesize,
        "timestamp": datetime.utcnow().isoformat(),
        "extractionMethod": "none",
        "formFields": {},
        "textContent": "",
    }

    # Plain text files
    if ext.lower() == ".txt":
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                payload["textContent"] = f.read(8000)
            payload["extractionMethod"] = "text"
        except OSError as e:
            print(f"  WARNING: Could not read file: {e}")
        return payload

    # PDF: try each strategy, collect as much as possible
    fields = extract_pdf_form_fields(filepath)
    if fields:
        payload["formFields"] = fields
        payload["extractionMethod"] = "acroform"
        print(f"  Found {len(fields)} form field(s)")

    text = extract_pdf_text(filepath)
    if text:
        payload["textContent"] = text[:8000]
        if payload["extractionMethod"] == "none":
            payload["extractionMethod"] = "text"

    # OCR only if nothing found yet
    if not text and not fields:
        ocr_text = extract_pdf_ocr(filepath)
        if ocr_text:
            payload["textContent"] = ocr_text[:8000]
            payload["extractionMethod"] = "ocr"

            # Print what OCR saw so you can debug naming issues
            print("  --- OCR OUTPUT ------------------------------------------")
            for i, line in enumerate(ocr_text.split("\n")):
                print(f"  {i:03d} | {line}")
            print("  ---------------------------------------------------------")
        else:
            if os.environ.get("ENABLE_OCR", "").lower() != "true":
                print(f"  WARNING: Scanned PDF detected - set ENABLE_OCR=true")
            else:
                print(f"  WARNING: OCR ran but found no text")

    return payload


# ---------------------------------------------------------------------------
# Watchdog handler
# ---------------------------------------------------------------------------

class ScanSnapHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}

    def on_created(self, event):
        self._handle_event(event)

    def send_and_rename(self, filename: str, filepath: str, filesize: int):
        payload = build_payload(filename, filepath, filesize)
        print(f"  Extraction: {payload['extractionMethod']} | "
              f"{len(payload['textContent'])} chars | "
              f"{len(payload['formFields'])} form fields")

        try:
            print(f"  Sending to n8n...")
            response = requests.post(WEBHOOK_TEST_URL, json=payload, timeout=120)

            if response.ok:
                result = response.json()
                print(f"  n8n responded: {result}")

                # Accept both casings in case n8n workflow is updated
                new_filename = result.get("newFileName") or result.get("newFilename")

                if new_filename and new_filename != filename:
                    new_path = os.path.join(os.path.dirname(filepath), new_filename)
                    if os.path.exists(new_path):
                        print(f"  WARNING: Target already exists: {new_filename}")
                        return
                    os.rename(filepath, new_path)
                    print(f"  Renamed: {filename} -> {new_filename}")
                else:
                    print(f"  No rename (n8n returned: {new_filename!r})")
            else:
                print(f"  ERROR: n8n {response.status_code}: {response.text[:300]}")

        except requests.RequestException as e:
            print(f"  ERROR: Request failed: {e}")
        except OSError as e:
            print(f"  ERROR: Rename failed: {e}")

    def _handle_event(self, event):
        if event.is_directory:
            return
        filepath = event.src_path
        filename = os.path.basename(filepath)
        _, ext = os.path.splitext(filename)
        if ext.lower() not in FILE_EXTENSIONS:
            return

        now = time.time()
        if now - self.last_triggered.get(filepath, 0) < DEBOUNCE_SECONDS:
            return
        self.last_triggered[filepath] = now

        time.sleep(2)  # wait for file to finish writing
        print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] New file: {filename}")

        try:
            filesize = os.path.getsize(filepath)
        except OSError:
            filesize = 0

        self.send_and_rename(filename, filepath, filesize)