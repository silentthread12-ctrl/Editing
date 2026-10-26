---
title: Shorts Maker
emoji: 🎬
colorFrom: yellow
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Shorts Maker

Turns a long video into vertical short-form clips with captions. English, Hindi, Nepali and Hinglish.
Free and open source tools only (FFmpeg, faster-whisper, optional free Gemini key).

## Two ways to run it

**Online (Hugging Face Space, free):** create a Space with SDK = Docker, upload these files.
Optional secrets in the Space settings: `GEMINI_API_KEY` (smarter clip picking) and `APP_PASSWORD`.
Make the Space *private* so only you can open it.

**On your PC (Windows, fastest):** download this repo as a ZIP, unzip, double-click `start.bat`.
Then open http://127.0.0.1:5055
