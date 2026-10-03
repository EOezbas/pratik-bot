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

import bigfiles
import metronome

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

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
COOKIE = "pz"
WHO_COOKIE = "pz_who"
ALERT_INTERVAL_SEC = 600
TOKEN_CACHE_SEC = 30
STREAK_MILESTONES = {7, 14, 21, 30, 60, 365}
COUNT_MILESTONES = {1, 5, 31, 50, 69, 100}
MAX_TG_BYTES = 20 * 1024 * 1024
HISTORY_DAYS = 21
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
    ("seri", "Son 21 gün"),
    ("detay", "Herkesin istatistikleri"),
    ("takvim", "Tüm kayıtların takvimi"),
    ("katil", "Gruba katıl"),
    ("ayril", "Hatırlatmalardan çık"),
    ("sil", "Kendi kaydına yanıt vererek sil"),
    ("yenilink", "Takvim linkini yenile"),
    ("yardim", "Nasıl çalışır"),
]


# ---------- helpers ----------

def now_local():
    return dt.datetime.now(TZ)


def practice_day(ts):
    return (ts - dt.timedelta(hours=DAY_START_HOUR)).date()


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


def tg(method, **params):
    try:
        for _ in range(3):
            r = requests.post(f"{API}/{method}", json=params, timeout=30)
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


ALLOWED_UPDATES = ["message", "edited_message", "message_reaction"]


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
    msg = upd.get("message")
    edited = upd.get("edited_message")
    if edited:
        cap = edited.get("caption")
        if cap is not None:
            ref = db.collection("recordings").document(f'{edited["chat"]["id"]}_{edited["message_id"]}')
            if ref.get().exists:
                ref.update({"caption": cap})
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
        deactivate_member(msg["left_chat_member"]["id"])
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
        upsert_member(user)
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
            old = field(snap, "caption") or ""
            ref.update({"caption": (old + "\n" + text).strip()})
            tg("setMessageReaction", chat_id=chat["id"], message_id=msg["message_id"],
               reaction=[{"type": "emoji", "emoji": "✍"}])


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


def save_recording(msg, user, media, kind):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    if db.collection("recordings").document(f"{chat_id}_{mid}").get().exists:
        return
    mime, ext = media_type(media, kind)

    if (media.get("file_size") or 0) > MAX_TG_BYTES:
        save_large_recording(msg, user, media, kind, mime, ext)
        return

    info = tg("getFile", file_id=media["file_id"])
    if not info or not info.get("file_path"):
        send(chat_id, "Bu kayıt kaydedilemedi. Dosya 20 MB’tan büyük olabilir, daha kısa bir kayıt gönder.", mid)
        return
    try:
        r = requests.get(f"{FILE_API}/{info['file_path']}", timeout=60)
        r.raise_for_status()
    except Exception:
        log.exception("download failed")
        alert("save", "Bir kayıt indirilemedi, loglara bak.")
        send(chat_id, "Bu kayıt kaydedilemedi. Lütfen tekrar gönder.", mid)
        return
    store_recording(msg, user, media, kind, r.content, mime, ext)


def save_large_recording(msg, user, media, kind, mime, ext):
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
    run_in_background(process_large_recording, msg, user, media, kind, mime, ext)


def run_in_background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def process_large_recording(msg, user, media, kind, mime, ext):
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
        store_recording(msg, user, media, kind, content, mime, ext)
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


