"""In-memory stand-ins for Firestore, Cloud Storage and the Telegram API."""
import datetime as dt
import importlib
import io
import os
import sys
import wave
from unittest import mock

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.update(BOT_TOKEN="test", WEBHOOK_SECRET="hook", CRON_SECRET="cron", WEB_TOKEN="tok",
                  BUCKET="bucket", PUBLIC_URL="https://bot.example", DAY_START_HOUR="4")

from google.cloud import firestore as fs_mod  # noqa: E402
from google.cloud import storage as st_mod  # noqa: E402


class Snap:
    def __init__(self, id, data):
        self.id, self._d = id, data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._d)

    def get(self, k):
        # Real Firestore raises for a missing field
        if k not in self._d:
            raise KeyError(f"'{k}' is not contained in the data")
        return self._d[k]


class Doc:
    def __init__(self, store, coll, id):
        self.s, self.c, self.id = store, coll, id

    def get(self):
        d = self.s.setdefault(self.c, {}).get(self.id)
        return Snap(self.id, None if d is None else dict(d))

    def set(self, data, merge=False, _deep=True):
        cur = self.s.setdefault(self.c, {})
        base = dict(cur.get(self.id, {})) if merge else {}
        for k, v in data.items():
            if v is fs_mod.DELETE_FIELD:
                base.pop(k, None)
            elif isinstance(v, fs_mod.ArrayUnion):
                base[k] = list(base.get(k, [])) + [x for x in v.values if x not in base.get(k, [])]
            elif merge and _deep and isinstance(v, dict) and isinstance(base.get(k), dict):
                # Like Firestore: set(merge=True) merges nested maps instead of replacing them
                base[k] = {**base[k], **v}
            else:
                base[k] = v
        cur[self.id] = base

    def update(self, data):
        if self.id not in self.s.get(self.c, {}):
            raise KeyError(self.id)
        self.set(data, merge=True, _deep=False)

    def delete(self):
        self.s.get(self.c, {}).pop(self.id, None)


class Query:
    OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b}

    def __init__(self, store, coll, filters=()):
        self.s, self.c, self.f = store, coll, list(filters)

    def where(self, filter):
        return Query(self.s, self.c, self.f + [filter])

    def select(self, fields):
        return self

    def stream(self):
        for id, d in list(self.s.get(self.c, {}).items()):
            if all(self.OPS[f.op_string](d.get(f.field_path), f.value) for f in self.f):
                yield Snap(id, dict(d))


class Coll(Query):
    def document(self, id):
        return Doc(self.s, self.c, id)


class FakeFirestore:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return Coll(self.store, name)


class Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    @property
    def size(self):
        return len(self.bucket.data[self.name])

    def upload_from_string(self, data, content_type=None):
        self.bucket.data[self.name] = data

    def download_as_bytes(self, start=None, end=None):
        d = self.bucket.data[self.name]
        return d if start is None else d[start:end + 1]

    def delete(self):
        self.bucket.data.pop(self.name)


class FakeBucket:
    def __init__(self):
        self.data = {}

    def blob(self, name):
        return Blob(self, name)

    def get_blob(self, name):
        return Blob(self, name) if name in self.data else None


