import calendar
import datetime as dt
import hmac
import html
import logging
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

import requests
from flask import Flask, Response, abort, make_response, redirect, render_template_string, request
from werkzeug.exceptions import HTTPException
from google.cloud import firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter
from google.api_core.exceptions import AlreadyExists

import bigfiles
import metronome
import music

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
CRON_SECRET = os.environ["CRON_SECRET"]
WEB_TOKEN = os.environ["WEB_TOKEN"]
BUCKET = os.environ["BUCKET"]
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Berlin"))
# Recordings before this hour count for the previous day
DAY_START_HOUR = int(os.environ.get("DAY_START_HOUR", "4"))
# Hours from midnight to the start of a practice day; an evening hour (e.g. 23) starts the next day early
DAY_OFFSET = DAY_START_HOUR if DAY_START_HOUR < 12 else DAY_START_HOUR - 24

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
COOKIE = "pz"
WHO_COOKIE = "pz_who"
ALERT_INTERVAL_SEC = 600
TOKEN_CACHE_SEC = 30
# Defaults; the admin can change them in private chat with /ekle and /cikar
STREAK_MILESTONES = {7, 14, 21, 30, 60, 365}
COUNT_MILESTONES = {1, 5, 31, 50, 69, 100}
MAX_TG_BYTES = 20 * 1024 * 1024
HISTORY_DAYS = 20
MEDIA_EXTS = {"ogg", "oga", "opus", "mp3", "m4a", "aac", "wav", "flac", "aif", "aiff", "wma",
              "mp4", "mov", "m4v", "webm", "mkv", "avi", "3gp"}

TR_MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
TR_DAYS_SHORT = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pratik")

db = firestore.Client()
bucket = storage.Client().bucket(BUCKET)
app = Flask(__name__)

BOT_COMMANDS = [
    ("bugun", "Bugün kim kaydetti"),
    ("seri", "Son 20 gün"),
    ("detay", "Herkesin istatistikleri"),
    ("takvim", "Tüm kayıtların takvimi"),
    ("katil", "Gruba katıl"),
    ("ayril", "Hatırlatmalardan çık"),
    ("sil", "Kendi kaydına yanıt vererek sil"),
    ("kaydet", "Bot kaydetmediyse kayda yanıt vererek kaydet"),
    ("sohbet", "Attıklarını takvime ekleme"),
    ("sarkilarim", "Bitmeyen şarkıların"),
    ("bitti", "Şarkının kaydına yanıt vererek bitir"),
    ("notsil", "Nota ya da kayda yanıt vererek notu sil"),
    ("metronomvar", "Kayda yanıt vererek metronomlu işaretle"),
    ("metronomyok", "Kayda yanıt vererek metronomsuz işaretle"),
    ("prova", "Prova günü anketi; /prova bitir ile sonuçlanır"),
    ("ilham", "Rastgele bir pratik fikri"),
    ("zar", "Zar at"),
    ("pratik", "Sohbet modunu bitir"),
    ("cikar", "Kaydı pratikten çıkar, mesaj grupta kalır"),
    ("yenilink", "Takvim linkini yenile"),
    ("yardim", "Nasıl çalışır"),
]


# ---------- helpers ----------

def now_local():
    return dt.datetime.now(TZ)


def practice_day(ts):
    return (ts - dt.timedelta(hours=DAY_OFFSET)).date()


def today():
    return practice_day(now_local())


# Failures of these calls are harmless and not worth an alert
QUIET_METHODS = {"setMessageReaction", "setMyCommands", "deleteMessage"}
_last_alert = {}


def alert(key, text):
    """Sends a rate-limited private message to the admin; never raises."""
    now = time.time()
    if now - _last_alert.get(key, 0) < ALERT_INTERVAL_SEC:
        return
    _last_alert[key] = now
    try:
        admin = get_admin_id()
        if admin:
            requests.post(f"{API}/sendMessage", timeout=15,
                          json={"chat_id": admin, "text": f"⚠️ Pratik botu: {text}"[:4000]})
    except Exception:
        log.exception("alert failed")


# Read-only calls are safe to repeat after a timeout; a repeated send could post twice
SAFE_RETRY = {"getFile", "getMe", "getWebhookInfo", "getChatMember"}


def post_api(method, params):
    for attempt in range(3):
        try:
            return requests.post(f"{API}/{method}", json=params, timeout=30)
        except (requests.ConnectionError, requests.Timeout) as e:
            # A connection that never opened did not reach Telegram, so any call can be repeated
            retry = isinstance(e, requests.ConnectTimeout) or not isinstance(e, requests.Timeout) \
                or method in SAFE_RETRY
            if not retry or attempt == 2:
                raise
            log.warning("telegram %s attempt %d failed: %r", method, attempt + 1, e)
            time.sleep(2 * (attempt + 1))


def tg(method, **params):
    try:
        for _ in range(3):
            r = post_api(method, params)
            data = r.json()
            wait = (data.get("parameters") or {}).get("retry_after")
            if data.get("ok") or data.get("error_code") != 429 or not wait:
                break
            time.sleep(min(wait, 30))
        if not data.get("ok"):
            log.warning("telegram %s failed: %s", method, data)
            if method not in QUIET_METHODS:
                alert(f"tg:{method}", f"{method} başarısız: {data.get('description', data)}")
            return None
        return data["result"]
    except Exception as e:
        log.exception("telegram %s error", method)
        if method not in QUIET_METHODS:
            alert(f"tg:{method}", f"{method} hatası: {e!r}")
        return None


ALLOWED_UPDATES = ["message", "edited_message", "message_reaction", "poll_answer"]


def register_commands():
    tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
    # Keeps the webhook subscribed to reactions without a manual re-run of deploy.sh
    if PUBLIC_URL:
        info = tg("getWebhookInfo")
        info = info if isinstance(info, dict) else {}
        if sorted(info.get("allowed_updates") or []) != sorted(ALLOWED_UPDATES):
            tg("setWebhook", url=f"{PUBLIC_URL}/telegram", secret_token=WEBHOOK_SECRET,
               allowed_updates=ALLOWED_UPDATES)


