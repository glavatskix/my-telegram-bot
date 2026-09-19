"""
Telegram + Gemini бот через вебхук — версия для бесплатного serverless-хостинга (Vercel),
с памятью разговора через Upstash Redis (тоже бесплатный, специально сделан для serverless —
работает по обычным HTTP-запросам, без "постоянного соединения", которое здесь недоступно).

Переменные окружения (задаются в панели Vercel, НЕ в этом файле):
  TELEGRAM_TOKEN            — токен бота от @BotFather
  GEMINI_API_KEY            — твой ключ Gemini API
  UPSTASH_REDIS_REST_URL    — адрес базы из консоли Upstash (для памяти; необязательно —
                               без него бот просто не будет помнить историю)
  UPSTASH_REDIS_REST_TOKEN  — токен базы из консоли Upstash
"""

import os
import json
import base64
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
MAX_HISTORY_TURNS = 10  # сколько последних обменов репликами помнить на один чат
HISTORY_TTL_SECONDS = 60 * 60 * 24 * 30  # чистим память неактивного чата через 30 дней


def redis_command(*args):
    """Отправляет одну команду в Upstash Redis через REST API. Если база не настроена
    (переменные окружения пустые) — тихо возвращает None, бот просто работает без памяти."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    resp = requests.post(UPSTASH_URL, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
                          json=list(args), timeout=10)
    resp.raise_for_status()
    return resp.json().get("result")


def load_history(chat_id):
    raw = redis_command("GET", f"chat_history:{chat_id}")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def save_history(chat_id, history):
    trimmed = history[-MAX_HISTORY_TURNS * 2:]
    redis_command("SET", f"chat_history:{chat_id}", json.dumps(trimmed, ensure_ascii=False),
                  "EX", str(HISTORY_TTL_SECONDS))


def telegram_send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)] or [""]
    for chunk in chunks:
        requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=30)


def telegram_get_file_path(file_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
    resp = requests.get(url, params={"file_id": file_id}, timeout=30)
    resp.raise_for_status()
    return resp.json()["result"]["file_path"]


def telegram_download_file(file_path):
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def call_gemini_text(history):
    """history — список [{"role": "user"/"model", "text": "..."}], последний — новое сообщение."""
    contents = [{"role": h["role"], "parts": [{"text": h["text"]}]} for h in history]
    payload = {"contents": contents}
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=55)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def call_gemini_with_image(history, caption, image_bytes, mime_type):
    """То же самое, но последнее сообщение — с приложенной картинкой (например, фото еды).
    Прошлая история идёт обычным текстом, картинка — только к новому сообщению."""
    contents = [{"role": h["role"], "parts": [{"text": h["text"]}]} for h in history]
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    text_part = {"text": caption.strip() if caption else "Что скажешь про это фото?"}
    image_part = {"inline_data": {"mime_type": mime_type, "data": b64}}
    contents.append({"role": "user", "parts": [text_part, image_part]})
    payload = {"contents": contents}
    headers = {"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"}
    resp = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=55)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def handle_update(update):
    """Вся логика в одной функции, отдельно от Flask — так её проще тестировать напрямую,
    без необходимости поднимать настоящий HTTP-сервер."""
    message = update.get("message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    history = load_history(chat_id)

    try:
        if "photo" in message:
            caption = message.get("caption", "")
            file_id = message["photo"][-1]["file_id"]  # самое большое разрешение
            file_path = telegram_get_file_path(file_id)
            image_bytes = telegram_download_file(file_path)
            reply = call_gemini_with_image(history, caption, image_bytes, "image/jpeg")
            history.append({"role": "user", "text": f"[прислал(а) фото] {caption}".strip()})
        elif "text" in message:
            user_text = message["text"]
            history_with_new = history + [{"role": "user", "text": user_text}]
            reply = call_gemini_text(history_with_new)
            history.append({"role": "user", "text": user_text})
        else:
            reply = "Пока умею отвечать только на текст и фото."
            telegram_send_message(chat_id, reply)
            return

        history.append({"role": "model", "text": reply})
        save_history(chat_id, history)
    except Exception as e:
        reply = f"Не получилось обработать сообщение: {e}"

    telegram_send_message(chat_id, reply)


@app.route("/", methods=["POST"])
@app.route("/api/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    handle_update(update)
    return jsonify({"ok": True})


@app.route("/", methods=["GET"])
@app.route("/api/webhook", methods=["GET"])
def health():
    memory_status = "включена" if (UPSTASH_URL and UPSTASH_TOKEN) else "выключена (нет переменных Upstash)"
    return jsonify({"status": "ok", "memory": memory_status,
                     "note": "Telegram webhook endpoint — отправь POST от Telegram сюда."})
