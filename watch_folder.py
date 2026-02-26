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
    """Extract AcroForm field values from a digitally-filled PDF."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(filepath)
        fields = reader.get_form_text_fields()
        if fields:
            return {k: v for k, v in fields.items() if v and str(v).strip()}
        return {}
    except Exception as e:
        print(f"  ⚠ Form field extraction failed: {e}")
        return {}


def extract_pdf_text(filepath: str) -> str:
    """Extract plain text from a text-based (non-scanned) PDF."""
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
    OCR for scanned PDFs using pypdfium2 (already installed) + tesseract.

    Container setup required — add to your Dockerfile:
        RUN apt-get update && apt-get install -y tesseract-ocr tesseract-ocr-deu
        RUN pip install pytesseract

    Enable by setting env var ENABLE_OCR=true in docker-compose.yml
    """
    if os.environ.get("ENABLE_OCR", "").lower() != "true":
        return ""
    try:
        import pypdfium2 as pdfium
        import pytesseract

        print(f"  🔍 Running OCR on {os.path.basename(filepath)}...")
        doc = pdfium.PdfDocument(filepath)
        texts = []

        for page_index in range(len(doc)):
            page = doc[page_index]
            # Render at 250 DPI for good OCR accuracy (scale = dpi/72)
            bitmap = page.render(scale=250 / 72)
            pil_image = bitmap.to_pil()
            text = pytesseract.image_to_string(pil_image, lang="deu+eng")
            if text.strip():
                texts.append(text.strip())
            print(f"  🔍 Page {page_index + 1}: {len(text)} chars extracted")

        doc.close()
        return "\n".join(texts)

    except ImportError as e:
        print(f"  ⚠ OCR dependency missing: {e}")
        print(f"  ⚠ Add to Dockerfile: apt-get install tesseract-ocr tesseract-ocr-deu && pip install pytesseract")
        return ""
    except Exception as e:
        print(f"  ⚠ OCR failed: {e}")
        return ""


def clean_ocr_line(line: str) -> str:
    """
    Remove OCR checkbox artifacts that appear when tesseract reads form boxes.
    e.g. "Unger [| Unfall, Unfallfolge" → "Unger"
         "[x] ambulante Behandlung"      → "ambulante Behandlung"
    """
    import re
    # Remove checkbox patterns: [x], [ ], [|], |], [, |
    cleaned = re.sub(r'\[[\s\|xX]*\]|\[\||\|\]|\[|\|', ' ', line)
    # Collapse multiple spaces
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned


def parse_patient_info(text: str) -> dict:
    """
    Extract structured fields from OCR'd German medical transport forms.
    Handles the common layout of Muster 4 (Krankenbeförderung):
      - Insurance name on same line as "Krankenkasse" header or just below
      - Last name and first name on separate lines after "Name, Vorname" label
      - Last name line often has form labels merged in by OCR (e.g. "Unger [| Unfall")
      - Prescription date (Datum) is on the Arzt-Nr row, NOT the treatment date
    """
    import re

    # ── Restrict to page 1 only ───────────────────────────────────────────────
    page1 = re.split(r'Bitte die Fahrt|Bestätigung|Bestatigung', text, maxsplit=1)[0]
    raw_lines  = page1.split("\n")
    lines      = [l.strip() for l in raw_lines if l.strip()]
    clean_lines = [clean_ocr_line(l) for l in lines]

    info = {}

    # ── 1. Insurance company ──────────────────────────────────────────────────
    # Search first 20 lines for known insurer names
    for cl in clean_lines[:20]:
        kk_match = re.search(
            r'\b(BARMER|AOK|TK\b|DAK|BKK|IKK|KKH|HEK|Techniker\w*)\b',
            cl, re.IGNORECASE
        )
        if kk_match:
            info["krankenkasse"] = kk_match.group(0).upper()
            break

    # ── 2. Patient name ───────────────────────────────────────────────────────
    # Find "Name, Vorname des Versicherten" label, then:
    #   - Next non-empty cleaned line = last name (take only first token if
    #     OCR merged a form label onto the same line)
    #   - Line after that = first name (if it looks like a name, not an address)
    for i, line in enumerate(lines):
        if re.search(r'name.*vorname|name.*versicherten', line, re.IGNORECASE):
            lastname  = None
            firstname = None

            for j in range(i + 1, min(i + 6, len(lines))):
                cl = clean_lines[j]

                if len(cl) < 2:
                    continue

                # Always extract first word first — the name is the first token
                # even when OCR merges form labels: "Unger Unfall, Unfallfolge"
                first_word = cl.split()[0].rstrip(".,;:")
                is_name_token = bool(re.match(r'^[A-Za-zÄÖÜäöüß\-]{2,}$', first_word))

                if lastname is None:
                    # For lastname: grab first word if it looks like a name,
                    # regardless of what follows on the same line
                    if is_name_token:
                        lastname = first_word
                    # else skip (e.g. pure number lines, empty tokens)
                    continue

                elif firstname is None:
                    # For firstname: skip address / label lines entirely
                    if re.search(r'str\.|straße|stra.e|\d{5}|\bam\b|\d{2}\.\d{2}', cl, re.IGNORECASE):
                        continue
                    if re.search(
                        r'kostenträger|versicherten|krankenkasse|zuzahlung|'
                        r'unfall|arbeitsunfall|versorgungsleiden|hinfahrt|rückfahrt',
                        cl, re.IGNORECASE
                    ):
                        continue
                    if re.match(r'^[\d\s]+$', cl):
                        continue
                    if is_name_token:
                        firstname = first_word
                    break  # stop after first name attempt regardless

            if lastname:
                info["name"]      = lastname
                info["firstname"] = firstname or ""
            break

    # ── 3. Prescription date (Datum field next to Arzt-Nr.) ──────────────────
    # Look specifically for the line that contains "Arzt-Nr" and "Datum"
    # The date on that same line or the very next line is the prescription date.
    for i, line in enumerate(lines):
        if re.search(r'arzt.?nr|betriebsstätten', line, re.IGNORECASE):
            # Check this line and the next two for a date
            for j in range(i, min(i + 3, len(lines))):
                dates = re.findall(r'\b\d{1,2}[.\-]\d{2}[.\-]\d{2,4}\b', lines[j])
                if dates:
                    info["datum"] = dates[-1]  # last date on that line
                    break
            if "datum" in info:
                break

    # Fallback: last date on page 1 that isn't a birth year
    if "datum" not in info:
        all_dates = re.findall(r'\b\d{1,2}[.\-]\d{2}[.\-]\d{2,4}\b', page1)
        recent = [d for d in all_dates if not re.search(r'\.(19\d{2})$', d)]
        if recent:
            info["datum"] = recent[-1]
        elif all_dates:
            info["datum"] = all_dates[-1]

    return info