def send(chat_id, text, reply_to=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return tg("sendMessage", **params)


def display_name(u):
    name = " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x).strip()
    return name or u.get("username") or str(u.get("id"))


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


def fmt_duration(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def tr_date(d, with_weekday=True):
    s = f"{d.day} {TR_MONTHS[d.month - 1]}"
    return f"{s}, {TR_DAYS[d.weekday()]}" if with_weekday else s


# ---------- state ----------

def field(snap, name):
    """Reads a document field; missing documents and fields give None instead of raising."""
    return (snap.to_dict() or {}).get(name) if snap.exists else None


def state_ref():
    return db.collection("config").document("state")


def get_admin_id():
    snap = state_ref().get()
    return field(snap, "admin_id")


_token_cache = {"value": None, "at": 0.0}


def web_token():
    if time.time() - _token_cache["at"] > TOKEN_CACHE_SEC:
        snap = state_ref().get()
        stored = field(snap, "web_token")
        _token_cache.update(value=stored or WEB_TOKEN, at=time.time())
    return _token_cache["value"]


def rotate_web_token():
    new = secrets.token_hex(16)
    state_ref().set({"web_token": new}, merge=True)
    _token_cache.update(value=new, at=time.time())
    return new


def get_chat_id():
    if ALLOWED_CHAT_ID:
        return ALLOWED_CHAT_ID
    snap = state_ref().get()
    cid = field(snap, "chat_id")
    return str(cid) if cid else None


def accept_chat(chat):
    cid = str(chat["id"])
    if ALLOWED_CHAT_ID:
        return cid == ALLOWED_CHAT_ID
    current = get_chat_id()
    if current:
        return cid == current
    if chat.get("type") in ("group", "supergroup"):
        state_ref().set({"chat_id": cid, "title": chat.get("title", "")}, merge=True)
        return True
    return False


def upsert_member(u, activate=True):
    ref = db.collection("members").document(str(u["id"]))
    snap = ref.get()
    data = {"name": display_name(u), "username": u.get("username", "")}
    if not snap.exists:
        data["first_day"] = today().isoformat()
        data["active"] = True
    elif activate and not field(snap, "active"):
        data["active"] = True
        data["left_day"] = firestore.DELETE_FIELD
    ref.set(data, merge=True)


def deactivate_member(uid):
    ref = db.collection("members").document(str(uid))
    if ref.get().exists:
        ref.set({"active": False, "left_day": today().isoformat()}, merge=True)


def load_members():
    out = []
    for s in db.collection("members").stream():
        m = s.to_dict()
        m["id"] = s.id
        out.append(m)
    out.sort(key=lambda m: (m.get("first_day", ""), m.get("name", "")))
    return out


def load_days(since, with_metro=False):
    days, metro = {}, {}
    q = (db.collection("recordings")
         .where(filter=FieldFilter("day", ">=", since.isoformat()))
         .select(["user_id", "day", "metronome"]))
    for s in q.stream():
        d = s.to_dict()
        uid = str(d["user_id"])
        days.setdefault(uid, set()).add(d["day"])
        if d.get("metronome"):
            metro.setdefault(uid, set()).add(d["day"])
    return (days, metro) if with_metro else days


def week_start(d):
    return d - dt.timedelta(days=d.weekday())


def load_stats():
    """Per user: recorded days, metronome days, total seconds, recording count."""
    stats = {}
    q = db.collection("recordings").select(["user_id", "day", "metronome", "duration"])
    for s in q.stream():
        d = s.to_dict()
        st = stats.setdefault(str(d["user_id"]), {"days": set(), "metro": set(), "sec": 0, "count": 0})
        st["days"].add(d["day"])
        if d.get("metronome"):
            st["metro"].add(d["day"])
        st["sec"] += int(d.get("duration") or 0)
        st["count"] += 1
    return stats


def empty_stats():
    return {"days": set(), "metro": set(), "sec": 0, "count": 0}


def history(member, st, t):
    """Daily status from the member's first day to the last finished day.

    Status per day: metro, done, joker (first miss of its Mon-Sun week) or missed.
    Today counts only once it has a recording.
    """
    first_iso = member.get("first_day") or (min(st["days"]) if st["days"] else t.isoformat())
    first = dt.date.fromisoformat(first_iso)
    if st["days"]:
        first = min(first, dt.date.fromisoformat(min(st["days"])))
    end = t if t.isoformat() in st["days"] else t - dt.timedelta(days=1)
    out, jokers = [], set()
    d = first
    while d <= end:
        iso = d.isoformat()
        if iso in st["metro"]:
            status = "metro"
        elif iso in st["days"]:
            status = "done"
        elif week_start(d) not in jokers:
            jokers.add(week_start(d))
            status = "joker"
        else:
            status = "missed"
        out.append((d, status))
        d += dt.timedelta(days=1)
    return out


def chain_stats(hist):
    """Current and longest streak; joker days keep the chain but add nothing."""
    longest = run = 0
    for _, status in hist:
        if status in ("metro", "done"):
            run += 1
        elif status == "missed":
            run = 0
        longest = max(longest, run)
    return run, longest


def joker_used_this_week(hist, t):
    ws = week_start(t)
    return any(status == "joker" and d >= ws for d, status in hist)


def current_chain_start(hist):
    start = None
    for d, status in hist:
        if status == "missed":
            start = None
        elif status in ("metro", "done") and start is None:
            start = d
    return start


# Words in a file name or note that mean the whole song was played
DONE_WORDS = ["bitti", "bitirdim", "tamami", "tamamı", "tamamladim", "tamamladım",
              "komple", "full", "complete", "completed", "finished", "done", "sonuna kadar",
              "bastan sona", "baştan sona", "eksiksiz", "✅"]
_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")


def _fold(text):
    return text.replace("İ", "i").replace("I", "ı").lower().translate(_FOLD)


_DONE_ALT = "|".join(sorted({re.escape(_fold(w)) for w in DONE_WORDS}, key=len, reverse=True))
_DONE_RE = re.compile(r"(?<![\w])(" + _DONE_ALT + r")(?![\w])")
# In a title only a trailing marker counts, so "Full Moon" stays a song name
_DONE_END_RE = re.compile(r"[\s\-_(\[]*(?<![\w])(" + _DONE_ALT + r")[\s)\]!.]*$")


def song_of(rec):
    """(title, key, progress, finished) from the file name and note, or None."""
    fname = (rec.get("file_name") or "").strip()
    caption = note_text(rec)
    if fname:
        title = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", fname)
        progress = caption
    elif caption:
        title, _, progress = caption.partition("\n")
    else:
        return None
    title = re.sub(r"[_\s]+", " ", title).strip(" -")
    # Date stamps such as 261004_ or 2026-10-04 in file names are not part of the song
    title = re.sub(r"^(\d{6,8}|\d{4}[-.]\d{2}[-.]\d{2}|\d{2}[-.]\d{2}[-.]\d{2,4})\b[\s\-]*", "", title)
    title = re.sub(r"[\s\-]*\b(\d{6,8}|\d{4}[-.]\d{2}[-.]\d{2})$", "", title).strip(" -") or title
    if " " not in title and "-" in title:
        title = title.replace("-", " ")
    tail = _DONE_END_RE.search(_fold(title))
    if tail:
        title = title[:tail.start()].strip(" -")
    title = re.sub(r"[\s\-]*\b(take|v|versiyon|version|kayıt|kayit|deneme)?\s*\d+$", "", title,
                   flags=re.IGNORECASE).strip(" -") or title
    finished = bool(rec.get("song_done")) or bool(tail) or bool(_DONE_RE.search(_fold(progress)))
    key = _fold(title)
    key = re.sub(r"\b(take|v|versiyon|version|kayit|deneme)\s*\d*\b", " ", key)
    key = re.sub(r"[^a-z0-9]+", " ", key)
    key = re.sub(r"(\s\d+)+$", "", key.strip()).strip()
    if not key:
        return None
    return title, key, progress.strip(), finished


def open_songs(uid):
    songs = {}
    for snap in db.collection("recordings").where(filter=FieldFilter("user_id", "==", str(uid))).stream():
        r = snap.to_dict()
        info = song_of(r)
        if not info:
            continue
        title, key, progress, finished = info
        s = songs.setdefault(key, {"title": title, "finished": False, "last": None, "progress": "",
                                   "order": None, "p_order": None})
        # Message ids break ties between takes sent within the same second
        mid = snap.id.rsplit("_", 1)[-1]
        order = (r["ts"], int(mid) if mid.isdigit() else 0)
        s["finished"] |= finished
        if s["order"] is None or order > s["order"]:
            s["order"], s["last"] = order, r["ts"]
            # Keep a capitalized spelling over a later all-lowercase one
            if title != title.lower() or s["title"] == s["title"].lower():
                s["title"] = title
        if progress and (s["p_order"] is None or order > s["p_order"]):
            s["progress"], s["p_order"] = progress, order
    return sorted((s for s in songs.values() if not s["finished"]), key=lambda s: s["order"], reverse=True)


IDEAS = [
    "Bugün sadece pentatonik, metronom 70'te.",
    "Sevdiğin bir solonun ilk 4 ölçüsünü kulaktan çıkar.",
    "Bildiğin bir parçayı yarı hızda, her notayı temiz çalarak kaydet.",
    "5 dakika boyunca sadece tek bir akorla doğaçlama yap.",
    "Bir gamı üçlü aralıklarla çal: 1-3, 2-4, 3-5...",
    "Metronomu sadece 2. ve 4. vuruşa koy, groove'u sen tut.",
    "Bugün en sevmediğin tonda çal.",
    "Bir parçayı hiç tekrar etmeden baştan sona tek seferde kaydet.",
    "Sadece iki nota kullanarak bir melodi uydur.",
    "Bildiğin bir riffi bir oktav yukarıda ya da aşağıda çal.",
    "10 dakika sadece ritim: aynı akoru farklı ritim kalıplarıyla çal.",
    "Dün çaldığın şeyi bugün 10 bpm daha hızlı dene.",
    "Gözlerin kapalı çal, sadece kulağına güven.",
    "Bir şarkının melodisini önce söyle, sonra çal.",
    "Bugün dinamik günü: aynı cümleyi çok yumuşak ve çok sert çal.",
    "Bir parçanın en zor 2 ölçüsünü seç, sadece onları çalış.",
    "Bildiğin bir akor dizisini başka bir tona aktar.",
    "Bugün staccato günü: her notayı kısa ve net çal.",
    "1 dakikalık bir kayıt at, içinde bir hata bile olmasın.",
    "Grupta başkasının dün attığı parçayı sen de dene.",
]

MARK = {"metro": "🔥", "done": "❤", "joker": "🃏", "missed": "💔"}


def fmt_total(sec):
    h, m = divmod(int(sec) // 60, 60)
    if h:
        return f"{h} sa {m} dk"
    if m:
        return f"{m} dk"
    return "<1 dk" if sec else "0 dk"


register_commands()


# ---------- telegram handling ----------

def handle_update(upd):
    if upd.get("message_reaction"):
        handle_reaction(upd["message_reaction"])
        return
    if upd.get("poll_answer"):
        handle_poll_answer(upd["poll_answer"])
        return
    msg = upd.get("message")
    edited = upd.get("edited_message")
    if edited:
        handle_edit(edited)
        return
    if not msg:
        return

    chat = msg["chat"]
    if msg.get("migrate_to_chat_id") and not ALLOWED_CHAT_ID:
        state_ref().set({"chat_id": str(msg["migrate_to_chat_id"])}, merge=True)
        return
    if chat.get("type") == "private":
        handle_private(msg)
        return
    if not accept_chat(chat):
        return

    for u in msg.get("new_chat_members", []):
        if not u.get("is_bot"):
            upsert_member(u)
    if msg.get("left_chat_member"):
        gone = msg["left_chat_member"]
        deactivate_member(gone["id"])
        return

    user = msg.get("from") or {}
    if not user or user.get("is_bot"):
        return

    # Replying to someone else's recording counts as having listened
    rep = msg.get("reply_to_message")
    if rep and not (msg.get("text") or "").strip().startswith("/"):
        add_listener(chat["id"], rep["message_id"], user["id"])

    media, kind = find_media(msg)

    if media:
        if has_left(user["id"]):
            send(chat["id"], "Ayrıldığın için bu kayıt sayılmadı. Katılmak için /katil yaz, "
                             "sonra bu kayda yanıt verip /kaydet ile ekleyebilirsin.", msg["message_id"])
        elif not chat_mode(user["id"]):
            save_recording(msg, user, media, kind)
        return

    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].replace("İ", "i").replace("I", "ı").lower()
        cmd = cmd.translate(str.maketrans("çğıöşü", "cgiosu"))
        handle_command(cmd, msg, user)
        return

    # Text reply to own recording becomes its note
    rep = msg.get("reply_to_message")
    if text and rep and (rep.get("from") or {}).get("id") == user.get("id") and \
            find_media(rep)[0]:
        ref = db.collection("recordings").document(f'{chat["id"]}_{rep["message_id"]}')
        snap = ref.get()
        if snap.exists:
            notes = dict(field(snap, "notes") or {})
            notes[str(msg["message_id"])] = text
            ref.update({"notes": notes, "note_ids": sorted({*(field(snap, "note_ids") or []), msg["message_id"]})})
            tg("setMessageReaction", chat_id=chat["id"], message_id=msg["message_id"],
               reaction=[{"type": "emoji", "emoji": "✍"}])


def note_owner(chat_id, note_mid):
    """The recording a reply note belongs to, as (ref, data), or None."""
    q = db.collection("recordings").where(filter=FieldFilter("note_ids", "array_contains", int(note_mid)))
    for snap in q.stream():
        if snap.id.startswith(f"{chat_id}_"):
            return db.collection("recordings").document(snap.id), snap.to_dict()
    return None


def handle_edit(edited):
    chat_id = edited["chat"]["id"]
    ref = db.collection("recordings").document(f'{chat_id}_{edited["message_id"]}')
    snap = ref.get()
    if snap.exists:
        # Removing the caption in Telegram arrives as an edit without one
        ref.update({"caption": edited.get("caption") or ""})
        return
    text = (edited.get("text") or "").strip()
    found = note_owner(chat_id, edited["message_id"]) if text else None
    if found:
        nref, data = found
        notes = dict(data.get("notes") or {})
        notes[str(edited["message_id"])] = text
        nref.update({"notes": notes})


def note_text(rec):
    """Caption and reply notes of a recording, oldest first."""
    notes = rec.get("notes") or {}
    parts = [rec.get("caption") or ""] + [notes[k] for k in sorted(notes, key=int)]
    return "\n".join(p for p in parts if p.strip()).strip()


def delete_note(msg, user):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    rep = msg.get("reply_to_message")
    if not rep:
        send(chat_id, "Silmek istediğin nota ya da kaydın kendisine yanıt olarak /notsil yaz.", mid)
        return
    admin = str(get_admin_id() or "") == str(user["id"])
    ref = db.collection("recordings").document(f'{chat_id}_{rep["message_id"]}')
    snap = ref.get()
    if snap.exists:
        if str(field(snap, "user_id")) != str(user["id"]) and not admin:
            send(chat_id, "Sadece kendi kaydının notlarını silebilirsin.", mid)
            return
        ref.update({"caption": "", "notes": {}, "note_ids": []})
        send(chat_id, "🗑 Bu kaydın tüm notları takvimden silindi.", mid)
        return
    found = note_owner(chat_id, rep["message_id"])
    if not found:
        send(chat_id, "Bu mesaj bir kaydın notu değil.", mid)
        return
    nref, data = found
    if str(data.get("user_id")) != str(user["id"]) and not admin:
        send(chat_id, "Sadece kendi notlarını silebilirsin.", mid)
        return
    notes = dict(data.get("notes") or {})
    notes.pop(str(rep["message_id"]), None)
    nref.update({"notes": notes, "note_ids": [i for i in data.get("note_ids") or [] if i != rep["message_id"]]})
    tg("setMessageReaction", chat_id=chat_id, message_id=rep["message_id"], reaction=[])
    send(chat_id, "🗑 Not takvimden silindi.", mid)


def handle_reaction(r):
    """Any reaction from someone other than the author counts as having listened."""
    user = r.get("user") or {}
    chat_id = str((r.get("chat") or {}).get("id"))
    if not user or user.get("is_bot") or not r.get("new_reaction") or chat_id != get_chat_id():
        return
    add_listener(chat_id, r["message_id"], user["id"])


def add_listener(chat_id, message_id, user_id):
    ref = db.collection("recordings").document(f"{chat_id}_{message_id}")
    snap = ref.get()
    uid = str(user_id)
    if snap.exists and field(snap, "user_id") != uid:
        ref.update({"listeners": firestore.ArrayUnion([uid])})


def find_media(msg):
    # GIFs and stickers also arrive with a video document attached; they are not practice
    if msg.get("animation") or msg.get("sticker"):
        return None, None
    for k in ("voice", "audio", "video_note", "video"):
        if msg.get(k):
            return msg[k], k
    doc = msg.get("document")
    if doc:
        mime = doc.get("mime_type") or ""
        name = (doc.get("file_name") or "").lower()
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        if mime.startswith(("audio/", "video/")) or ext in MEDIA_EXTS:
            return doc, "document"
    return None, None


def media_type(media, kind):
    fname = (media.get("file_name") or "").lower()
    ext = {"voice": "ogg", "video_note": "mp4"}.get(kind)
    if not ext:
        ext = fname.rsplit(".", 1)[-1] if "." in fname else ""
    mime = media.get("mime_type") or ""
    if not mime or mime == "application/octet-stream":
        mime = mimetypes.guess_type(f"x.{ext}")[0] or ("video/mp4" if kind == "video" else "audio/mpeg")
    if not ext:
        ext = (mimetypes.guess_extension(mime) or ".bin").lstrip(".")
    return mime, re.sub(r"[^a-z0-9]", "", ext)[:5] or "bin"


def has_left(uid):
    """True for a member who left with /ayril; new people are not members yet."""
    snap = db.collection("members").document(str(uid)).get()
    return snap.exists and field(snap, "active") is False


def chat_mode(uid):
    """True while the member has switched off recording with /sohbet."""
    snap = db.collection("members").document(str(uid)).get()
    until = field(snap, "chat_until") if snap.exists else None
    return bool(until) and until > dt.datetime.now(TZ)


def next_day_start():
    now = dt.datetime.now(TZ)
    midnight = dt.datetime.combine(practice_day(now) + dt.timedelta(days=1), dt.time(0), TZ)
    return midnight + dt.timedelta(hours=DAY_OFFSET)


def is_resend(msg, user, media):
    """True when the member already sent this exact file on the same practice day."""
    fuid = media.get("file_unique_id")
    if not fuid:
        return False
    day = practice_day(dt.datetime.fromtimestamp(msg["date"], TZ)).isoformat()
    q = (db.collection("recordings")
         .where(filter=FieldFilter("user_id", "==", str(user["id"])))
         .where(filter=FieldFilter("file_uid", "==", fuid)))
    return any(s.to_dict().get("day") == day for s in q.stream())


def save_recording(msg, user, media, kind, force=False):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    if db.collection("recordings").document(f"{chat_id}_{mid}").get().exists:
        return
    if not force and is_resend(msg, user, media):
        log.info("same file sent again by %s, not recorded", user["id"])
        return
    mime, ext = media_type(media, kind)

    if (media.get("file_size") or 0) > MAX_TG_BYTES:
        save_large_recording(msg, user, media, kind, mime, ext, force)
        return

    # Telegram resends the update when analysis outlasts its timeout; only the first copy runs
    lock = db.collection("processing").document(f"{chat_id}_{mid}")
    try:
        lock.create({"started": dt.datetime.now(TZ)})
    except AlreadyExists:
        started = field(lock.get(), "started")
        # A lock left behind by a crashed instance must not block the take forever
        if isinstance(started, dt.datetime) and dt.datetime.now(TZ) - started < dt.timedelta(minutes=10):
            log.info("%s_%s already being processed", chat_id, mid)
            return
        lock.set({"started": dt.datetime.now(TZ)})
    try:
        download_and_store(msg, user, media, kind, mime, ext, force)
    finally:
        lock.delete()


def download_and_store(msg, user, media, kind, mime, ext, force):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    info = tg("getFile", file_id=media["file_id"])
    if not info or not info.get("file_path"):
        send(chat_id, "Bu kaydı Telegram'dan alamadım. Biraz sonra kayda yanıt verip /kaydet yaz.", mid)
        return
    r = None
    for attempt in range(3):
        try:
            r = requests.get(f"{FILE_API}/{info['file_path']}", timeout=60)
            r.raise_for_status()
            break
        except Exception:
            log.exception("download attempt %d failed", attempt + 1)
            r = None
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if r is None:
        alert("save", "Bir kayıt indirilemedi, loglara bak.")
        send(chat_id, "Bu kaydı Telegram'dan alamadım. Biraz sonra kayda yanıt verip /kaydet yaz.", mid)
        return
    store_recording(msg, user, media, kind, r.content, mime, ext, force)


def save_large_recording(msg, user, media, kind, mime, ext, force=False):
    """Files above 20 MB come over MTProto in the background, shrunk before storing."""
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    if not bigfiles.available():
        send(chat_id, "Bu dosya 20 MB’tan büyük, Telegram botların indirmesine izin vermiyor. "
                      "Daha kısa ya da daha düşük kaliteli bir kayıt gönder.", mid)
        return
    if (media.get("file_size") or 0) > bigfiles.MAX_BYTES:
        send(chat_id, "Bu dosya 1 GB’tan büyük, kaydedemiyorum. Daha kısa bir kayıt gönder.", mid)
        return
    # Telegram may redeliver the update while the download runs
    lock = db.collection("processing").document(f"{chat_id}_{mid}")
    if lock.get().exists:
        return
    lock.set({"started": dt.datetime.now(TZ)})
    tg("setMessageReaction", chat_id=chat_id, message_id=mid, reaction=[{"type": "emoji", "emoji": "👀"}])
    run_in_background(process_large_recording, msg, user, media, kind, mime, ext, force)


def run_in_background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def process_large_recording(msg, user, media, kind, mime, ext, force=False):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, f"in.{ext}")
        downloader().download(chat_id, mid, src)
        small = bigfiles.compress(src, mime)
        if small:
            path, mime, ext = small
        else:
            path = src
        with open(path, "rb") as f:
            content = f.read()
        if not store_recording(msg, user, media, kind, content, mime, ext, force):
            tg("setMessageReaction", chat_id=chat_id, message_id=mid, reaction=[])
    except Exception:
        log.exception("large download failed")
        alert("large", "Büyük bir kayıt indirilemedi, loglara bak.")
        tg("setMessageReaction", chat_id=chat_id, message_id=mid, reaction=[])
        send(chat_id, "Bu büyük kaydı kaydedemedim. Biraz sonra tekrar gönder.", mid)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        db.collection("processing").document(f"{chat_id}_{mid}").delete()


_downloader = None


def downloader():
    global _downloader
    if _downloader is None:
        _downloader = bigfiles.Downloader(
            BOT_TOKEN,
            load_session=lambda: field(state_ref().get(), "mtproto_session"),
            save_session=lambda s: state_ref().set({"mtproto_session": s}, merge=True))
    return _downloader


def store_recording(msg, user, media, kind, content, mime, ext, force=False):
    """Returns False when the take holds no music and is left out of the practice log."""
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    ref = db.collection("recordings").document(f"{chat_id}_{mid}")
    # Voice messages that are only talk are chat between members, not practice
    if not force and not music.has_music(content):
        log.info("no music in %s_%s, not recorded", chat_id, mid)
        return False
    upsert_member(user)
    try:
        ts = dt.datetime.fromtimestamp(msg["date"], TZ)
        day = practice_day(ts)
        path = f"recordings/{day:%Y/%m/%d}/{user['id']}_{mid}.{ext}"
        bucket.blob(path).upload_from_string(content, content_type=mime)
        try:
            has_metro, bpm, decoded_sec, tempo = metronome.analyze(content)
        except Exception:
            log.exception("metronome detection failed")
            has_metro, bpm, decoded_sec, tempo = False, None, 0, None
        ref.set({
            "user_id": str(user["id"]),
            "name": display_name(user),
            "day": day.isoformat(),
            "ts": ts,
            "duration": int(media.get("duration") or decoded_sec or 0),
            "size": len(content),
            "gcs_path": path,
            "mime": mime,
            "kind": kind,
            "caption": msg.get("caption") or "",
            "file_name": media.get("file_name") or "",
            "file_uid": media.get("file_unique_id") or "",
            "metronome": has_metro,
            "bpm": bpm,
            "tempo": tempo,
        })
    except Exception:
        log.exception("save failed")
        alert("save", "Bir kayıt kaydedilemedi, loglara bak.")
        send(chat_id, "Bu kayıt kaydedilemedi. Lütfen tekrar gönder.", mid)
        return True
    tg("setMessageReaction", chat_id=chat_id, message_id=mid,
       reaction=[{"type": "emoji", "emoji": "🔥" if has_metro else "❤"}])
    badge = time_badge(ts)
    if badge:
        # A message with a single emoji shows up large and animated
        send(chat_id, badge, mid)
    try:
        check_milestones(chat_id, user)
    except Exception:
        log.exception("milestone check failed")
    return True


NIGHT_EMOJI = ["🦉", "🌙", "🌚", "🌌", "⭐", "🌃"]
MORNING_EMOJI = ["🌅", "☀️", "🐓", "☕", "🌄", "🌞"]


def time_badge(ts):
    """Secret emoji for takes sent late at night or in the morning."""
    hour = ts.astimezone(TZ).hour
    if hour < 5:
        return secrets.choice(NIGHT_EMOJI)
    if hour < 10:
        return secrets.choice(MORNING_EMOJI)
    return None


def check_milestones(chat_id, user):
    uid = str(user["id"])
    ref = db.collection("members").document(uid)
    snap = ref.get()
    if not snap.exists:
        return
    member = snap.to_dict()
    st = load_stats().get(uid, empty_stats())
    hist = history(member, st, today())
    cur, _ = chain_stats(hist)
    reached = set(member.get("milestones") or [])
    new_keys, lines, emoji, cel_key = [], [], "🎉", None
    start = current_chain_start(hist)
    cel = load_celebrations()
    if cur in cel["streaks"] and start:
        key = f"streak{cur}:{start.isoformat()}"
        if key not in reached:
            new_keys.append(key)
            lines.append(f"🎉 {mention(uid, member.get('name', ''))} {cur} günlük seriye ulaştı!")
            cel_key = f"streak{cur}"
            if cur >= 100:
                emoji = "🏆"
    if st["count"] in cel["counts"]:
        key = f"count{st['count']}"
        if key not in reached:
            new_keys.append(key)
            nth = "ilk" if st["count"] == 1 else f"{st['count']}."
            lines.append(f"🎉 {mention(uid, member.get('name', ''))} {nth} kaydını attı!")
            if cel_key is None:
                cel_key, emoji = f"count{st['count']}", "🎊"
    if new_keys:
        ref.set({"milestones": sorted(reached | set(new_keys))}, merge=True)
        send(chat_id, "\n".join(lines))
        send_celebration(chat_id, cel_key, emoji)


def handle_command(cmd, msg, user):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    t = today()

    if cmd in ("/start", "/yardim", "/help"):
        send(chat_id,
             "Her gün pratikten kısa bir sesli mesaj, video ya da ses dosyası at, ben kaydedip ❤ koyarım.\n"
             "Metronomla çalışırsan (hoparlörden, kayıtta duyulacak şekilde) 🔥 alırsın.\n"
             "Haftada 1 gün atlama hakkın var (🃏 joker), seri bozulmaz.\n"
             "Not eklemek için mesaja açıklama yaz ya da kendi kaydına yanıt ver.\n"
             "İçinde müzik olmayan sesli mesajlar (sadece konuşma) pratik sayılmaz.\n\n"
             "/bugun – bugün kim kaydetti\n"
             "/seri – son 20 gün (🔥 metronomlu, ❤ kaydetti, 🃏 joker, 💔 atladı)\n"
             "/detay – herkesin istatistikleri\n"
             "/takvim – tüm kayıtların takvimi\n"
             "/katil – kayıt atmadan gruba katıl\n"
             "/ayril – hatırlatmalardan çık\n"
             "/sil – kendi kaydına yanıt olarak yaz, kayıt silinir\n"
             "/kaydet – bot pratiğini konuşma sanıp kaydetmediyse kayda yanıt olarak yaz\n"
             "/sohbet – bundan sonra attıkların takvime eklenmez, /pratik ile biter\n"
             "/sarkilarim – bitmeyen şarkıların (şarkı adı dosya adından ya da notun ilk satırından, "
             "nereye kadar çaldığın nottan; notta \"bitti\", \"tamamı\", \"full\" gibi bir şey yazınca biter)\n"
             "/bitti – şarkının kaydına yanıt olarak yaz, listeden çıkar\n"
             "/metronomvar, /metronomyok – bot metronomu yanlış algıladıysa kayda yanıt olarak yaz (/metronomvar 120 ile BPM de girebilirsin)\n"
             "/notsil – bir notuna yanıt olarak yaz, o not silinir; kaydın kendisine yanıt olarak yazarsan tüm notları silinir\n"
             "/atesle, /alkis – yanıt verdiğin mesaja 🔥 ya da 👏 bırakır\n"
             "/zar – zar atar · /ilham – rastgele bir pratik fikri\n"
             "/prova – 7 günlük prova anketi açar (17:30'dan önce bugün de dahil), /prova bitir en çok seçilen günü duyurur\n"
             "/cikar – pratik olmayan bir kayda yanıt olarak yaz, mesaj grupta kalır ama pratik sayılmaz\n"
             "/yenilink – takvim linki grup dışına çıktıysa yenisini oluştur", mid)
    elif cmd == "/katil":
        was_active = field(db.collection("members").document(str(user["id"])).get(), "active")
        upsert_member(user)
        send(chat_id, f"{mention(user['id'], display_name(user))} katıldı.", mid)
        if not was_active:
            send_celebration(chat_id, "join", "🎉")
    elif cmd == "/ayril":
        was_active = field(db.collection("members").document(str(user["id"])).get(), "active")
        deactivate_member(user["id"])
        send(chat_id, "Hatırlatmalardan çıkarıldın. Tekrar katılmak için /katil yaz.", mid)
        if was_active:
            send_celebration(chat_id, "leave", "👋")
    elif cmd == "/bugun":
        members = [m for m in load_members() if m.get("active")]
        done = load_days(t)
        lines = [f"<b>{tr_date(t)}</b>"]
        for m in members:
            ok = t.isoformat() in done.get(m["id"], set())
            lines.append(f"{'✅' if ok else '⏳'} {html.escape(m['name'])}")
        send(chat_id, "\n".join(lines) if members else "Henüz kimse yok.", mid)
    elif cmd == "/seri":
        members = [m for m in load_members() if m.get("active")]
        stats = load_stats()
        rows = []
        for m in members:
            hist = history(m, stats.get(m["id"], empty_stats()), t)
            cur, _ = chain_stats(hist)
            marks = "".join(MARK[s] for _, s in hist[-HISTORY_DAYS:])
            rows.append((cur, m["name"], marks or "–"))
        rows.sort(key=lambda r: (-r[0], r[1]))
        lines = []
        for _, name, marks in rows:
            lines += [f"<b>{html.escape(name)}</b>", marks, ""]
        send(chat_id, "\n".join(lines).strip() if rows else "Henüz kayıt yok.", mid)
    elif cmd == "/detay":
        members = [m for m in load_members() if m.get("active")]
        stats = load_stats()
        month_name = TR_MONTHS[t.month - 1]
        month_first = t.replace(day=1)
        rows = []
        for m in members:
            st = stats.get(m["id"], empty_stats())
            hist = history(m, st, t)
            cur, longest = chain_stats(hist)
            start = max(month_first, dt.date.fromisoformat(m.get("first_day") or t.isoformat()))
            if st["days"]:
                start = max(month_first, min(start, dt.date.fromisoformat(min(st["days"]))))
            span = (t - start).days + 1
            month_done = sum(1 for d in st["days"] if month_first.isoformat() <= d <= t.isoformat())
            month_metro = sum(1 for d in st["metro"] if month_first.isoformat() <= d <= t.isoformat())
            joker = "kullanıldı" if joker_used_this_week(hist, t) else "duruyor"
            rows.append((cur, m["name"], [
                f"🏆 En uzun seri: {longest} gün · şu an {cur} gün",
                f"🃏 Bu haftanın jokeri: {joker}",
                f"📅 {month_name}: {month_done}/{span} gün",
                f"🔥 Metronomlu: {month_metro} gün ({month_name}) · {len(st['metro'])} gün (toplam)",
                f"⏱ Toplam kayıt: {html.escape(fmt_total(st['sec']))} · {st['count']} kayıt",
            ]))
        rows.sort(key=lambda r: (-r[0], r[1]))
        lines = ["📊 <b>Detay</b>"]
        for _, name, detail in rows:
            lines += ["", f"<b>{html.escape(name)}</b>"] + detail
        send(chat_id, "\n".join(lines) if rows else "Henüz kayıt yok.", mid)
    elif cmd == "/takvim":
        if PUBLIC_URL:
            send(chat_id, f"Takvim: {PUBLIC_URL}/?t={web_token()}", mid)
        else:
            send(chat_id, "Takvim adresi henüz ayarlanmadı.", mid)
    elif cmd == "/yenilink":
        new = rotate_web_token()
        send(chat_id, "🔑 Takvim linki yenilendi, eski link artık çalışmıyor.\n"
                      f"Yeni link: {PUBLIC_URL}/?t={new}", mid)
    elif cmd == "/sil":
        delete_recording(msg, user)
    elif cmd in ("/atesle", "/alkis"):
        rep = msg.get("reply_to_message")
        if rep:
            tg("setMessageReaction", chat_id=chat_id, message_id=rep["message_id"],
               reaction=[{"type": "emoji", "emoji": "🔥" if cmd == "/atesle" else "👏"}])
            tg("deleteMessage", chat_id=chat_id, message_id=mid)
    elif cmd == "/notsil":
        delete_note(msg, user)
    elif cmd == "/sarkilarim":
        songs = open_songs(user["id"])
        if not songs:
            send(chat_id, "🎵 Bitmeyen şarkın yok.", mid)
        else:
            lines = ["🎵 <b>Bitmeyen şarkıların</b>"]
            for sg in songs[:25]:
                d = sg["last"].astimezone(TZ)
                note = f" — {html.escape(sg['progress'].splitlines()[0][:80])}" if sg["progress"] else ""
                lines.append(f"• {html.escape(sg['title'])}{note} ({d.day} {TR_MONTHS[d.month - 1][:3]})")
            send(chat_id, "\n".join(lines), mid)
    elif cmd == "/bitti":
        mark_song_done(msg, user)
    elif cmd in ("/metronomvar", "/metronomyok"):
        set_metronome(msg, user, cmd == "/metronomvar", (msg.get("text") or "").split()[1:])
    elif cmd == "/prova":
        rehearsal_command(msg, (msg.get("text") or "").split()[1:])
    elif cmd == "/provabitir":
        rehearsal_command(msg, ["bitir"])
    elif cmd == "/zar":
        tg("sendDice", chat_id=chat_id, emoji="🎲")
    elif cmd == "/ilham":
        send(chat_id, f"💡 {secrets.choice(IDEAS)}", mid)
    elif cmd == "/kaydet":
        force_save(msg, user)
    elif cmd == "/sohbet" and chat_mode(user["id"]):
        send(chat_id, "🔇 Zaten sohbet modundasın. Bitirmek için /pratik yaz.", mid)
    elif cmd == "/pratik" and not chat_mode(user["id"]):
        send(chat_id, "🎵 Zaten pratik modundasın.", mid)
    elif cmd == "/sohbet":
        upsert_member(user, activate=False)
        db.collection("members").document(str(user["id"])).set({"chat_until": next_day_start()}, merge=True)
        send(chat_id, "🔇 Sohbet modu: bundan sonra attığın ses ve videolar takvime eklenmeyecek.\n"
                      f"Bitirmek için /pratik yaz. Unutursan saat {DAY_START_HOUR:02d}:00'te kendiliğinden biter.", mid)
    elif cmd == "/pratik":
        ref = db.collection("members").document(str(user["id"]))
        if ref.get().exists:
            ref.set({"chat_until": None}, merge=True)
        send(chat_id, "🎵 Sohbet modu bitti, attıkların yine takvime eklenecek.", mid)
    elif cmd == "/cikar":
        delete_recording(msg, user, keep_message=True)


def set_metronome(msg, user, has_metro, args):
    """Manual override when metronome detection got a take wrong."""
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    rep = msg.get("reply_to_message")
    ref = db.collection("recordings").document(f'{chat_id}_{rep["message_id"]}') if rep else None
    snap = ref.get() if ref else None
    if not snap or not snap.exists:
        send(chat_id, "Düzeltmek istediğin kayda yanıt olarak yaz. BPM de ekleyebilirsin: /metronomvar 120", mid)
        return
    r = snap.to_dict()
    if str(r.get("user_id")) != str(user["id"]) and str(get_admin_id() or "") != str(user["id"]):
        send(chat_id, "Sadece kendi kaydını düzeltebilirsin.", mid)
        return
    bpm = int(args[0]) if args and args[0].isdigit() and 20 <= int(args[0]) <= 400 else None
    if has_metro:
        bpm = bpm or r.get("bpm") or r.get("tempo")
        ref.update({"metronome": True, "bpm": bpm})
        text = f"🔥 Metronomlu olarak işaretlendi{f' ({bpm} bpm)' if bpm else ''}."
    else:
        ref.update({"metronome": False, "bpm": None})
        text = "❤ Metronomsuz olarak işaretlendi."
    tg("setMessageReaction", chat_id=chat_id, message_id=rep["message_id"],
       reaction=[{"type": "emoji", "emoji": "🔥" if has_metro else "❤"}])
    send(chat_id, text, mid)


COVER_VOTE_URL = os.environ.get("COVER_VOTE_URL", "https://cover-vote-five.vercel.app").rstrip("/")
COVER_VOTE_CODE = os.environ.get("COVER_VOTE_CODE", "")


def top_cover_song():
    """Highest-scoring song in the cover vote app, ranked the same way the app does, or None."""
    if not COVER_VOTE_URL:
        return None
    try:
        r = requests.get(f"{COVER_VOTE_URL}/api/songs", headers={"x-room-code": COVER_VOTE_CODE}, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        log.exception("cover vote lookup failed")
        return None
    songs = [s for s in data.get("songs") or [] if isinstance(s, dict) and s.get("id")]
    tally = {s["id"]: [0, 0] for s in songs}
    for key, value in (data.get("votes") or {}).items():
        sid = key.split("|", 1)[0]
        if sid in tally:
            v = float(value)
            # A neutral (0) vote counts as voted but does not move the score
            tally[sid][0] += (v > 0) - (v < 0)
            tally[sid][1] += v > 0
    if not songs:
        return None
    best = max(songs, key=lambda s: (tally[s["id"]][0], tally[s["id"]][1], -(s.get("createdAt") or 0)))
    if tally[best["id"]][0] <= 0:
        return None
    return {"title": best.get("title", ""), "artist": best.get("artist", ""), "score": tally[best["id"]][0]}


REHEARSAL_DAYS = 7
REHEARSAL_TODAY_UNTIL = dt.time(17, 30)


def handle_poll_answer(ans):
    poll = field(state_ref().get(), "rehearsal_poll")
    user = ans.get("user") or {}
    if not poll or ans.get("poll_id") != poll.get("poll_id") or not user:
        return
    voters = set(poll.get("voters") or [])
    uid = str(user["id"])
    # An empty answer means the vote was retracted
    voters = voters | {uid} if ans.get("option_ids") else voters - {uid}
    poll["voters"] = sorted(voters)
    state_ref().set({"rehearsal_poll": poll}, merge=True)


def message_link(chat_id, message_id):
    cid = str(chat_id)
    return f"https://t.me/c/{cid[4:]}/{message_id}" if cid.startswith("-100") else None


def rehearsal_command(msg, args):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    poll = field(state_ref().get(), "rehearsal_poll")
    if args and _fold(args[0]) == "bitir":
        if not poll:
            send(chat_id, "Açık bir prova anketi yok. Başlatmak için /prova yaz.", mid)
            return
        state_ref().set({"rehearsal_poll": None}, merge=True)
        result = tg("stopPoll", chat_id=chat_id, message_id=poll["message_id"])
        tg("unpinChatMessage", chat_id=chat_id, message_id=poll["message_id"])
        votes = [o.get("voter_count", 0) for o in (result or {}).get("options", [])]
        if not votes or max(votes) == 0:
            send(chat_id, "🎸 Prova anketi kapandı, kimse gün seçmedi.", mid)
            return
        best = votes.index(max(votes))
        day = dt.date.fromisoformat(poll["days"][best])
        days = set(field(state_ref().get(), "rehearsals") or []) | {day.isoformat()}
        update = {"rehearsals": sorted(days)}
        lines = [f"🎸 Prova günü: <b>{tr_date(day)}</b> ({max(votes)} kişi uygun)"]
        song = top_cover_song()
        if song:
            name = " – ".join(x for x in (song["title"], song["artist"]) if x)
            lines.append(f"🎵 Çalınacak: <b>{html.escape(name)}</b> ({song['score']} oy)")
            update["rehearsal_songs"] = {**(field(state_ref().get(), "rehearsal_songs") or {}), day.isoformat(): name}
        state_ref().set(update, merge=True)
        send(chat_id, "\n".join(lines))
        return
    if poll:
        send(chat_id, "Zaten açık bir prova anketi var. Sonucu görmek için /prova bitir yaz.",
             poll["message_id"])
        return
    now = now_local()
    # Today is still an option until late afternoon
    first = 0 if now.time() < REHEARSAL_TODAY_UNTIL else 1
    days = [now.date() + dt.timedelta(days=i) for i in range(first, first + REHEARSAL_DAYS)]
    sent = tg("sendPoll", chat_id=chat_id, question="🎸 Prova için hangi günler uygun?",
              options=[{"text": f"{TR_DAYS[d.weekday()]}, {d.day} {TR_MONTHS[d.month - 1]}"} for d in days],
              is_anonymous=False, allows_multiple_answers=True)
    if sent:
        tg("pinChatMessage", chat_id=chat_id, message_id=sent["message_id"], disable_notification=True)
        state_ref().set({"rehearsal_poll": {"message_id": sent["message_id"],
                                            "poll_id": (sent.get("poll") or {}).get("id"),
                                            "voters": [], "days": [d.isoformat() for d in days]}},
                        merge=True)


def mark_song_done(msg, user):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    rep = msg.get("reply_to_message")
    ref = db.collection("recordings").document(f'{chat_id}_{rep["message_id"]}') if rep else None
    snap = ref.get() if ref else None
    if not snap or not snap.exists:
        send(chat_id, "Bitirdiğin şarkının kaydına yanıt olarak /bitti yaz.", mid)
        return
    r = snap.to_dict()
    if str(r.get("user_id")) != str(user["id"]):
        send(chat_id, "Sadece kendi şarkını bitti olarak işaretleyebilirsin.", mid)
        return
    info = song_of(r)
    if not info:
        send(chat_id, "Bu kaydın şarkı adı yok. Dosya adına ya da notun ilk satırına şarkının adını yaz.", mid)
        return
    ref.update({"song_done": True})
    send(chat_id, f"✅ {html.escape(info[0])} bitti, listenden çıktı.", mid)


def force_save(msg, user):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    rep = msg.get("reply_to_message") or {}
    media, kind = find_media(rep)
    if not media:
        send(chat_id, "Kaydetmek istediğin ses ya da videoya yanıt olarak /kaydet yaz.", mid)
        return
    owner = rep.get("from") or {}
    admin = get_admin_id()
    if str(owner.get("id")) != str(user["id"]) and not (admin and str(admin) == str(user["id"])):
        send(chat_id, "Sadece kendi kaydını kaydedebilirsin.", mid)
        return
    if db.collection("recordings").document(f"{chat_id}_{rep['message_id']}").get().exists:
        send(chat_id, "Bu kayıt zaten kaydedilmiş.", mid)
        return
    if has_left(owner.get("id")):
        send(chat_id, "Önce /katil yazmalısın, sonra kaydı ekleyebilirsin.", mid)
        return
    save_recording(rep, owner, media, kind, force=True)


def delete_recording(msg, user, keep_message=False):
    """/sil removes the take everywhere; /cikar only drops it from the practice log."""
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    rep = msg.get("reply_to_message")
    if not rep:
        send(chat_id, f"Bu komutu kayda yanıt olarak yaz: {'/cikar' if keep_message else '/sil'}", mid)
        return
    ref = db.collection("recordings").document(f'{chat_id}_{rep["message_id"]}')
    snap = ref.get()
    if not snap.exists:
        send(chat_id, "Bu mesaj kayıtlı bir pratik kaydı değil.", mid)
        return
    r = snap.to_dict()
    is_admin = str(get_admin_id() or "") == str(user["id"])
    if str(r.get("user_id")) != str(user["id"]) and not is_admin:
        send(chat_id, f"Sadece kendi kayıtlarını {'çıkarabilirsin' if keep_message else 'silebilirsin'}.", mid)
        return
    try:
        blob = bucket.get_blob(r["gcs_path"])
        if blob is not None:
            blob.delete()
        ref.delete()
    except Exception:
        log.exception("delete failed")
        send(chat_id, "Kayıt silinemedi. Biraz sonra tekrar dene.", mid)
        return
    if keep_message:
        tg("setMessageReaction", chat_id=chat_id, message_id=rep["message_id"], reaction=[])
        send(chat_id, "↩️ Pratikten çıkarıldı, mesaj grupta duruyor.", mid)
        return
    if tg("deleteMessage", chat_id=chat_id, message_id=rep["message_id"]):
        tg("deleteMessage", chat_id=chat_id, message_id=mid)
        return
    # Bots cannot delete messages older than 48 hours or without the delete permission
    tg("setMessageReaction", chat_id=chat_id, message_id=rep["message_id"], reaction=[])
    send(chat_id, "🗑 Kayıt takvimden silindi, ama Telegram'daki mesajı silemedim "
                  "(48 saatten eski olabilir). Mesajı elle silebilirsin.", mid)

def milestone_labels(cel):
    return [(f"streak{n}", f"{n} gün") for n in sorted(cel["streaks"])] + \
           [(f"count{n}", "İlk kayıt" if n == 1 else f"{n}. kayıt") for n in sorted(cel["counts"])] + \
           [("join", "Katılma (/katil)"), ("leave", "Ayrılma (/ayril)")]


def celebrations_ref():
    return db.collection("config").document("celebrations")


def load_celebrations():
    snap = celebrations_ref().get()
    data = snap.to_dict() if snap.exists else {}
    streaks, counts = data.get("streaks"), data.get("counts")
    return {"assigned": dict(data.get("assigned") or {}), "pending": data.get("pending"),
            "streaks": set(STREAK_MILESTONES if streaks is None else streaks),
            "counts": set(COUNT_MILESTONES if counts is None else counts)}


def celebration_media(msg):
    for kind in ("sticker", "animation"):
        item = msg.get(kind)
        if item:
            return {"type": kind, "file_id": item["file_id"], "uid": item["file_unique_id"]}
    return None


def send_media(chat_id, item):
    method = "sendSticker" if item["type"] == "sticker" else "sendAnimation"
    return tg(method, chat_id=chat_id, **{item["type"]: item["file_id"]})


def send_celebration(chat_id, key, emoji):
    item = load_celebrations()["assigned"].get(key)
    if item and send_media(chat_id, item):
        return
    # A message with a single emoji shows up large and animated
    tg("sendMessage", chat_id=chat_id, text=emoji)


SEND_GAP_SEC = 1.0  # stays under Telegram's per-chat rate limit


def list_celebrations(chat_id, cel):
    assigned, labels = cel["assigned"], milestone_labels(cel)
    lines = ["Kutlamalar:"]
    for key, label in labels:
        item = assigned.get(key)
        lines.append(f"{label}: {'sticker' if item and item['type'] == 'sticker' else 'GIF' if item else '– büyük emoji'}")
    lines.append("\nAyarlananlar aşağıda. Kaldırmak istediğine yanıt verip /sil yaz.")
    lines.append("Kutlama eklemek ya da çıkarmak için: /ekle 40 gün, /cikar 100 kayıt")
    tg("sendMessage", chat_id=chat_id, text="\n".join(lines))
    for key, label in labels:
        item = assigned.get(key)
        if not item:
            continue
        time.sleep(SEND_GAP_SEC)
        if item["type"] == "sticker":
            # Stickers cannot carry a caption
            tg("sendMessage", chat_id=chat_id, text=f"{label}:")
            time.sleep(SEND_GAP_SEC)
            send_media(chat_id, item)
        else:
            tg("sendAnimation", chat_id=chat_id, animation=item["file_id"], caption=label)


def milestone_keyboard(cel):
    labels = [label for _, label in milestone_labels(cel)]
    rows = [labels[i:i + 3] for i in range(0, len(labels), 3)] + [["İptal"]]
    return {"keyboard": [[{"text": t} for t in row] for row in rows],
            "one_time_keyboard": True, "resize_keyboard": True}


def handle_private(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from") or {}
    raw = (msg.get("text") or "").strip()
    text = raw.lower()
    cmd = text.split()[0].split("@")[0] if text else ""
    cmd = cmd.replace("ö", "o").replace("ı", "i").replace("ş", "s").replace("ç", "c")
    admin = get_admin_id()
    is_admin = bool(admin) and str(admin) == str(user.get("id"))

    if cmd == "/yonetici":
        if is_admin:
            send(chat_id, "Zaten yöneticisin, hata uyarıları sana geliyor.")
        elif admin:
            send(chat_id, "Yönetici zaten ayarlı.")
        elif not db.collection("members").document(str(user.get("id"))).get().exists:
            send(chat_id, "Önce grupta kayıt at ya da /katil yaz.")
        else:
            state_ref().set({"admin_id": str(user["id"])}, merge=True)
            send(chat_id, "Tamam, botta bir sorun olursa sana buradan haber vereceğim.\n\n"
                          "Kutlamalarda gidecek sticker ya da GIF'leri de bana buradan atabilirsin.")
        return

    cel = load_celebrations()
    labels = milestone_labels(cel)
    label_to_key = {label: key for key, label in labels}
    media = celebration_media(msg)
    cel_command = cmd in ("/kutlamalar", "/sil", "/ekle", "/cikar")
    if not (media or cel_command or raw in label_to_key or raw == "İptal"):
        send(chat_id, "Bu bot sadece pratik grubunda çalışıyor.")
        return
    if not is_admin:
        send(chat_id, "Kutlamaları sadece yönetici değiştirebilir.")
        return

    current = dict(labels)
    used_by = {v["uid"]: current[k] for k, v in cel["assigned"].items() if k in current}
    if media:
        if media["uid"] in used_by:
            send(chat_id, f"Bu zaten {used_by[media['uid']]} kutlamasında kullanılıyor. "
                          "Taşımak istersen önce oradan /sil ile kaldır.")
            return
        celebrations_ref().set({"pending": media}, merge=True)
        tg("sendMessage", chat_id=chat_id, text="Bu hangi kutlama için?", reply_markup=milestone_keyboard(cel))
    elif raw == "İptal":
        celebrations_ref().set({"pending": None}, merge=True)
        tg("sendMessage", chat_id=chat_id, text="İptal edildi.", reply_markup={"remove_keyboard": True})
    elif raw in label_to_key:
        if not cel["pending"]:
            tg("sendMessage", chat_id=chat_id, text="Önce bir sticker ya da GIF at.",
               reply_markup={"remove_keyboard": True})
            return
        other = used_by.get(cel["pending"]["uid"])
        if other and other != raw:
            tg("sendMessage", chat_id=chat_id, text=f"Bu zaten {other} kutlamasında kullanılıyor.",
               reply_markup={"remove_keyboard": True})
            return
        cel["assigned"][label_to_key[raw]] = cel["pending"]
        celebrations_ref().set({"pending": None}, merge=True)
        celebrations_ref().update({"assigned": cel["assigned"]})
        tg("sendMessage", chat_id=chat_id, text=f"✅ {raw} kutlaması için ayarlandı.",
           reply_markup={"remove_keyboard": True})
    elif cmd == "/kutlamalar":
        # In the background so the webhook answers at once and Telegram does not resend the command
        run_in_background(list_celebrations, chat_id, cel)
    elif cmd in ("/ekle", "/cikar"):
        change_milestone(chat_id, cmd == "/ekle", text.split()[1:], cel)
    else:
        rep = msg.get("reply_to_message") or {}
        target = celebration_media(rep)
        if not target:
            send(chat_id, "Kaldırmak istediğin sticker ya da GIF'e yanıt verip /sil yaz.")
            return
        # The listed GIFs carry their milestone as caption; only that one is removed
        only = label_to_key.get((rep.get("caption") or "").strip())
        keep = {k: v for k, v in cel["assigned"].items()
                if v["uid"] != target["uid"] or (only and k != only)}
        if len(keep) < len(cel["assigned"]):
            # update replaces the map; set(merge=True) would merge it and keep the removed keys
            celebrations_ref().update({"assigned": keep})
        removed = [label for k, label in labels if k in cel["assigned"] and k not in keep]
        send(chat_id, f"🗑 Kaldırıldı: {', '.join(removed)}. Bu kutlamalarda büyük emoji gidecek."
             if removed else "Bu hiçbir kutlamaya atanmamış.")


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    log.exception("request failed")
    alert(f"http:{request.path}", f"{request.method} {request.path} hatası: {e!r}")
    return Response("Bir hata oluştu.", status=500, mimetype="text/plain; charset=utf-8")


@app.post("/telegram")
def telegram_webhook():
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(got, WEBHOOK_SECRET):
        abort(403)
    try:
        handle_update(request.get_json(silent=True) or {})
    except Exception:
        log.exception("update failed")
        alert("update", "Bir Telegram mesajı işlenirken hata oluştu, loglara bak.")
    return "ok"


@app.post("/cron/reminder")
def cron_reminder():
    if not hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), CRON_SECRET):
        abort(403)
    chat_id = get_chat_id()
    if not chat_id:
        return "no chat"
    t = today()
    members = [m for m in load_members() if m.get("active")]
    if not members:
        return "no members"
    stats = load_stats()
    missing = [m for m in members if t.isoformat() not in stats.get(m["id"], empty_stats())["days"]]
    poll_lines = rehearsal_reminder(chat_id, members)
    if not missing:
        send(chat_id, "\n".join(["Bugün herkes kaydetti ❤"] + poll_lines))
        return "all done"
    lines = ["⏰ <b>Bugünün kaydı bekleniyor</b>"]
    for m in missing:
        hist = history(m, stats.get(m["id"], empty_stats()), t)
        cur, _ = chain_stats(hist)
        if joker_used_this_week(hist, t):
            tail = f" – {cur} günlük seri bozulmasın, bu haftanın jokeri kullanıldı" if cur else ""
        else:
            tail = f" – {cur} günlük seri · 🃏 jokerin var" if cur else ""
        lines.append(f"• {mention(m['id'], m['name'])}{tail}")
    done_n = len(members) - len(missing)
    lines.append(f"\n{done_n}/{len(members)} kişi kaydetti.")
    send(chat_id, "\n".join(lines + poll_lines))
    return "sent"


def rehearsal_reminder(chat_id, members):
    poll = field(state_ref().get(), "rehearsal_poll")
    # Polls opened before votes were tracked have no id, so their voters are unknown
    if not poll or not poll.get("poll_id"):
        return []
    voters = set(poll.get("voters") or [])
    waiting = [m for m in members if m["id"] not in voters]
    if not waiting:
        return []
    link = message_link(chat_id, poll["message_id"])
    head = f'<a href="{link}">Prova anketine</a>' if link else "Prova anketine"
    return ["", f"🎸 {head} oy vermeyenler: " + ", ".join(mention(m["id"], m["name"]) for m in waiting)]


@app.post("/cron/weekly")
def cron_weekly():
    if not hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), CRON_SECRET):
        abort(403)
    chat_id = get_chat_id()
    if not chat_id:
        return "no chat"
    t = today()
    ws = week_start(t)
    we = ws + dt.timedelta(days=6)
    members = [m for m in load_members() if m.get("active")]
    if not members:
        return "no members"
    days, metro = load_days(ws, with_metro=True)
    rows = []
    for m in members:
        d = sum(1 for x in days.get(m["id"], set()) if x <= we.isoformat())
        w = sum(1 for x in metro.get(m["id"], set()) if x <= we.isoformat())
        rows.append((w, d, m))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2]["name"]))

    if ws.month == we.month:
        span = f"{ws.day}–{we.day} {TR_MONTHS[we.month - 1]}"
    else:
        span = f"{tr_date(ws, False)} – {tr_date(we, False)}"
    lines = [f"📊 <b>Haftalık özet</b> · {span}", ""]
    for w, d, m in rows:
        lines.append(f"{html.escape(m['name'])}: 📅 {d}/7 gün · 🔥 {w} metronomlu")
    top = rows[0][0]
    if top:
        names = ", ".join(mention(m["id"], m["name"]) for w, d, m in rows if w == top)
        lines += ["", f"🏆 Bu haftanın metronom ustası: {names} ({top} gün)"]
    else:
        lines += ["", "Bu hafta metronomlu kayıt yok. Gelecek hafta 🔥 toplayalım!"]
    send(chat_id, "\n".join(lines))
    return "sent"


