import os
import time
import requests
from datetime import datetime
from watchdog.events import FileSystemEventHandler

WATCH_FOLDER = "/home/node/.n8n-files"
WEBHOOK_URL = "http://n8n:5678/webhook-test/981184cc-f50d-4a42-bd24-4b8e0943f53e"
FILE_EXTENSIONS = ['.txt', '.pdf']
DEBOUNCE_SECONDS = 2

os.makedirs(WATCH_FOLDER, exist_ok=True)


# ---------------------------------------------------------------------------
# PDF extraction helpers
# ---------------------------------------------------------------------------

def extract_pdf_form_fields(filepath: str) -> dict:
    """
    Extract AcroForm field values from a digitally-filled PDF.
    Returns a dict of {field_label: value} or empty dict if none found.
    Requires: pip install pypdf
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(filepath)
        fields = reader.get_form_text_fields()
        if fields:
            # Remove empty values
            return {k: v for k, v in fields.items() if v and str(v).strip()}
        return {}
    except Exception as e:
        print(f"  ⚠ Form field extraction failed: {e}")
        return {}


def extract_pdf_text(filepath: str) -> str:
    """
    Extract plain text from a text-based PDF.
    Returns extracted text or empty string.
    Requires: pip install pdfplumber
    """
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            pages_text = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text.strip())
        return "\n".join(pages_text)
    except Exception as e:
        print(f"  ⚠ Text extraction failed: {e}")
        return ""


def extract_pdf_ocr(filepath: str) -> str:
    """
    OCR fallback for scanned PDFs.
    Requires: pip install pdf2image pytesseract
              apt-get install tesseract-ocr tesseract-ocr-deu poppler-utils
    Set ENABLE_OCR=true env variable to activate.
    """
    if not os.environ.get("ENABLE_OCR", "").lower() == "true":
        return ""
    try:
        from pdf2image import convert_from_path
        import pytesseract
        images = convert_from_path(filepath, dpi=200)
        texts = [pytesseract.image_to_string(img, lang="deu+eng") for img in images]
        return "\n".join(texts)
    except Exception as e:
        print(f"  ⚠ OCR failed: {e}")
        return ""


def build_pdf_payload(filename: str, filepath: str, filesize: int) -> dict:
    """
    Build a rich payload for PDFs by trying multiple extraction strategies:
      1. AcroForm fields (digitally filled forms)
      2. Embedded text (pdfplumber)
      3. OCR (optional, for scanned pages)
    """
    payload = {
        "filename": filename,
        "filepath": filepath,
        "filesize": filesize,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "extractionMethod": None,
        "formFields": {},
        "textContent": "",
    }

    # Strategy 1: form fields (best for fillable German medical forms)
    fields = extract_pdf_form_fields(filepath)
    if fields:
        print(f"  📋 Found {len(fields)} form field(s): {list(fields.keys())[:5]}")
        payload["formFields"] = fields
        payload["extractionMethod"] = "acroform"
        # Try to derive a meaningful name from common field names
        name_keys = [k for k in fields if any(
            kw in k.lower() for kw in ["name", "vorname", "patient", "versicherter", "nachname"]
        )]
        if name_keys:
            payload["suggestedName"] = " ".join(
                str(fields[k]) for k in name_keys[:2]
            ).strip()

    # Strategy 2: embedded text
    text = extract_pdf_text(filepath)
    if text:
        payload["textContent"] = text[:4000]  # cap to avoid huge payloads
        if not payload["extractionMethod"]:
            payload["extractionMethod"] = "text"

    # Strategy 3: OCR (only if nothing found yet and env var set)
    if not text and not fields:
        ocr_text = extract_pdf_ocr(filepath)
        if ocr_text:
            payload["textContent"] = ocr_text[:4000]
            payload["extractionMethod"] = "ocr"

    if not payload["extractionMethod"]:
        payload["extractionMethod"] = "none"
        print(f"  ⚠ No text content found — PDF may be a blank template or scanned without OCR enabled")

    return payload


def build_txt_payload(filename: str, filepath: str, filesize: int) -> dict:
    """Read plain text files directly."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(4000)
    except OSError as e:
        content = ""
        print(f"  ⚠ Could not read text file: {e}")
    return {
        "filename": filename,
        "filepath": filepath,
        "filesize": filesize,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "extractionMethod": "text",
        "formFields": {},
        "textContent": content,
    }


# ---------------------------------------------------------------------------
# Watchdog handler
# ---------------------------------------------------------------------------

class ScanSnapHandler(FileSystemEventHandler):
    def __init__(self):
        self.last_triggered = {}

    def on_created(self, event):
        self._handle_event(event)

    def send_to_n8n_and_rename(self, filename: str, filepath: str, filesize: int):
        """Extract content, send to n8n, get new name, rename file."""
        _, ext = os.path.splitext(filename)

        if ext.lower() == ".pdf":
            payload = build_pdf_payload(filename, filepath, filesize)
        else:
            payload = build_txt_payload(filename, filepath, filesize)

        method = payload.get("extractionMethod", "none")
        print(f"  📄 Extraction method: {method}")

        try:
            print(f"  → Sending to n8n...")
            response = requests.post(WEBHOOK_URL, json=payload, timeout=30)

            if response.ok:
                result = response.json()
                print(f"  ✓ n8n processed successfully")

                new_filename = result.get("newFilename")
                if new_filename and new_filename != filename:
                    directory = os.path.dirname(filepath)
                    new_path = os.path.join(directory, new_filename)
                    if os.path.exists(new_path):
                        print(f"  ⚠ Target already exists: {new_filename}")
                        return
                    os.rename(filepath, new_path)
                    print(f"  ✓ Renamed: {filename} → {new_filename}")
                else:
                    print(f"  ℹ No rename needed")
            else:
                print(f"  ✗ n8n error {response.status_code}: {response.text[:200]}")

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

        time.sleep(2)  # wait for file to finish writing
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] New file detected: {filename}")

        try:
            filesize = os.path.getsize(filepath)
        except OSError:
            filesize = 0

        self.send_to_n8n_and_rename(filename, filepath, filesize)
        print()