FROM python:3.12-slim

# unrar-free -> .cbr support; pymupdf -> .pdf support
RUN apt-get update && apt-get install -y --no-install-recommends unrar-free \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir flask pillow rarfile pymupdf

ENV LONGBOX_DOCKER=1
WORKDIR /app
COPY app/longbox.py .

EXPOSE 8767
CMD ["python", "longbox.py"]