# ---------- web calendar ----------

def authed():
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), web_token())


def require_auth():
    t = request.args.get("t")
    if t and hmac.compare_digest(t, web_token()):
        args = {k: v for k, v in request.args.items() if k != "t"}
        target = request.path + ("?" + "&".join(f"{k}={v}" for k, v in args.items()) if args else "")
        resp = make_response(redirect(target))
        resp.set_cookie(COOKIE, web_token(), max_age=365 * 86400, httponly=True,
                        secure=True, samesite="Lax")
        return resp
    if not authed():
        return Response("Takvim bağlantısını Telegram grubunda /takvim yazarak al.",
                        status=403, mimetype="text/plain; charset=utf-8")
    return None


def member_state(m, day, recorded, t):
    first = m.get("first_day") or "0000-00-00"
    left = m.get("left_day")
    iso = day.isoformat()
    if recorded:
        return "done"
    if iso < first or (left and iso >= left) or day > t:
        return None
    if day == t:
        return "pending"
    return "missed"


@app.get("/")
def calendar_page():
    denied = require_auth()
    if denied:
        return denied
    t = today()
    try:
        y, mo = map(int, request.args.get("m", "").split("-"))
        first = dt.date(y, mo, 1)
    except ValueError:
        first = t.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])

    q = (db.collection("recordings")
         .where(filter=FieldFilter("day", ">=", first.isoformat()))
         .where(filter=FieldFilter("day", "<=", last.isoformat())))
    recs = []
    for s in q.stream():
        r = s.to_dict()
        r["id"] = s.id
        recs.append(r)
    recs.sort(key=lambda r: r["ts"])

    members = load_members()
    by_day = {}
    for r in recs:
        by_day.setdefault(r["day"], []).append(r)
    rec_users = {(r["day"], r["user_id"]) for r in recs}
    metro_users = {(r["day"], r["user_id"]) for r in recs if r.get("metronome")}
    month_users = {r["user_id"] for r in recs}
    shown = [m for m in members if m.get("active") or m["id"] in month_users]
    names = {m["id"]: m["name"] for m in members}
    all_stats = load_stats()
    hists = {m["id"]: history(m, all_stats.get(m["id"], empty_stats()), t) for m in shown}
    jokers = {uid: {d.isoformat() for d, s in h if s == "joker"} for uid, h in hists.items()}

    def states_for(day):
        out = []
        for m in shown:
            key = (day.isoformat(), m["id"])
            st = member_state(m, day, key in rec_users, t)
            if st == "done" and key in metro_users:
                st = "metro"
            elif st == "missed" and day.isoformat() in jokers[m["id"]]:
                st = "joker"
            if st:
                out.append({"name": m["name"], "state": st})
        return out

    rehearsals = set(field(state_ref().get(), "rehearsals") or [])
    rehearsal_songs = field(state_ref().get(), "rehearsal_songs") or {}
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        row = []
        for d in week:
            in_month = d.month == first.month
            sts = states_for(d) if in_month else []
            row.append({
                "date": d, "iso": d.isoformat(), "in_month": in_month, "is_today": d == t,
                "states": sts, "done": sum(1 for s in sts if s["state"] in ("done", "metro")),
                "rehearsal": d.isoformat() in rehearsals,
                "rehearsal_song": rehearsal_songs.get(d.isoformat(), ""),
                "total": len(sts), "has_list": in_month and d <= t and bool(sts),
            })
        weeks.append(row)

    days = []
    d = min(last, t)
    while d >= first:
        sts = states_for(d)
        items = []
        for r in by_day.get(d.isoformat(), []):
            items.append({
                "id": r["id"], "name": names.get(r["user_id"], r.get("name", "")),
                "time": r["ts"].astimezone(TZ).strftime("%H:%M"),
                "duration": fmt_duration(r.get("duration")),
                "caption": note_text(r), "mime": r.get("mime", ""),
                "file_name": r.get("file_name", ""),
                "video": (r.get("mime") or "").startswith("video/") or r.get("kind") in ("video_note", "video"),
                "round": r.get("kind") == "video_note",
                "metronome": bool(r.get("metronome")), "bpm": r.get("bpm"), "tempo": r.get("tempo"),
                "listeners": [names.get(u, "") for u in (r.get("listeners") or [])
                              if u != r["user_id"] and names.get(u)],
            })
        if items or sts:
            days.append({
                "iso": d.isoformat(), "label": tr_date(d), "is_today": d == t, "recs": items,
                "missed": [s["name"] for s in sts if s["state"] == "missed"],
                "jokers": [s["name"] for s in sts if s["state"] == "joker"],
                "pending": [s["name"] for s in sts if s["state"] == "pending"],
            })
        d -= dt.timedelta(days=1)

    all_days = {uid: st["days"] for uid, st in all_stats.items()}
    all_metro = {uid: st["metro"] for uid, st in all_stats.items()}
    summary = []
    for m in shown:
        s = all_days.get(m["id"], set())
        month_done = sum(1 for x in s if first.isoformat() <= x <= last.isoformat())
        missed = 0
        d = first
        while d <= min(last, t - dt.timedelta(days=1)):
            if member_state(m, d, d.isoformat() in s, t) == "missed" and d.isoformat() not in jokers[m["id"]]:
                missed += 1
            d += dt.timedelta(days=1)
        metro_month = sum(1 for x in all_metro.get(m["id"], set()) if first.isoformat() <= x <= last.isoformat())
        cur, _ = chain_stats(hists[m["id"]])
        summary.append({"name": m["name"], "streak": cur, "done": month_done, "missed": missed,
                        "metro": metro_month,
                        "active": m.get("active")})
    summary.sort(key=lambda x: (-x["streak"], x["name"]))

    prev_m = (first - dt.timedelta(days=1)).strftime("%Y-%m")
    next_first = last + dt.timedelta(days=1)
    next_m = next_first.strftime("%Y-%m") if next_first <= t else None

    return render_template_string(
        PAGE, month_label=f"{TR_MONTHS[first.month - 1]} {first.year}", weeks=weeks,
        weekday_labels=TR_DAYS_SHORT, days=days, summary=summary, prev_m=prev_m, next_m=next_m,
        today_label=tr_date(t), rec_count=len(recs), cover_url=COVER_VOTE_URL,
        who=current_who(names), people=[{"id": m["id"], "name": m["name"]} for m in members if m.get("active")])


