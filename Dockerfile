FROM python:3.12.12-trixie

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        git \
        tesseract-ocr \
        tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# pytesseract installed separately so it's explicit and easy to remove if not needed
RUN pip install --no-cache-dir pytesseract

COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]