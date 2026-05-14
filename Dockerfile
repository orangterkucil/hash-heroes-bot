FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Jakarta

WORKDIR /app

# Install deps first so subsequent code edits keep the layer cached.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy source. accounts.json + totals.json are mounted at runtime via volume.
COPY api.py bot.py ./

# Default: loop forever, 5s pause between accounts.
CMD ["python", "-u", "bot.py", "--loop", "--sleep", "5"]