def current_who(names):
    uid = request.cookies.get(WHO_COOKIE, "")
    return {"id": uid, "name": names[uid]} if uid in names else None


@app.get("/ben/<uid>")
def set_who(uid):
    if not authed():
        abort(403)
    resp = make_response(redirect("/"))
    if db.collection("members").document(uid).get().exists:
        resp.set_cookie(WHO_COOKIE, uid, max_age=365 * 86400, httponly=True, secure=True, samesite="Lax")
    else:
        resp.delete_cookie(WHO_COOKIE)
    return resp


@app.post("/dinle/<doc_id>")
def mark_listened(doc_id):
    if not authed():
        abort(403)
    uid = request.cookies.get(WHO_COOKIE, "")
    if not uid or not db.collection("members").document(uid).get().exists:
        return "", 204
    ref = db.collection("recordings").document(doc_id)
    snap = ref.get()
    if snap.exists and field(snap, "user_id") != uid:
        ref.update({"listeners": firestore.ArrayUnion([uid])})
    return "", 204


@app.get("/audio/<doc_id>")
def audio(doc_id):
    if not authed():
        abort(403)
    snap = db.collection("recordings").document(doc_id).get()
    if not snap.exists:
        abort(404)
    r = snap.to_dict()
    blob = bucket.get_blob(r["gcs_path"])
    if blob is None:
        abort(404)
    size = blob.size
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=86400"}
    if request.args.get("dl"):
        fname = f"{r['day']}_{re.sub(r'[^A-Za-z0-9_-]', '', r.get('name', ''))}_{doc_id}.{r['gcs_path'].rsplit('.', 1)[-1]}"
        headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    m = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("Range", ""))
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:
            start = max(0, size - int(m.group(2)))
            end = size - 1
        end = min(end, size - 1)
        if start > end:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        data = blob.download_as_bytes(start=start, end=end)
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(data, status=206, mimetype=r.get("mime"), headers=headers)
    return Response(blob.download_as_bytes(), mimetype=r.get("mime"), headers=headers)