class FakeTelegram:
    """Records every Bot API call; file downloads return `next_file`."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.next_file = b""

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json or {}))
        if method in self.fail:
            return mock.Mock(json=lambda: {"ok": False, "description": "Bad Request: test"})
        result = {"file_path": "f/file"} if method == "getFile" else True
        return mock.Mock(json=lambda: {"ok": True, "result": result})

    def get(self, url, timeout=None):
        return mock.Mock(content=self.next_file, raise_for_status=lambda: None)

    def sent(self, chat_id=None):
        return [p["text"] for m, p in self.calls
                if m == "sendMessage" and (chat_id is None or str(p["chat_id"]) == str(chat_id))]

    def reactions(self):
        return [p["reaction"] for m, p in self.calls if m == "setMessageReaction"]


def wav_bytes(seconds=20, bpm=None, play=True, seed=0, sr=16000):
    """Synthetic practice take: plucked notes with human timing, optional metronome clicks."""
    rng = np.random.default_rng(seed)
    y = np.zeros(int(seconds * sr) + sr)
    if bpm:
        n = int(0.012 * sr)
        t = np.arange(n) / sr
        click = 0.2 * np.sin(2 * np.pi * 2500 * t) * np.exp(-t / 0.002) + 0.1 * rng.standard_normal(n) * np.exp(-t / 0.0015)
        k = 0
        while k * 60 / bpm + 0.3 < seconds:
            i = int((k * 60 / bpm + 0.3) * sr)
            y[i:i + n] += click
            k += 1
    if play:
        step = 60 / 80 / 2
        k = 0
        while k * step + 0.3 < seconds:
            if rng.random() < 0.85:
                f = rng.choice([196, 220, 247, 262, 294, 330])
                n = int(0.5 * sr)
                t = np.arange(n) / sr
                note = sum(np.sin(2 * np.pi * f * h * t) / h ** 1.2 for h in range(1, 9)) * np.exp(-t / 0.25)
                note[:48] *= np.linspace(0, 1, 48)
                note[:64] += 0.4 * rng.standard_normal(64)
                note = 0.6 * note / np.max(np.abs(note))
                i = max(0, int((k * step + 0.3 + rng.normal(0, 0.02)) * sr))
                y[i:i + n] += note[:len(y) - i]
            k += 1
    y += 0.003 * rng.standard_normal(len(y))
    y /= np.max(np.abs(y)) * 1.1
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


@pytest.fixture
def env():
    fs, bucket, telegram = FakeFirestore(), FakeBucket(), FakeTelegram()
    with mock.patch.object(fs_mod, "Client", return_value=fs), \
            mock.patch.object(st_mod, "Client", return_value=mock.Mock(bucket=lambda n: bucket)), \
            mock.patch("requests.post", telegram.post), \
            mock.patch("requests.get", telegram.get):
        import main
        main = importlib.reload(main)
        main._last_alert.clear()
        # Reactions in tests must not depend on the time of day the suite runs
        main.real_time_badge, main.time_badge = main.time_badge, lambda ts: None
        yield Env(main, fs, bucket, telegram)


class Env:
    CHAT = {"id": -1001, "type": "supergroup", "title": "Band"}

    def __init__(self, main, fs, bucket, telegram):
        self.main, self.fs, self.bucket, self.tg = main, fs, bucket, telegram
        self.mid = 0
        self.client = main.app.test_client()

    def user(self, uid, name):
        return {"id": uid, "first_name": name}

    def message(self, user, chat=None, when=None, **fields):
        self.mid += 1
        when = when or dt.datetime.now(self.main.TZ)
        msg = {"message_id": self.mid, "chat": chat or self.CHAT, "from": user,
               "date": int(when.timestamp()), **fields}
        self.main.handle_update({"message": msg})
        return self.mid

    def command(self, user, text, **fields):
        return self.message(user, text=text, **fields)

    def voice(self, user, data, when=None, caption=None):
        self.tg.next_file = data
        extra = {"caption": caption} if caption else {}
        return self.message(user, when=when, voice={"file_id": "f", "duration": 20, "mime_type": "audio/ogg"}, **extra)

    def add_recording(self, uid, back_days, metro=False, duration=60):
        day = self.main.today() - dt.timedelta(days=back_days)
        self.fs.store.setdefault("recordings", {})[f"r_{uid}_{back_days}"] = {
            "user_id": str(uid), "name": "", "day": day.isoformat(),
            "ts": dt.datetime.combine(day, dt.time(20), self.main.TZ), "duration": duration,
            "metronome": metro, "bpm": 80 if metro else None, "gcs_path": f"p/{uid}/{back_days}",
            "mime": "audio/ogg", "kind": "voice", "caption": ""}
        self.bucket.data[f"p/{uid}/{back_days}"] = b"OggS0000"

    def set_first_day(self, uid, back_days):
        self.fs.store["members"][str(uid)]["first_day"] = (self.main.today() - dt.timedelta(days=back_days)).isoformat()