def suggest_filename_from_ocr(text: str, fallback_date: str) -> str:
    """Build a human-friendly filename from OCR text of a German transport form.
    Target pattern: BARMER_Unger_Ute_2026-02-18
    """
    import re
    info = parse_patient_info(text)
    parts = []

    if info.get("krankenkasse"):
        parts.append(info["krankenkasse"])

    if info.get("name"):
        parts.append(info["name"])

    if info.get("firstname"):
        parts.append(info["firstname"])

    if not parts:
        if re.search(r'krankenbeförd|krankenbefoerd', text, re.IGNORECASE):
            parts.append("Krankenbefoerderung")
        elif re.search(r'rezept', text, re.IGNORECASE):
            parts.append("Rezept")
        elif re.search(r'überweisung', text, re.IGNORECASE):
            parts.append("Ueberweisung")
        else:
            parts.append("Dokument")

    # Normalize date to YYYY-MM-DD
    if info.get("datum"):
        m = re.match(r'(\d{1,2})[.\-](\d{2})[.\-](\d{2,4})', info["datum"])
        if m:
            day, month, year = m.groups()
            year = "20" + year if len(year) == 2 else year
            parts.append(f"{year}-{month}-{day.zfill(2)}")
    else:
        parts.append(fallback_date)

    # Sanitize: allow letters (incl. German), digits, dash, underscore
    name = "_".join(parts)
    name = name.replace(" ", "_").replace("/", "_")
    name = "".join(c if (c.isalnum() or c in "-_äöüÄÖÜß") else "_" for c in name)
    # Collapse multiple underscores
    name = re.sub(r'_+', '_', name).strip("_")
    return name[:180]


def build_pdf_payload(filename: str, filepath: str, filesize: int) -> dict:
    """
    Build payload using three strategies:
      1. AcroForm fields (digitally filled forms)
      2. Embedded text layer (pdfplumber)
      3. OCR (scanned documents — requires ENABLE_OCR=true)
    """
    fallback_date = datetime.utcnow().strftime("%Y-%m-%d")

    payload = {
        "filename": filename,
        "filepath": filepath,
        "filesize": filesize,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "extractionMethod": None,
        "formFields": {},
        "textContent": "",
        "suggestedName": None,
    }

    # Strategy 1: AcroForm fields
    fields = extract_pdf_form_fields(filepath)
    if fields:
        print(f"  📋 Found {len(fields)} form field(s): {list(fields.keys())[:5]}")
        payload["formFields"] = fields
        payload["extractionMethod"] = "acroform"
        name_keys = [k for k in fields if any(
            kw in k.lower() for kw in ["name", "vorname", "patient", "versicherter", "nachname"]
        )]
        if name_keys:
            payload["suggestedName"] = "_".join(
                str(fields[k]) for k in name_keys[:2]
            ).strip()

    # Strategy 2: Embedded text
    text = extract_pdf_text(filepath)
    if text:
        payload["textContent"] = text[:4000]
        if not payload["extractionMethod"]:
            payload["extractionMethod"] = "text"

    # Strategy 3: OCR (scanned PDFs)
    if not text and not fields:
        ocr_text = extract_pdf_ocr(filepath)
        if ocr_text:
            payload["textContent"] = ocr_text[:4000]
            payload["extractionMethod"] = "ocr"

            # ── DEBUG: print full OCR output so you can see what was extracted ──
            print("  ─── FULL OCR TEXT ───────────────────────────────────────")
            for i, line in enumerate(ocr_text.split("\n")):
                print(f"  {i:03d} | {line}")
            print("  ─────────────────────────────────────────────────────────")

            suggested = suggest_filename_from_ocr(ocr_text, fallback_date)
            payload["suggestedName"] = suggested
            print(f"  💡 Suggested filename: {suggested}")
        else:
            payload["extractionMethod"] = "none"
            if os.environ.get("ENABLE_OCR", "").lower() != "true":
                print(f"  ⚠ Scanned PDF detected — set ENABLE_OCR=true to enable OCR")
            else:
                print(f"  ⚠ OCR ran but found no text")

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
        "suggestedName": None,
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
        _, ext = os.path.splitext(filename)
        payload = build_pdf_payload(filename, filepath, filesize) if ext.lower() == ".pdf" \
            else build_txt_payload(filename, filepath, filesize)

        print(f"  📄 Extraction method: {payload.get('extractionMethod', 'none')}")

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

        time.sleep(2)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] New file detected: {filename}")

        try:
            filesize = os.path.getsize(filepath)
        except OSError:
            filesize = 0

        self.send_to_n8n_and_rename(filename, filepath, filesize)
        print()