PAGE = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pratik Zinciri</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{
  --bg:#EEF0F4;--surface:#FFFFFF;--ink:#141820;--muted:#5E6675;--line:#D6DAE2;
  --accent:#2B45D4;--gold:#C28A12;--gold-soft:#F6E7C1;--done:#1F8A5B;--done-soft:#D5EFE2;--miss:#C2412D;--miss-soft:#F7DDD8;--empty:#E3E6EC;
  --f-display:"Bricolage Grotesque",system-ui,sans-serif;--f-body:"IBM Plex Sans",system-ui,sans-serif;
  --f-mono:"IBM Plex Mono",ui-monospace,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0F1218;--surface:#181C25;--ink:#E8EBF1;--muted:#98A0B0;--line:#2A303C;
  --accent:#7C8FFF;--gold:#F0BE4C;--gold-soft:#3A2E12;--done:#4CC98E;--done-soft:#17392A;--miss:#F07F6C;--miss-soft:#3C1E19;--empty:#262B37;color-scheme:dark}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--f-body)}
.wrap{max-width:880px;margin:0 auto;padding-inline:16px;padding-block:24px 56px;display:grid;gap:22px}
h1,h2{font-family:var(--f-display);margin:0;text-wrap:balance}
h1{font-size:clamp(28px,6vw,40px);font-weight:800;letter-spacing:-.02em;line-height:1.05}
h2{font-size:19px;font-weight:700}
.label{font-family:var(--f-mono);font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
a{color:var(--accent)}
header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:12px}
.nav{display:flex;gap:8px;align-items:center}
.nav a,.nav span{font-family:var(--f-mono);font-size:13px;text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:6px 10px;background:var(--surface)}
.nav span{color:var(--muted);opacity:.5}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px;display:grid;gap:12px;min-width:0}
.cal{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}
.wd{font-family:var(--f-mono);font-size:11px;color:var(--muted);text-align:center;padding-bottom:2px}
.cell{display:grid;gap:4px;align-content:start;min-height:64px;padding:6px;border-radius:8px;border:1px solid var(--line);text-decoration:none;color:var(--ink);background:var(--bg)}
.cell.out{visibility:hidden}
.cell.today{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.cell.nolink{pointer-events:none}
.cell .top{display:flex;justify-content:space-between;align-items:baseline;gap:4px}
.cell .num{font-weight:600;font-variant-numeric:tabular-nums}
.cell .cnt{font-family:var(--f-mono);font-size:10.5px;color:var(--muted)}
.cell.all{background:var(--done-soft);border-color:var(--done)}
.dots{display:flex;flex-wrap:wrap;gap:3px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.dot.done{background:var(--done)}
.dot.metro{background:var(--gold);box-shadow:0 0 0 1.5px var(--gold-soft)}
.bpm{font-family:var(--f-mono);font-size:12px;font-weight:500;color:var(--ink);background:var(--gold-soft);border:1px solid var(--gold);border-radius:999px;padding:1px 8px}
.tempo{font-family:var(--f-mono);font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 8px}
.dot.missed{background:transparent;box-shadow:inset 0 0 0 2px var(--miss)}
.dot.joker{background:transparent;box-shadow:inset 0 0 0 2px var(--accent)}
.note.joker{background:var(--bg);border:1px solid var(--accent)}
.dot.pending{background:var(--empty);box-shadow:inset 0 0 0 1.5px var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--muted)}
.legend span{display:inline-flex;gap:6px;align-items:center}
.tablewrap{overflow-x:auto}
table.sum{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
table.sum th,table.sum td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
table.sum th{font-family:var(--f-mono);font-weight:400;font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
table.sum td.n{font-family:var(--f-mono)}
table.sum tr.inactive td{color:var(--muted)}
.miss-n{color:var(--miss)}
.coverlink{font-size:14px;text-decoration:none;color:var(--accent)}
.sumbox summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px}
.sumbox summary::-webkit-details-marker{display:none}
.sumbox summary::after{content:"▸";color:var(--muted);transition:transform .15s}
.sumbox[open] summary::after{transform:rotate(90deg)}
.sumbox summary h2{margin:0}
.sumbox:not([open]){display:block}
.day{display:grid;gap:8px;scroll-margin-top:16px}
.js .day{display:none}
.js .day.show{display:grid}
.cell.sel{background:color-mix(in srgb,var(--accent) 18%,var(--bg));border-color:var(--accent)}
.dayhead{display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;align-items:baseline}
.rec{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;display:grid;gap:8px;min-width:0}
.rec .head{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:baseline}
.rec .who{font-weight:600}
.rec .meta{font-family:var(--f-mono);font-size:12px;color:var(--muted)}
.rec .dl{margin-left:auto;font-size:13px}
.rec .fname{font-family:var(--f-mono);font-size:12.5px;color:var(--muted);overflow-wrap:anywhere}
.rec .cap{white-space:pre-wrap;overflow-wrap:anywhere}
.rec audio,.rec video{width:100%;max-width:100%}
.rec video{border-radius:8px;max-height:70vh;background:#000}
.rec video.round{max-width:240px;border-radius:50%;aspect-ratio:1;object-fit:cover}
.note{border-radius:8px;padding:8px 12px;font-size:14px}
.note.miss{background:var(--miss-soft);border:1px solid var(--miss)}
.note.pend{background:var(--bg);border:1px dashed var(--line);color:var(--muted)}
.whobar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:14px;color:var(--muted)}
.whobar a.pick{font-size:13px;text-decoration:none;border:1px solid var(--line);border-radius:999px;padding:3px 10px;background:var(--surface);color:var(--ink)}
.whobar a.pick:hover{border-color:var(--accent)}
.rec .ears{font-size:13px;color:var(--muted)}
.empty{padding:20px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:10px}
@media (max-width:520px){.cell{min-height:52px;padding:4px}.dot{width:7px;height:7px}.cell .cnt{display:none}}
</style></head><body>
<div class="wrap">
  <header>
    <div><div class="label">Bugün · {{ today_label }}</div><h1>Pratik Zinciri</h1>
      {% if cover_url %}<a class="coverlink" href="{{ cover_url }}" target="_blank" rel="noopener">🎵 Şarkı oylaması</a>{% endif %}</div>
    <nav class="nav">
      <a href="?m={{ prev_m }}">← Önceki</a>
      <strong style="font-family:var(--f-display);font-size:18px;padding:0 6px">{{ month_label }}</strong>
      {% if next_m %}<a href="?m={{ next_m }}">Sonraki →</a>{% else %}<span>Sonraki →</span>{% endif %}
    </nav>
  </header>

  <div class="whobar">
    {% if who %}<span>👋 {{ who.name }} olarak dinliyorsun ·</span><a href="#" id="whochange">değiştir</a>
    {% else %}<span>Kim olduğunu seç, dinlediğin kayıtlarda adın görünsün:</span>
      {% for p in people %}<a class="pick" href="/ben/{{ p.id }}">{{ p.name }}</a>{% endfor %}{% endif %}
  </div>
  {% if who %}<div class="whobar" id="whopick" hidden>
    {% for p in people %}<a class="pick" href="/ben/{{ p.id }}">{{ p.name }}</a>{% endfor %}
  </div>{% endif %}

  <section class="panel">
    <div class="cal">
      {% for w in weekday_labels %}<div class="wd">{{ w }}</div>{% endfor %}
      {% for week in weeks %}{% for c in week %}
        <a class="cell{% if not c.in_month %} out{% endif %}{% if c.is_today %} today{% endif %}{% if not c.has_list %} nolink{% endif %}{% if c.total and c.done == c.total %} all{% endif %}"
           {% if c.has_list %}href="#d-{{ c.iso }}"{% endif %}
           title="{% for s in c.states %}{{ s.name }}: {{ {'done':'kaydetti','metro':'metronomla kaydetti','joker':'joker kullandı','missed':'kaydetmedi','pending':'bekleniyor'}[s.state] }}{% if not loop.last %}&#10;{% endif %}{% endfor %}">
          <span class="top"><span class="num">{{ c.date.day }}{% if c.rehearsal %} <span title="Prova{% if c.rehearsal_song %}: {{ c.rehearsal_song }}{% endif %}">🎸</span>{% endif %}</span>{% if c.total %}<span class="cnt">{{ c.done }}/{{ c.total }}</span>{% endif %}</span>
          <span class="dots">{% for s in c.states %}<i class="dot {{ s.state }}"></i>{% endfor %}</span>
        </a>
      {% endfor %}{% endfor %}
    </div>
    <div class="legend">
      <span><i class="dot done"></i>kaydetti</span>
      <span><i class="dot metro"></i>metronomla</span>
      <span><i class="dot joker"></i>joker</span>
      <span><i class="dot missed"></i>kaydetmedi</span>
      <span><i class="dot pending"></i>bugün bekleniyor</span>
      <span>Bir güne dokun, o günün kayıtları aşağıda görünür.</span>
    </div>
  </section>

  {% if summary %}
  <details class="panel sumbox">
    <summary><h2>{{ month_label }} özeti</h2></summary>
    <div class="tablewrap"><table class="sum">
      <tr><th>Kişi</th><th>Seri</th><th title="Kaydettiği gün">Gün</th><th title="Metronomlu gün">🔥 Gün</th><th title="Kaçırdığı gün">Kaçırdı</th></tr>
      {% for s in summary %}
      <tr class="{% if not s.active %}inactive{% endif %}">
        <td>{{ s.name }}{% if not s.active %} (ayrıldı){% endif %}</td>
        <td class="n">{{ s.streak }}</td><td class="n">{{ s.done }}</td><td class="n">🔥 {{ s.metro }}</td>
        <td class="n{% if s.missed %} miss-n{% endif %}">{{ s.missed }}</td>
      </tr>
      {% endfor %}
    </table></div>
  </details>
  {% endif %}

  <section style="display:grid;gap:22px">
    {% if not days %}<div class="empty">Bu ay henüz kayıt yok. Telegram grubuna sesli mesaj atınca burada görünür.</div>{% endif %}
    {% for d in days %}
    <div class="day" id="d-{{ d.iso }}">
      <div class="dayhead"><h2>{{ d.label }}{% if d.is_today %} · bugün{% endif %}</h2>
        <span class="label">{{ d.recs|length }} kayıt</span></div>
      {% for r in d.recs %}
      <article class="rec">
        <div class="head"><span class="who">{{ r.name }}</span>
          <span class="meta">{{ r.time }} · {{ r.duration }}</span>
          {% if r.metronome %}<span class="bpm">🔥 ♩ {{ r.bpm }} bpm</span>
          {% elif r.tempo %}<span class="tempo" title="Metronomsuz, çalınan tempodan tahmin">♩ ~{{ r.tempo }} bpm</span>{% endif %}
          <a class="dl" href="/audio/{{ r.id }}?dl=1">İndir</a></div>
        {% if r.file_name %}<div class="fname">📄 {{ r.file_name }}</div>{% endif %}
        {% if r.caption %}<div class="cap">{{ r.caption }}</div>{% endif %}
        {% if r.video %}<video controls preload="metadata" playsinline class="{{ 'round' if r.round }}" data-id="{{ r.id }}" src="/audio/{{ r.id }}#t=0.1"></video>
        {% else %}<audio controls preload="none" data-id="{{ r.id }}" src="/audio/{{ r.id }}"></audio>{% endif %}
        {% if r.listeners %}<div class="ears">👂 {{ r.listeners|join(', ') }} dinledi</div>{% endif %}
      </article>
      {% endfor %}
      {% if d.jokers %}<div class="note joker">🃏 Joker: {{ d.jokers|join(', ') }}</div>{% endif %}
      {% if d.missed %}<div class="note miss">Kaydetmedi: {{ d.missed|join(', ') }}</div>{% endif %}
      {% if d.pending %}<div class="note pend">Bekleniyor: {{ d.pending|join(', ') }}</div>{% endif %}
    </div>
    {% endfor %}
  </section>
</div>
<script>
// Only the chosen day's recordings are shown; without script every day stays visible
document.body.classList.add("js");
function showDay(iso, scroll) {
  var el = document.getElementById("d-" + iso);
  if (!el) return false;
  document.querySelectorAll(".day.show").forEach(function (d) { d.classList.remove("show"); });
  document.querySelectorAll(".cell.sel").forEach(function (c) { c.classList.remove("sel"); });
  el.classList.add("show");
  var cell = document.querySelector('.cell[href="#d-' + iso + '"]');
  if (cell) cell.classList.add("sel");
  if (scroll) el.scrollIntoView({behavior: "smooth", block: "start"});
  return true;
}
document.querySelectorAll(".cell[href]").forEach(function (c) {
  c.addEventListener("click", function (e) {
    e.preventDefault();
    var iso = c.getAttribute("href").slice(3);
    showDay(iso, true);
    history.replaceState(null, "", "#d-" + iso);
  });
});
var first = document.querySelector(".day");
var fromHash = location.hash.indexOf("#d-") === 0 && showDay(location.hash.slice(3), true);
if (!fromHash && first) showDay(first.id.slice(2), false);
{% if who %}
document.querySelectorAll("audio[data-id],video[data-id]").forEach(function (el) {
  el.addEventListener("play", function () {
    if (el.dataset.sent) return;
    el.dataset.sent = "1";
    fetch("/dinle/" + el.dataset.id, {method: "POST", credentials: "same-origin"}).catch(function () {});
  });
});
var ch = document.getElementById("whochange");
if (ch) ch.addEventListener("click", function (e) {
  e.preventDefault();
  document.getElementById("whopick").hidden = false;
});
{% endif %}
</script>
</body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)


def change_milestone(chat_id, add, args, cel):
    n = int(args[0]) if args and args[0].isdigit() else 0
    unit = args[1][:1] if len(args) > 1 else ""
    field_name = {"g": "streaks", "k": "counts"}.get(unit)
    if not field_name or n < 1 or n > 10000 or (field_name == "streaks" and n < 2):
        send(chat_id, "Örnek: /ekle 40 gün, /ekle 200 kayıt, /cikar 14 gün")
        return
    label = f"{n} gün" if field_name == "streaks" else "İlk kayıt" if n == 1 else f"{n}. kayıt"
    values = set(cel[field_name])
    if add == (n in values):
        send(chat_id, f"{label} zaten {'var' if add else 'yok'}.")
        return
    values = values | {n} if add else values - {n}
    celebrations_ref().set({field_name: sorted(values)}, merge=True)
    if add:
        send(chat_id, f"✅ {label} eklendi. GIF'ini bana atıp {label} butonunu seçebilirsin.")
    else:
        send(chat_id, f"🗑 {label} çıkarıldı. Atadığın GIF saklanıyor, geri eklersen yine gelir.")
