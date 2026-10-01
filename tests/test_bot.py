import datetime as dt
import re

import pytest

from conftest import wav_bytes

EMRE = {"id": 1, "first_name": "Emre"}
CAN = {"id": 2, "first_name": "Can"}
DENIZ = {"id": 3, "first_name": "Deniz"}
TELEGRAM_HTML = re.compile(r"<(?!/?b>|a href=\"tg://user\?id=\d+\">|/a>)")


@pytest.fixture(scope="module")
def metro_wav():
    return wav_bytes(bpm=92)


@pytest.fixture(scope="module")
def plain_wav():
    return wav_bytes()


def join_all(env, *users):
    for u in users:
        env.command(u, "/katil")


def assert_valid_html(env):
    for text in env.tg.sent():
        assert not TELEGRAM_HTML.search(text), text


# ---------- metronome detection ----------

def test_metronome_detected_with_bpm(env, metro_wav):
    has, bpm, duration = env.main.metronome.detect(metro_wav)
    assert has and bpm == 92 and duration >= 19


def test_no_metronome_in_plain_playing(env, plain_wav):
    has, bpm, _ = env.main.metronome.detect(plain_wav)
    assert not has and bpm is None


def test_short_or_silent_audio(env):
    assert env.main.metronome.detect(wav_bytes(seconds=2, play=False))[0] is False


# ---------- recordings ----------

def test_voice_saved_with_reactions(env, metro_wav, plain_wav):
    env.voice(EMRE, metro_wav, caption="G majör gam")
    env.voice(CAN, plain_wav)
    recs = env.fs.store["recordings"]
    assert len(recs) == 2 and len(env.bucket.data) == 2
    emre = next(r for r in recs.values() if r["user_id"] == "1")
    assert emre["metronome"] and emre["bpm"] == 92 and emre["caption"] == "G majör gam"
    assert [r[0]["emoji"] for r in env.tg.reactions()] == ["🔥", "❤"]


def test_audio_file_sent_as_document(env, plain_wav):
    env.tg.next_file = plain_wav
    env.message(EMRE, document={"file_id": "d", "file_name": "Take 3.WAV",
                                "mime_type": "application/octet-stream", "file_size": len(plain_wav)})
    (rec,) = env.fs.store["recordings"].values()
    assert rec["kind"] == "document" and rec["mime"].startswith("audio/") and rec["gcs_path"].endswith(".wav")


def test_non_media_document_ignored(env):
    env.message(EMRE, document={"file_id": "p", "file_name": "notes.pdf", "mime_type": "application/pdf"})
    assert not env.fs.store.get("recordings")


def test_too_large_file_rejected(env):
    env.message(EMRE, video={"file_id": "v", "mime_type": "video/mp4", "file_size": 30 * 1024 * 1024})
    assert not env.fs.store.get("recordings")
    assert "20 MB" in env.tg.sent()[-1]


def test_late_night_counts_for_previous_day(env, plain_wav):
    now = dt.datetime.now(env.main.TZ)
    late = now.replace(hour=2, minute=30) if now.hour >= 4 else (now - dt.timedelta(days=1)).replace(hour=2, minute=30)
    env.voice(EMRE, plain_wav, when=late)
    (rec,) = env.fs.store["recordings"].values()
    assert rec["day"] == (late.date() - dt.timedelta(days=1)).isoformat()


def test_reply_adds_note(env, plain_wav):
    vid = env.voice(EMRE, plain_wav)
    env.message(EMRE, text="köprü kısmı", reply_to_message={"message_id": vid, "from": EMRE, "voice": {"file_id": "f"}})
    (rec,) = env.fs.store["recordings"].values()
    assert rec["caption"] == "köprü kısmı"


# ---------- commands ----------

def test_turkish_command_spelling(env):
    join_all(env, EMRE)
    env.command(EMRE, "/BUGÜN")
    assert "Emre" in env.tg.sent()[-1]


def test_seri_shows_history_with_joker(env):
    join_all(env, EMRE, CAN)
    env.set_first_day(1, 20)
    env.set_first_day(2, 2)
    for back in range(0, 21):
        if back not in (3, 9, 10):
            env.add_recording(1, back, metro=back % 4 == 0)
    env.command(EMRE, "/seri")
    text = env.tg.sent()[-1]
    assert "⛓" not in text and not text.startswith("Seriler")
    emre_line = text.split("\n")[1]
    assert len(re.findall("🔥|❤|🃏|💔", emre_line)) == 21
    assert "🃏" in emre_line
    can_line = text.split("\n")[-1]
    assert len(re.findall("🃏|💔", can_line)) == 2 and not re.search("🔥|❤", can_line)
    assert_valid_html(env)


