"""Telegram alerts + simple commands, only from your own chat id."""
import logging
import queue
import threading
import time

import requests

import config

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self):
        self.base = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"
        self.inbox = queue.Queue()
        self.offset = None
        threading.Thread(target=self._poll, daemon=True).start()

    def send(self, text):
        try:
            requests.post(f"{self.base}/sendMessage", timeout=15,
                          data={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True})
        except Exception as e:
            log.warning("telegram send failed: %s", e)

    def _poll(self):
        while True:
            try:
                r = requests.get(f"{self.base}/getUpdates", timeout=40,
                                 params={"timeout": 30, "offset": self.offset}).json()
                for u in r.get("result", []):
                    self.offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) == str(config.TELEGRAM_CHAT_ID) and msg.get("text"):
                        self.inbox.put(msg["text"].strip())
            except Exception as e:
                log.warning("telegram poll failed: %s", e)
                time.sleep(5)

    def messages(self):
        out = []
        while not self.inbox.empty():
            out.append(self.inbox.get())
        return out