def store_recording(msg, user, media, kind, content, mime, ext):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    ref = db.collection("recordings").document(f"{chat_id}_{mid}")
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
            "metronome": has_metro,
            "bpm": bpm,
            "tempo": tempo,
        })
    except Exception:
        log.exception("save failed")
        alert("save", "Bir kayıt kaydedilemedi, loglara bak.")
        send(chat_id, "Bu kayıt kaydedilemedi. Lütfen tekrar gönder.", mid)
        return
    tg("setMessageReaction", chat_id=chat_id, message_id=mid,
       reaction=[{"type": "emoji", "emoji": "🔥" if has_metro else "❤"}])
    try:
        check_milestones(chat_id, user)
    except Exception:
        log.exception("milestone check failed")


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
    if cur in STREAK_MILESTONES and start:
        key = f"streak{cur}:{start.isoformat()}"
        if key not in reached:
            new_keys.append(key)
            lines.append(f"🎉 {mention(uid, member.get('name', ''))} {cur} günlük seriye ulaştı!")
            cel_key = f"streak{cur}"
            if cur >= 100:
                emoji = "🏆"
    if st["count"] in COUNT_MILESTONES:
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
             "Not eklemek için mesaja açıklama yaz ya da kendi kaydına yanıt ver.\n\n"
             "/bugun – bugün kim kaydetti\n"
             "/seri – son 21 gün (🔥 metronomlu, ❤ kaydetti, 🃏 joker, 💔 atladı)\n"
             "/detay – herkesin istatistikleri\n"
             "/takvim – tüm kayıtların takvimi\n"
             "/katil – kayıt atmadan gruba katıl\n"
             "/ayril – hatırlatmalardan çık\n"
             "/sil – kendi kaydına yanıt olarak yaz, kayıt silinir\n"
             "/yenilink – takvim linki grup dışına çıktıysa yenisini oluştur", mid)
    elif cmd == "/katil":
        upsert_member(user)
        send(chat_id, f"{mention(user['id'], display_name(user))} katıldı.", mid)
    elif cmd == "/ayril":
        deactivate_member(user["id"])
        send(chat_id, "Hatırlatmalardan çıkarıldın. Kayıt attığında tekrar eklenirsin.", mid)
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


def delete_recording(msg, user):
    chat_id = msg["chat"]["id"]
    mid = msg["message_id"]
    rep = msg.get("reply_to_message")
    if not rep:
        send(chat_id, "Silmek istediğin kayda yanıt olarak /sil yaz.", mid)
        return
    ref = db.collection("recordings").document(f'{chat_id}_{rep["message_id"]}')
    snap = ref.get()
    if not snap.exists:
        send(chat_id, "Bu mesaj kayıtlı bir pratik kaydı değil.", mid)
        return
    r = snap.to_dict()
    is_admin = str(get_admin_id() or "") == str(user["id"])
    if str(r.get("user_id")) != str(user["id"]) and not is_admin:
        send(chat_id, "Sadece kendi kayıtlarını silebilirsin.", mid)
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
    if tg("deleteMessage", chat_id=chat_id, message_id=rep["message_id"]):
        tg("deleteMessage", chat_id=chat_id, message_id=mid)
        return
    # Bots cannot delete messages older than 48 hours or without the delete permission
    tg("setMessageReaction", chat_id=chat_id, message_id=rep["message_id"], reaction=[])
    send(chat_id, "🗑 Kayıt takvimden silindi, ama Telegram'daki mesajı silemedim "
                  "(48 saatten eski olabilir). Mesajı elle silebilirsin.", mid)

MILESTONE_LABELS = [(f"streak{n}", f"{n} gün") for n in sorted(STREAK_MILESTONES)] + \
                   [(f"count{n}", "İlk kayıt" if n == 1 else f"{n}. kayıt") for n in sorted(COUNT_MILESTONES)]
LABEL_TO_KEY = {label: key for key, label in MILESTONE_LABELS}


def celebrations_ref():
    return db.collection("config").document("celebrations")


def load_celebrations():
    snap = celebrations_ref().get()
    data = snap.to_dict() if snap.exists else {}
    return {"assigned": dict(data.get("assigned") or {}), "pending": data.get("pending")}


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


def list_celebrations(chat_id, assigned):
    lines = ["Kutlamalar:"]
    for key, label in MILESTONE_LABELS:
        item = assigned.get(key)
        lines.append(f"{label}: {'sticker' if item and item['type'] == 'sticker' else 'GIF' if item else '– büyük emoji'}")
    lines.append("\nAyarlananlar aşağıda. Kaldırmak istediğine yanıt verip /sil yaz.")
    tg("sendMessage", chat_id=chat_id, text="\n".join(lines))
    for key, label in MILESTONE_LABELS:
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