def test_joker_rules(env):
    join_all(env, EMRE)
    m = env.main
    member = {"first_day": "2026-09-01"}
    t = dt.date(2026, 9, 30)  # Wednesday
    days = {d.isoformat() for d in (dt.date(2026, 9, 1) + dt.timedelta(n) for n in range(30))}
    days -= {"2026-09-13", "2026-09-14", "2026-09-27", "2026-09-26"}  # Sun, Mon, Sat+Sun same week
    st = {"days": days, "metro": set(), "sec": 0, "count": len(days)}
    hist = dict((d.isoformat(), s) for d, s in m.history(member, st, t))
    assert hist["2026-09-13"] == "joker" and hist["2026-09-14"] == "joker"
    assert hist["2026-09-26"] == "joker" and hist["2026-09-27"] == "missed"
    cur, longest = m.chain_stats(m.history(member, st, t))
    assert cur == 3 and longest == 23  # Sep 1-25 minus two joker days


def test_detay_valid_for_short_recordings(env):
    join_all(env, EMRE, DENIZ)
    env.add_recording(1, 0, duration=5)
    env.command(EMRE, "/detay")
    text = env.tg.sent()[-1]
    assert "&lt;1 dk · 1 kayıt" in text and "0 dk · 0 kayıt" in text
    assert "⛓" not in text
    assert_valid_html(env)


def test_sil_only_own_recording(env, plain_wav):
    vid = env.voice(EMRE, plain_wav)
    reply = {"message_id": vid, "from": EMRE, "voice": {"file_id": "f"}}
    env.command(CAN, "/sil", reply_to_message=reply)
    assert len(env.fs.store["recordings"]) == 1
    env.command(EMRE, "/sil", reply_to_message=reply)
    assert not env.fs.store["recordings"] and not env.bucket.data


def test_yenilink_invalidates_old_link(env):
    join_all(env, EMRE)
    assert env.client.get("/?t=tok").status_code == 302
    env.command(EMRE, "/yenilink")
    new = re.search(r"\?t=([0-9a-f]+)", env.tg.sent()[-1]).group(1)
    env.client.delete_cookie("pz")
    assert env.client.get("/?t=tok").status_code == 403
    assert env.client.get(f"/?t={new}").status_code == 302


# ---------- milestones ----------

def test_streak_milestone_announced_once(env, plain_wav):
    join_all(env, EMRE)
    env.set_first_day(1, 10)
    for back in range(1, 7):
        env.add_recording(1, back)
    env.voice(EMRE, plain_wav)
    env.voice(EMRE, plain_wav)
    announcements = [t for t in env.tg.sent() if "7 günlük seriye" in t]
    assert len(announcements) == 1


