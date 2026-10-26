FROM python:3.11-slim

# FFmpeg (with caption support) and fonts for English + Hindi/Nepali captions
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fontconfig fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV PATH=/home/user/.local/bin:$PATH \
    HOSTED=1 PORT=7860 PYTHONUNBUFFERED=1 PYTHONUTF8=1 \
    FONT_LATIN="DejaVu Sans" FONT_DEVANAGARI="Noto Sans Devanagari" \
    HF_HOME=/home/user/.cache/huggingface
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# bake the fast speech model into the image so the first video starts quickly
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"

COPY --chown=user . .
EXPOSE 7860
CMD ["python", "app.py"]
