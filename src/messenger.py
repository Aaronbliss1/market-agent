"""Headless Telegram messenger — plain Bot API HTTP calls (no long polling).

Used in headless / GitHub Actions mode, where no persistent listener process
exists. send_text / send_photo mirror the messenger interface used by bot.py.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("messenger")

API = "https://api.telegram.org/bot{token}/{method}"


class HeadlessMessenger:
    def __init__(self, token: str, chat_id: int | str):
        self.token = token
        self.chat_id = chat_id
        self.s = requests.Session()

    # ------------------------------------------------------------- plumbing
    def _call(self, method: str, payload: dict, files: dict | None = None,
              tries: int = 3) -> dict | None:
        for attempt in range(1, tries + 1):
            try:
                if files:
                    r = self.s.post(API.format(token=self.token, method=method),
                                    data=payload, files=files, timeout=90)
                else:
                    r = self.s.post(API.format(token=self.token, method=method),
                                    json=payload, timeout=60)
                d = r.json()
                if d.get("ok"):
                    return d
                log.error("telegram %s failed: %s", method, str(d)[:200])
            except Exception as e:
                log.error("telegram %s error (attempt %d/%d): %s",
                          method, attempt, tries, e)
            if attempt < tries:
                time.sleep(2 * attempt)
        return None

    # --------------------------------------------------------------- public
    def send_text(self, text: str):
        """Send text, chunking Telegram's 4096-char limit."""
        for i in range(0, len(text), 4000):
            self._call("sendMessage", {"chat_id": self.chat_id,
                                       "text": text[i:i + 4000]})

    def send_photo(self, path: str, caption: str = ""):
        with open(path, "rb") as f:
            self._call("sendPhoto",
                       {"chat_id": self.chat_id, "caption": caption[:1024]},
                       files={"photo": (path.rsplit("/", 1)[-1], f, "image/png")})