def test_count_milestone(env, plain_wav):
    join_all(env, EMRE)
    env.set_first_day(1, 60)
    for back in range(1, 50):
        env.add_recording(1, back + (back // 3) * 0)
    env.voice(EMRE, plain_wav)
    assert any("50. kaydını attı" in t for t in env.tg.sent())


# ---------- reminders ----------

def test_reminder_tags_only_missing_with_joker_state(env):
    join_all(env, EMRE, CAN)
    env.set_first_day(1, 5)
    env.set_first_day(2, 5)
    env.add_recording(1, 0)
    for back in (1, 2):
        env.add_recording(2, back)
    r = env.client.post("/cron/reminder", headers={"X-Cron-Secret": "cron"})
    assert r.status_code == 200
    text = env.tg.sent()[-1]
    assert "tg://user?id=2" in text and "tg://user?id=1" not in text
    assert_valid_html(env)


def test_cron_requires_secret(env):
    assert env.client.post("/cron/reminder").status_code == 403
    assert env.client.post("/cron/weekly").status_code == 403


def test_weekly_summary(env):
    join_all(env, EMRE, CAN)
    env.add_recording(1, 0, metro=True)
    assert env.client.post("/cron/weekly", headers={"X-Cron-Secret": "cron"}).status_code == 200
    assert "metronom ustası" in env.tg.sent()[-1]
    assert_valid_html(env)


# ---------- calendar ----------

def test_calendar_and_listening(env, plain_wav):
    join_all(env, EMRE, CAN)
    env.voice(EMRE, plain_wav)
    rec_id = next(iter(env.fs.store["recordings"]))
    c = env.client
    assert c.get("/").status_code == 403
    assert c.get("/?t=tok").status_code == 302
    page = c.get("/").data.decode()
    assert "Kim olduğunu seç" in page
    c.get("/ben/2")
    assert c.post(f"/dinle/{rec_id}").status_code == 204
    c.get("/ben/1")
    c.post(f"/dinle/{rec_id}")
    assert env.fs.store["recordings"][rec_id]["listeners"] == ["2"]
    page = c.get("/").data.decode()
    assert "👂 Can dinledi" in page


def test_audio_range_request(env, plain_wav):
    env.voice(EMRE, plain_wav)
    rec_id = next(iter(env.fs.store["recordings"]))
    env.client.get("/?t=tok")
    r = env.client.get(f"/audio/{rec_id}", headers={"Range": "bytes=0-3"})
    assert r.status_code == 206 and r.data == plain_wav[:4]


# ---------- admin and alerts ----------

def test_admin_registration_and_alert(env, plain_wav):
    private = {"id": 1, "type": "private"}
    env.message(EMRE, chat=private, text="/yönetici")
    assert "Önce grupta" in env.tg.sent(1)[-1]
    join_all(env, EMRE)
    env.message(EMRE, chat=private, text="/yonetici")
    assert env.fs.store["config"]["state"]["admin_id"] == "1"
    env.tg.fail.add("sendMessage")
    env.command(EMRE, "/bugun")
    env.tg.fail.clear()
    alerts = [p for m, p in env.tg.calls if m == "sendMessage" and str(p["chat_id"]) == "1" and "⚠️" in p["text"]]
    assert len(alerts) == 1


def test_webhook_secret(env):
    assert env.client.post("/telegram", json={}).status_code == 403
    assert env.client.post("/telegram", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "hook"}).status_code == 200


# ---------- celebrations ----------

def make_admin(env):
    join_all(env, EMRE)
    env.message(EMRE, chat={"id": 1, "type": "private"}, text="/yonetici")


def test_milestone_sends_big_emoji_without_stickers(env, plain_wav):
    join_all(env, EMRE)
    env.set_first_day(1, 10)
    for back in range(1, 7):
        env.add_recording(1, back)
    env.voice(EMRE, plain_wav)
    assert env.tg.sent()[-1] == "🎉"


def test_admin_assigns_celebration_per_milestone(env, plain_wav):
    make_admin(env)
    private = {"id": 1, "type": "private"}
    env.message(CAN, chat={"id": 2, "type": "private"}, sticker={"file_id": "S0", "file_unique_id": "u0"})
    assert "sadece yönetici" in env.tg.sent(2)[-1]
    env.command(CAN, "/kutlamalar")
    assert not any("Kutlamalar" in t for t in env.tg.sent(-1001))

    env.message(EMRE, chat=private, sticker={"file_id": "S7", "file_unique_id": "u7"})
    assert env.tg.calls[-1][1]["reply_markup"]["keyboard"]
    env.message(EMRE, chat=private, text="7 gün")
    env.message(EMRE, chat=private, animation={"file_id": "G30", "file_unique_id": "u30"})
    env.message(EMRE, chat=private, text="30 gün")
    assigned = env.fs.store["config"]["celebrations"]["assigned"]
    assert assigned["streak7"]["file_id"] == "S7" and assigned["streak30"]["file_id"] == "G30"

    env.message(EMRE, chat=private, text="/kutlamalar")
    assert any("50 gün: – büyük emoji" in t for t in env.tg.sent(1))
    env.message(EMRE, chat=private, text="/sil", reply_to_message={"message_id": 9, "animation": {"file_id": "G30", "file_unique_id": "u30"}})
    assert "streak30" not in env.fs.store["config"]["celebrations"]["assigned"]

    env.set_first_day(1, 10)
    for back in range(1, 7):
        env.add_recording(1, back)
    env.voice(EMRE, plain_wav)
    assert ("sendSticker", {"chat_id": -1001, "sticker": "S7"}) in env.tg.calls


# ---------- reactions as listens ----------

def test_reaction_marks_listened(env, plain_wav):
    join_all(env, EMRE, CAN)
    vid = env.voice(EMRE, plain_wav)
    rec_id = f"-1001_{vid}"

    def react(user, emoji, chat_id=-1001):
        env.main.handle_update({"message_reaction": {
            "chat": {"id": chat_id, "type": "supergroup"}, "message_id": vid, "user": user, "date": 0,
            "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": emoji}] if emoji else []}})

    react(EMRE, "👍")
    react(CAN, None)
    react(CAN, "👂", chat_id=-999)
    assert "listeners" not in env.fs.store["recordings"][rec_id]
    react(CAN, "👂")
    react(CAN, "🔥")
    assert env.fs.store["recordings"][rec_id]["listeners"] == ["2"]


def test_webhook_subscribes_to_reactions(env):
    calls = [p for m, p in env.tg.calls if m == "setWebhook"]
    assert calls and "message_reaction" in calls[-1]["allowed_updates"]


def test_milestones_31_and_69(env):
    m = env.main
    assert {31, 69} <= m.STREAK_MILESTONES and {31, 69} <= m.COUNT_MILESTONES
    labels = [b["text"] for row in m.milestone_keyboard()["keyboard"] for b in row]
    assert {"31 gün", "69 gün", "31. kayıt", "69. kayıt"} <= set(labels)


def test_first_and_fifth_recording(env, plain_wav):
    join_all(env, CAN)
    env.voice(CAN, plain_wav)
    assert any("Can</a> ilk kaydını attı!" in t for t in env.tg.sent())
    for back in range(1, 4):
        env.add_recording(2, back)
    env.voice(CAN, plain_wav)
    assert any("Can</a> 5. kaydını attı!" in t for t in env.tg.sent())
    labels = [b["text"] for row in env.main.milestone_keyboard()["keyboard"] for b in row]
    assert "İlk kayıt" in labels and "5. kayıt" in labels