def milestone_keyboard():
    labels = [label for _, label in MILESTONE_LABELS]
    rows = [labels[i:i + 3] for i in range(0, len(labels), 3)] + [["İptal"]]
    return {"keyboard": [[{"text": t} for t in row] for row in rows],
            "one_time_keyboard": True, "resize_keyboard": True}


def handle_private(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from") or {}
    raw = (msg.get("text") or "").strip()
    text = raw.lower()
    cmd = text.split()[0].split("@")[0] if text else ""
    cmd = cmd.replace("ö", "o").replace("ı", "i").replace("ş", "s")
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

    media = celebration_media(msg)
    cel_command = cmd in ("/kutlamalar", "/sil")
    if not (media or cel_command or raw in LABEL_TO_KEY or raw == "İptal"):
        send(chat_id, "Bu bot sadece pratik grubunda çalışıyor.")
        return
    if not is_admin:
        send(chat_id, "Kutlamaları sadece yönetici değiştirebilir.")
        return

    cel = load_celebrations()
    if media:
        celebrations_ref().set({"pending": media}, merge=True)
        tg("sendMessage", chat_id=chat_id, text="Bu hangi kutlama için?", reply_markup=milestone_keyboard())
    elif raw == "İptal":
        celebrations_ref().set({"pending": None}, merge=True)
        tg("sendMessage", chat_id=chat_id, text="İptal edildi.", reply_markup={"remove_keyboard": True})
    elif raw in LABEL_TO_KEY:
        if not cel["pending"]:
            tg("sendMessage", chat_id=chat_id, text="Önce bir sticker ya da GIF at.",
               reply_markup={"remove_keyboard": True})
            return
        cel["assigned"][LABEL_TO_KEY[raw]] = cel["pending"]
        celebrations_ref().set({"assigned": cel["assigned"], "pending": None})
        tg("sendMessage", chat_id=chat_id, text=f"✅ {raw} kutlaması için ayarlandı.",
           reply_markup={"remove_keyboard": True})
    elif cmd == "/kutlamalar":
        # In the background so the webhook answers at once and Telegram does not resend the command
        run_in_background(list_celebrations, chat_id, cel["assigned"])
    else:
        target = celebration_media(msg.get("reply_to_message") or {})
        if not target:
            send(chat_id, "Kaldırmak istediğin sticker ya da GIF'e yanıt verip /sil yaz.")
            return
        keep = {k: v for k, v in cel["assigned"].items() if v["uid"] != target["uid"]}
        celebrations_ref().set({"assigned": keep}, merge=True)
        removed = [label for k, label in MILESTONE_LABELS if k in cel["assigned"] and k not in keep]
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
    if not missing:
        send(chat_id, "Bugün herkes kaydetti ❤")
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
    send(chat_id, "\n".join(lines))
    return "sent"


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

    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        row = []
        for d in week:
            in_month = d.month == first.month
            sts = states_for(d) if in_month else []
            row.append({
                "date": d, "iso": d.isoformat(), "in_month": in_month, "is_today": d == t,
                "states": sts, "done": sum(1 for s in sts if s["state"] in ("done", "metro")),
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
                "caption": r.get("caption", ""), "mime": r.get("mime", ""),
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
        today_label=tr_date(t), rec_count=len(recs),
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
.day{display:grid;gap:8px;scroll-margin-top:16px}
.day:target .dayhead h2{color:var(--accent)}
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
    <div><div class="label">Bugün · {{ today_label }}</div><h1>Pratik Zinciri</h1></div>
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
          <span class="top"><span class="num">{{ c.date.day }}</span>{% if c.total %}<span class="cnt">{{ c.done }}/{{ c.total }}</span>{% endif %}</span>
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
      <span>Bir güne dokun, o günün kayıtlarına git.</span>
    </div>
  </section>

  {% if summary %}
  <section class="panel">
    <h2>{{ month_label }} özeti</h2>
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
  </section>
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
