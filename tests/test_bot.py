import os
import datetime as dt
import re
from unittest import mock

import numpy as np
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
    assert env.main.note_text(rec) == "köprü kısmı"


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
    assert len(re.findall("🔥|❤|🃏|💔", emre_line)) == 20
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


def test_admin_assigns_celebration_per_milestone(env, plain_wav, monkeypatch):
    monkeypatch.setattr(env.main, "run_in_background", lambda fn, *a: fn(*a))
    monkeypatch.setattr(env.main, "SEND_GAP_SEC", 0)
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
    assert any("60 gün: – büyük emoji" in t for t in env.tg.sent(1))
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
    assert not {31, 69} & m.STREAK_MILESTONES and {31, 69} <= m.COUNT_MILESTONES
    labels = [b["text"] for row in m.milestone_keyboard(m.load_celebrations())["keyboard"] for b in row]
    assert {"31. kayıt", "69. kayıt"} <= set(labels) and not {"31 gün", "69 gün"} & set(labels)


def test_milestone_list(env):
    labels = [b["text"] for row in env.main.milestone_keyboard(env.main.load_celebrations())["keyboard"] for b in row]
    assert labels == ["7 gün", "14 gün", "21 gün", "30 gün", "60 gün", "365 gün", "İlk kayıt",
                      "5. kayıt", "31. kayıt", "50. kayıt", "69. kayıt", "100. kayıt", "İptal"]


def test_first_and_fifth_recording(env, plain_wav):
    join_all(env, CAN)
    env.voice(CAN, plain_wav)
    assert any("Can</a> ilk kaydını attı!" in t for t in env.tg.sent())
    for back in range(1, 4):
        env.add_recording(2, back)
    env.voice(CAN, plain_wav)
    assert any("Can</a> 5. kaydını attı!" in t for t in env.tg.sent())
    labels = [b["text"] for row in env.main.milestone_keyboard(env.main.load_celebrations())["keyboard"] for b in row]
    assert "İlk kayıt" in labels and "5. kayıt" in labels


def test_calendar_marks_joker_days(env, monkeypatch):
    monkeypatch.setattr(env.main, "today", lambda: dt.date(2026, 9, 30))
    join_all(env, EMRE)
    env.fs.store["members"]["1"]["first_day"] = "2026-09-14"
    for back in range(0, 17):
        if back not in (8, 9):  # Tue 22nd and Mon 21st, same week
            env.add_recording(1, back)
    env.client.get("/?t=tok")
    page = env.client.get("/?m=2026-09").data.decode()
    assert page.count("🃏 Joker: Emre") == 1 and page.count("Kaydetmedi: Emre") == 1
    assert 'class="dot joker"' in page


# ---------- tempo without metronome ----------

def test_tempo_estimated_for_plain_take(env, plain_wav):
    has, bpm, _, tempo = env.main.metronome.analyze(plain_wav)
    assert not has and bpm is None and tempo == 80


def test_no_tempo_when_metronome_present(env, metro_wav):
    assert env.main.metronome.analyze(metro_wav)[3] is None


def test_no_tempo_for_free_playing(env):
    m = env.main.metronome
    rng = np.random.default_rng(3)
    sr = m.SR
    y = np.zeros(sr * 40)
    t = 0.3
    while t < 39:
        n = int(0.4 * sr)
        tt = np.arange(n) / sr
        note = np.sin(2 * np.pi * rng.choice([196, 247, 330]) * tt) * np.exp(-tt / 0.2)
        i = int(t * sr)
        y[i:i + n] += note[:len(y) - i]
        t += rng.exponential(0.35) + 0.05
    y += 0.005 * rng.standard_normal(len(y))
    assert m.estimate_tempo((y / np.abs(y).max()).astype(np.float32)) is None


def test_tempo_shown_in_calendar_but_not_counted(env, plain_wav):
    join_all(env, EMRE)
    env.voice(EMRE, plain_wav)
    (rec,) = env.fs.store["recordings"].values()
    assert rec["tempo"] == 80 and not rec["metronome"]
    assert env.tg.reactions()[0][0]["emoji"] == "❤"
    env.client.get("/?t=tok")
    assert "♩ ~80 bpm" in env.client.get("/").data.decode()


def test_reply_from_others_marks_listened(env, plain_wav):
    join_all(env, EMRE, CAN, DENIZ)
    vid = env.voice(EMRE, plain_wav)
    rec_id = f"-1001_{vid}"
    target = {"message_id": vid, "from": EMRE, "voice": {"file_id": "f"}}
    env.message(EMRE, text="kendi notum", reply_to_message=target)
    env.command(DENIZ, "/sil", reply_to_message=target)
    assert "listeners" not in env.fs.store["recordings"][rec_id]
    env.message(CAN, text="çok iyi olmuş", reply_to_message=target)
    env.tg.next_file = plain_wav
    env.message(DENIZ, voice={"file_id": "g", "duration": 20}, reply_to_message=target)
    rec = env.fs.store["recordings"][rec_id]
    assert sorted(rec["listeners"]) == ["2", "3"] and env.main.note_text(rec) == "kendi notum"
    assert len(env.fs.store["recordings"]) == 2


def test_gif_and_sticker_not_saved(env, plain_wav):
    join_all(env, EMRE)
    gif = {"file_id": "a", "file_unique_id": "ua", "mime_type": "video/mp4", "file_name": "funny.gif.mp4", "duration": 3}
    env.message(EMRE, animation=gif, document=dict(gif))
    env.message(EMRE, sticker={"file_id": "s", "file_unique_id": "us", "is_video": True})
    assert not env.fs.store.get("recordings") and not env.tg.reactions()


def test_admin_can_delete_any_recording(env, plain_wav):
    make_admin(env)
    join_all(env, CAN)
    vid = env.voice(CAN, plain_wav)
    reply = {"message_id": vid, "from": CAN, "voice": {"file_id": "f"}}
    env.command(DENIZ, "/sil", reply_to_message=reply)
    assert len(env.fs.store["recordings"]) == 1
    env.command(EMRE, "/sil", reply_to_message=reply)
    assert not env.fs.store["recordings"] and not env.bucket.data


# ---------- real-world recording formats ----------

def to_phone_mp4(wav):
    """MP4 as phones write it: index (moov) at the end, so it cannot be read from a pipe."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        src, dst = f"{d}/in.wav", f"{d}/out.mp4"
        open(src, "wb").write(wav)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=10",
                        "-i", src, "-shortest", "-c:v", "libx264", "-c:a", "aac", dst], check=True)
        return open(dst, "rb").read()


def test_phone_video_is_analyzed(env, metro_wav):
    mp4 = to_phone_mp4(metro_wav)
    assert mp4.find(b"moov") > mp4.find(b"mdat")
    has, bpm, duration, _ = env.main.metronome.analyze(mp4)
    assert has and bpm == 92 and duration >= 19


def test_metronome_with_subdivision_clicks(env):
    import io
    import wave
    m = env.main.metronome
    sr = 16000
    rng = np.random.default_rng(5)
    y = np.zeros(sr * 30)
    n = int(0.012 * sr)
    t = np.arange(n) / sr
    click = np.sin(2 * np.pi * 2500 * t) * np.exp(-t / 0.002)
    beat = 60 / 124
    k = 0
    while k * beat / 2 + 0.3 < 29:
        i = int((k * beat / 2 + 0.3) * sr)
        y[i:i + n] += click * (0.25 if k % 2 == 0 else 0.12)
        k += 1
    y += 0.3 * wave_noise(rng, len(y), sr)
    y /= np.abs(y).max() * 1.1
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype(np.int16).tobytes())
    has, bpm, _, _ = m.analyze(buf.getvalue())
    assert has and bpm == 124


def wave_noise(rng, n, sr):
    """Plucked notes on eighths with human timing, as a backing for click tests."""
    y = np.zeros(n)
    step = 60 / 124 / 2
    k = 0
    while k * step + 0.3 < n / sr - 1:
        f = rng.choice([196, 247, 294])
        m = int(0.4 * sr)
        tt = np.arange(m) / sr
        note = np.sin(2 * np.pi * f * tt) * np.exp(-tt / 0.2)
        i = max(0, int((k * step + 0.3 + rng.normal(0, 0.02)) * sr))
        y[i:i + m] += note[:n - i]
        k += 1
    return y


def test_sil_deletes_telegram_messages(env, plain_wav):
    join_all(env, EMRE)
    vid = env.voice(EMRE, plain_wav)
    cmd_id = env.command(EMRE, "/sil", reply_to_message={"message_id": vid, "from": EMRE, "voice": {"file_id": "f"}})
    deleted = [p["message_id"] for m, p in env.tg.calls if m == "deleteMessage"]
    assert deleted == [vid, cmd_id]
    assert not any("silindi" in s for s in env.tg.sent())


def test_sil_old_message_falls_back_to_notice(env, plain_wav):
    join_all(env, EMRE)
    vid = env.voice(EMRE, plain_wav)
    env.tg.fail.add("deleteMessage")
    env.command(EMRE, "/sil", reply_to_message={"message_id": vid, "from": EMRE, "voice": {"file_id": "f"}})
    assert not env.fs.store["recordings"]
    assert "elle silebilirsin" in env.tg.sent()[-1]
    assert not [p for m, p in env.tg.calls if m == "sendMessage" and "⚠️" in p["text"]]


# ---------- steady clicks that drift (mechanical metronome) ----------

def drifting_click_take(seconds=36, drift=0.03, click_jitter=0.003, play_jitter=0.02, seed=7):
    import io
    import wave
    sr = 16000
    rng = np.random.default_rng(seed)
    y = np.zeros(sr * (seconds + 1))
    n = int(0.01 * sr)
    tt = np.arange(n) / sr
    click = rng.standard_normal(n) * np.exp(-tt / 0.0015)
    t = 0.4
    while t < seconds:
        period = 1.0 * (1 + drift * np.sin(2 * np.pi * t / 30))
        i = int((t + rng.normal(0, click_jitter)) * sr)
        y[i:i + n] += 0.5 * click
        # Notes on every beat and in between, as a player following the click
        for sub in (0, 0.25, 0.5, 0.75):
            m = int(0.3 * sr)
            nt = np.arange(m) / sr
            note = np.sin(2 * np.pi * rng.choice([196, 247, 294]) * nt) * np.exp(-nt / 0.15)
            note[:80] *= np.linspace(0, 1, 80)  # soft attack, unlike the click
            j = int((t + sub * period + rng.normal(0, play_jitter)) * sr)
            y[j:j + m] += 0.6 * note[:len(y) - j]
        t += period
    y /= np.abs(y).max() * 1.1
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def test_drifting_mechanical_metronome_detected(env):
    has, bpm, _, _ = env.main.metronome.analyze(drifting_click_take())
    assert has and 58 <= bpm <= 62


def test_loose_human_playing_not_metronome(env):
    m = env.main.metronome
    assert not any(m.detect(wav_bytes(seed=s, seconds=36))[0] for s in range(3))


# ---------- files above 20 MB ----------

def test_large_file_downloaded_compressed_and_saved(env, metro_wav, monkeypatch, tmp_path):
    m = env.main
    big_wav = metro_wav * 1  # analysis input; size is faked below
    monkeypatch.setattr(m.bigfiles, "available", lambda: True)
    monkeypatch.setattr(m, "run_in_background", lambda fn, *a: fn(*a))
    calls = []

    class FakeDownloader:
        def download(self, chat_id, message_id, path):
            calls.append((chat_id, message_id))
            open(path, "wb").write(big_wav)
            return path

    monkeypatch.setattr(m, "downloader", lambda: FakeDownloader())
    monkeypatch.setattr(m.bigfiles, "COMPRESS_ABOVE", 1000)
    join_all(env, EMRE)
    env.message(EMRE, document={"file_id": "d", "file_name": "take.wav", "mime_type": "audio/wav",
                                "file_size": 300 * 1024 * 1024})
    (rec,) = env.fs.store["recordings"].values()
    assert calls and rec["mime"] == "audio/ogg" and rec["gcs_path"].endswith(".ogg")
    assert rec["metronome"] and rec["bpm"] == 92
    assert [r[0]["emoji"] for r in env.tg.reactions() if r] == ["👀", "🔥"]
    assert not env.fs.store.get("processing")


def test_large_file_failure_reports_and_cleans_up(env, monkeypatch):
    m = env.main
    monkeypatch.setattr(m.bigfiles, "available", lambda: True)
    monkeypatch.setattr(m, "run_in_background", lambda fn, *a: fn(*a))

    class Broken:
        def download(self, *a):
            raise RuntimeError("network")

    monkeypatch.setattr(m, "downloader", lambda: Broken())
    join_all(env, EMRE)
    env.message(EMRE, video={"file_id": "v", "mime_type": "video/mp4", "file_size": 50 * 1024 * 1024})
    assert not env.fs.store.get("recordings") and not env.fs.store.get("processing")
    assert "büyük kaydı kaydedemedim" in env.tg.sent()[-1]


def test_over_one_gb_rejected(env, monkeypatch):
    monkeypatch.setattr(env.main.bigfiles, "available", lambda: True)
    join_all(env, EMRE)
    env.message(EMRE, video={"file_id": "v", "mime_type": "video/mp4", "file_size": 2 * 1024 ** 3})
    assert "1 GB" in env.tg.sent()[-1]


def test_compress_video_to_720p(env, metro_wav, tmp_path, monkeypatch):
    import subprocess
    src = tmp_path / "in.mp4"
    wav = tmp_path / "a.wav"
    wav.write_bytes(metro_wav)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30", "-i", str(wav),
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "5", "-c:a", "aac", str(src)],
                   check=True)
    monkeypatch.setattr(env.main.bigfiles, "COMPRESS_ABOVE", 0)
    path, mime, ext = env.main.bigfiles.compress(str(src), "video/mp4")
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    assert mime == "video/mp4" and int(out) == 720
    assert env.main.metronome.analyze(open(path, "rb").read())[:2] == (True, 92)


def test_file_name_shown_in_calendar(env, plain_wav):
    join_all(env, EMRE)
    env.tg.next_file = plain_wav
    env.message(EMRE, document={"file_id": "d", "file_name": "time_solo.mp3", "mime_type": "audio/mpeg",
                                "file_size": len(plain_wav)})
    env.voice(EMRE, plain_wav)
    names = sorted(r["file_name"] for r in env.fs.store["recordings"].values())
    assert names == ["", "time_solo.mp3"]
    env.client.get("/?t=tok")
    page = env.client.get("/").data.decode()
    assert page.count("📄 time_solo.mp3") == 1 and page.count('class="fname"') == 1


def test_steady_passage_in_long_take_is_not_metronome(env):
    """A short steady stretch inside a long free take should not count as a metronome."""
    import io
    import wave
    m = env.main.metronome
    steady = np.frombuffer(drifting_click_take(seconds=20)[44:], dtype=np.int16).astype(float)
    rng = np.random.default_rng(9)
    sr = 16000
    free = np.zeros(sr * 60)
    t = 0.3
    while t < 59:
        n = int(0.4 * sr)
        tt = np.arange(n) / sr
        note = np.sin(2 * np.pi * rng.choice([196, 247, 330]) * tt) * np.exp(-tt / 0.2)
        note[:80] *= np.linspace(0, 1, 80)
        i = int(t * sr)
        free[i:i + n] += 8000 * note[:len(free) - i]
        t += rng.exponential(0.4) + 0.05
    y = np.concatenate([free[:sr * 30], steady, free[sr * 30:]])
    y = y / np.abs(y).max() * 0.9
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype(np.int16).tobytes())
    assert not m.detect(buf.getvalue())[0]


# ---------- half tempo when the pulse found is the note rate ----------

def pulse_env(bpm, seconds=40, accent=1.0, seed=0):
    m = __import__("metronome")
    rng = np.random.default_rng(seed)
    env = np.abs(rng.normal(0, 0.05, int(seconds * m.FPS)))
    period = 60 * m.FPS / bpm
    for k, pos in enumerate(np.arange(5, len(env) - 5, period)):
        env[int(round(pos))] += accent if k % 2 == 0 else 1.0
    return env


def test_accented_pulse_is_halved(env):
    m = env.main.metronome
    assert m.beat_tempo(pulse_env(120, accent=2.0), 120) == 60


def test_even_pulse_kept_in_normal_range(env):
    m = env.main.metronome
    assert m.beat_tempo(pulse_env(90), 90) == 90
    assert m.beat_tempo(pulse_env(100), 100) == 100


def test_fast_even_pulse_prefers_slower_beat(env):
    m = env.main.metronome
    assert m.beat_tempo(pulse_env(136), 136) == 68


def test_never_halved_below_minimum(env):
    m = env.main.metronome
    assert m.beat_tempo(pulse_env(80, accent=2.0), 80) == 80


def test_clicks_heard_only_where_playing_pauses(env):
    """Strumming covers the clicks; a few seconds of clicks alone are enough."""
    import io
    import wave
    sr = 16000
    rng = np.random.default_rng(11)
    period = 60 / 95
    y = np.zeros(sr * 27)
    n = int(0.01 * sr)
    tt = np.arange(n) / sr
    click = rng.standard_normal(n) * np.exp(-tt / 0.0015)
    t = 0.5
    while t < 26:
        i = int(t * sr)
        y[i:i + n] += 0.15 * click
        if t < 20:
            # A loud strum near each beat and between beats, with human timing
            for sub in (0, 0.5):
                m = int(0.3 * sr)
                st = np.arange(m) / sr
                strum = rng.standard_normal(m) * np.exp(-st / 0.08)
                j = int((t + sub * period + rng.normal(0, 0.02)) * sr)
                y[j:j + m] += 0.8 * strum[:len(y) - j]
        t += period
    y /= np.abs(y).max() * 1.1
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype(np.int16).tobytes())
    has, bpm, _ = env.main.metronome.detect(buf.getvalue())
    assert has and bpm == 95


def test_rate_limited_call_is_retried(env, monkeypatch):
    monkeypatch.setattr(env.main.time, "sleep", lambda s: None)
    orig, hits = env.tg.post, []

    def post(url, json=None, timeout=None):
        hits.append(url)
        if len(hits) == 1:
            return mock.Mock(json=lambda: {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}})
        return orig(url, json=json, timeout=timeout)

    monkeypatch.setattr(env.main.requests, "post", post)
    assert env.main.tg("sendMessage", chat_id=1, text="x") is True and len(hits) == 2


def test_celebration_list_labels_gifs(env, monkeypatch):
    monkeypatch.setattr(env.main, "run_in_background", lambda fn, *a: fn(*a))
    monkeypatch.setattr(env.main, "SEND_GAP_SEC", 0)
    make_admin(env)
    private = {"id": 1, "type": "private"}
    env.message(EMRE, chat=private, animation={"file_id": "G7", "file_unique_id": "u7"})
    env.message(EMRE, chat=private, text="7 gün")
    env.message(EMRE, chat=private, text="/kutlamalar")
    assert ("sendAnimation", {"chat_id": 1, "animation": "G7", "caption": "7 gün"}) in env.tg.calls


def test_celebration_removed_from_one_milestone_and_no_duplicates(env):
    make_admin(env)
    private = {"id": 1, "type": "private"}
    gif = {"file_id": "G", "file_unique_id": "uG"}
    env.fs.store.setdefault("config", {})["celebrations"] = {
        "assigned": {"streak7": {"type": "animation", "file_id": "G", "uid": "uG"},
                     "count50": {"type": "animation", "file_id": "G", "uid": "uG"}},
        "pending": None}
    env.message(EMRE, chat=private, text="/sil",
                reply_to_message={"message_id": 9, "animation": gif, "caption": "50. kayıt"})
    assigned = env.fs.store["config"]["celebrations"]["assigned"]
    assert "count50" not in assigned and "streak7" in assigned

    env.message(EMRE, chat=private, animation=gif)
    assert "7 gün kutlamasında kullanılıyor" in env.tg.sent(1)[-1]
    assert env.fs.store["config"]["celebrations"]["pending"] is None


def test_admin_adds_and_removes_milestones(env, plain_wav):
    make_admin(env)
    private = {"id": 1, "type": "private"}
    env.message(CAN, chat={"id": 2, "type": "private"}, text="/ekle 3 gün")
    assert "sadece yönetici" in env.tg.sent(2)[-1]
    env.message(EMRE, chat=private, text="/ekle 3 gün")
    assert "3 gün eklendi" in env.tg.sent(1)[-1]
    env.message(EMRE, chat=private, text="/çıkar 7 gün")
    env.message(EMRE, chat=private, text="/ekle 2 kayıt")
    env.message(EMRE, chat=private, text="/ekle kırk")
    assert "Örnek" in env.tg.sent(1)[-1]
    m = env.main
    labels = [b["text"] for row in m.milestone_keyboard(m.load_celebrations())["keyboard"] for b in row]
    assert "3 gün" in labels and "7 gün" not in labels and "2. kayıt" in labels

    env.set_first_day(1, 10)
    for back in range(1, 3):
        env.add_recording(1, back)
    env.voice(EMRE, plain_wav)
    assert any("3 günlük seriye" in t for t in env.tg.sent())


SPEECH = open(os.path.join(os.path.dirname(__file__), "data", "speech.ogg"), "rb").read()


def test_talk_only_voice_message_is_not_practice(env, plain_wav):
    import music
    assert not music.has_music(SPEECH) and music.has_music(plain_wav)
    vid = env.voice(EMRE, SPEECH)
    assert f"-1001_{vid}" not in env.fs.store.get("recordings", {})
    assert "1" not in env.fs.store.get("members", {})
    assert not [p for m, p in env.tg.calls if m == "setMessageReaction"]


def test_force_save_by_owner_only(env):
    vid = env.voice(EMRE, SPEECH)
    env.command(CAN, "/kaydet", reply_to_message={"message_id": vid, "from": EMRE, "chat": env.CHAT,
                                                 "date": int(__import__("time").time()), "voice": {"file_id": "f", "duration": 12}})
    assert "Sadece kendi kaydını" in env.tg.sent()[-1]
    env.command(EMRE, "/kaydet", reply_to_message={"message_id": vid, "from": EMRE, "chat": env.CHAT,
                                                  "date": int(__import__("time").time()), "voice": {"file_id": "f", "duration": 12}})
    assert env.fs.store["recordings"][f"-1001_{vid}"]["user_id"] == "1"
    assert ("setMessageReaction", {"chat_id": -1001, "message_id": vid,
                                   "reaction": [{"type": "emoji", "emoji": "❤"}]}) in env.tg.calls


def test_cikar_drops_recording_but_keeps_message(env, plain_wav):
    join_all(env, EMRE, CAN)
    vid = env.voice(EMRE, plain_wav)
    env.command(CAN, "/cikar", reply_to_message={"message_id": vid})
    assert "Sadece kendi" in env.tg.sent()[-1]
    env.command(EMRE, "/çıkar", reply_to_message={"message_id": vid})
    assert f"-1001_{vid}" not in env.fs.store["recordings"]
    assert "Pratikten çıkarıldı" in env.tg.sent()[-1]
    assert not [p for m, p in env.tg.calls if m == "deleteMessage"]


def test_chat_mode_skips_recordings_until_ended(env, plain_wav):
    join_all(env, EMRE)
    env.command(EMRE, "/sohbet")
    assert "Sohbet modu" in env.tg.sent()[-1]
    vid = env.voice(EMRE, plain_wav)
    assert f"-1001_{vid}" not in env.fs.store.get("recordings", {})
    env.command(EMRE, "/pratik")
    vid = env.voice(EMRE, plain_wav)
    assert f"-1001_{vid}" in env.fs.store["recordings"]


def test_chat_mode_ends_at_day_start(env, plain_wav):
    join_all(env, EMRE)
    env.command(EMRE, "/sohbet")
    until = env.fs.store["members"]["1"]["chat_until"]
    assert until.hour == env.main.DAY_START_HOUR and until > env.main.dt.datetime.now(env.main.TZ)
    env.fs.store["members"]["1"]["chat_until"] = env.main.dt.datetime.now(env.main.TZ) - env.main.dt.timedelta(minutes=1)
    vid = env.voice(EMRE, plain_wav)
    assert f"-1001_{vid}" in env.fs.store["recordings"]


def test_chat_mode_repeated_commands(env):
    join_all(env, EMRE)
    env.command(EMRE, "/pratik")
    assert "Zaten pratik modundasın" in env.tg.sent()[-1]
    env.command(EMRE, "/sohbet")
    env.command(EMRE, "/sohbet")
    assert "Zaten sohbet modundasın" in env.tg.sent()[-1]


def test_evening_day_start(env, monkeypatch):
    m = env.main
    monkeypatch.setattr(m, "DAY_OFFSET", -1)
    monkeypatch.setattr(m, "DAY_START_HOUR", 23)
    at = lambda d, h, mi=0: dt.datetime(2026, 10, d, h, mi, tzinfo=m.TZ)
    assert m.practice_day(at(4, 22, 59)) == dt.date(2026, 10, 4)
    assert m.practice_day(at(4, 23, 0)) == dt.date(2026, 10, 5)
    assert m.practice_day(at(5, 3, 0)) == dt.date(2026, 10, 5)
    monkeypatch.setattr(m, "now_local", lambda: at(4, 23, 0))
    monkeypatch.setattr(m.dt, "datetime", type("D", (dt.datetime,), {"now": staticmethod(lambda tz=None: at(4, 22, 0))}))
    assert m.next_day_start() == at(4, 23, 0)


def test_atesle_only_reacts(env):
    env.command(EMRE, "/ateşle", reply_to_message={"message_id": 77})
    assert ("setMessageReaction", {"chat_id": -1001, "message_id": 77,
                                   "reaction": [{"type": "emoji", "emoji": "🔥"}]}) in env.tg.calls
    assert not env.fs.store.get("recordings")


def test_fun_commands(env):
    env.command(EMRE, "/alkış", reply_to_message={"message_id": 5})
    assert ("setMessageReaction", {"chat_id": -1001, "message_id": 5,
                                   "reaction": [{"type": "emoji", "emoji": "👏"}]}) in env.tg.calls
    env.command(EMRE, "/zar")
    assert ("sendDice", {"chat_id": -1001, "emoji": "🎲"}) in env.tg.calls
    env.command(EMRE, "/ilham")
    assert env.tg.sent()[-1].startswith("💡 ")


def test_time_badges(env):
    m = env.main
    at = lambda h: dt.datetime(2026, 10, 4, h, 30, tzinfo=m.TZ)
    badge = m.real_time_badge
    assert badge(at(2)) in m.NIGHT_EMOJI and badge(at(9)) in m.MORNING_EMOJI and badge(at(10)) is None


def test_time_badge_sent_as_message(env, plain_wav, monkeypatch):
    monkeypatch.setattr(env.main, "time_badge", lambda ts: "🦉")
    vid = env.voice(EMRE, plain_wav)
    assert env.tg.sent()[-1] == "🦉" or "🦉" in env.tg.sent()
    assert ("setMessageReaction", {"chat_id": -1001, "message_id": vid,
                                   "reaction": [{"type": "emoji", "emoji": "❤"}]}) in env.tg.calls


def test_song_parsing(env):
    so = env.main.song_of
    a = so({"file_name": "Jamiroquai_-_Dont_Give_Hate_a_Chance.mp3", "caption": "2. nakarata kadar"})
    b = so({"file_name": "jamiroquai - dont give hate a chance 2.mp3", "caption": ""})
    c = so({"file_name": "Dont Give Hate a Chance take3.m4a", "caption": "tamamı"})
    assert a[0] == "Jamiroquai - Dont Give Hate a Chance" and a[2] == "2. nakarata kadar" and not a[3]
    assert a[1] == b[1] and c[3]
    assert not so({"file_name": "Full Moon.mp3"})[3]
    full = so({"file_name": "Little Black Submarines FULL.mp3"})
    assert full[3] and full[0] == "Little Black Submarines"
    w = so({"caption": "Wonderwall\nintro + 1. kıta"})
    assert w[0] == "Wonderwall" and w[2] == "intro + 1. kıta" and not w[3]
    assert so({"caption": "Wonderwall\nbaştan sona çaldım"})[3]
    assert so({"caption": "Wonderwall\nComplete"})[3]
    assert so({"caption": "Yesterday ✅"})[3]
    new = so({"file_name": "261004_goodbye-stranger.mp3", "caption": "tamami"})
    old = so({"file_name": "Goodbye Stranger.mp3", "caption": "intro"})
    assert new[0] == "goodbye stranger" and new[1] == old[1] and new[3]
    assert so({"file_name": "2026-10-04 Wonderwall.m4a"})[0] == "Wonderwall"
    assert so({}) is None


def test_open_songs_command(env, plain_wav):
    join_all(env, EMRE, CAN)
    env.tg.next_file = plain_wav
    env.message(EMRE, audio={"file_id": "f", "duration": 20, "file_name": "Wonderwall.mp3"}, caption="intro")
    env.tg.next_file = plain_wav
    last = env.message(EMRE, audio={"file_id": "f", "duration": 20, "file_name": "wonderwall_2.mp3"}, caption="1. kıtaya kadar")
    env.tg.next_file = plain_wav
    env.message(EMRE, audio={"file_id": "f", "duration": 20, "file_name": "Yesterday.mp3"}, caption="full")
    env.tg.next_file = plain_wav
    env.message(CAN, audio={"file_id": "f", "duration": 20, "file_name": "Creep.mp3"})
    env.command(EMRE, "/şarkılarım")
    out = env.tg.sent()[-1]
    assert "Wonderwall — 1. kıtaya kadar" in out and "Yesterday" not in out and "Creep" not in out
    env.command(CAN, "/bitti", reply_to_message={"message_id": last})
    assert "Sadece kendi" in env.tg.sent()[-1]
    env.command(EMRE, "/bitti", reply_to_message={"message_id": last})
    env.command(EMRE, "/sarkilarim")
    assert "Bitmeyen şarkın yok" in env.tg.sent()[-1]


def test_same_file_twice_same_day_recorded_once(env, plain_wav):
    join_all(env, EMRE)
    audio = {"file_id": "f", "file_unique_id": "U1", "duration": 20, "file_name": "Cheat.m4a"}
    env.tg.next_file = plain_wav
    a = env.message(EMRE, audio=dict(audio))
    env.tg.next_file = plain_wav
    b = env.message(EMRE, audio=dict(audio))
    recs = env.fs.store["recordings"]
    assert f"-1001_{a}" in recs and f"-1001_{b}" not in recs
    env.tg.next_file = plain_wav
    c = env.message(EMRE, audio={**audio, "file_unique_id": "U2"})
    assert f"-1001_{c}" in env.fs.store["recordings"]


def test_notes_edit_and_delete(env, plain_wav):
    join_all(env, EMRE, CAN)
    vid = env.voice(EMRE, plain_wav, caption="Wonderwall")
    rid = f"-1001_{vid}"
    n1 = env.message(EMRE, text="intro", reply_to_message={"message_id": vid, "from": EMRE, "voice": {"file_id": "f"}})
    n2 = env.message(EMRE, text="yanlış not", reply_to_message={"message_id": vid, "from": EMRE, "voice": {"file_id": "f"}})
    assert env.main.note_text(env.fs.store["recordings"][rid]) == "Wonderwall\nintro\nyanlış not"

    env.main.handle_update({"edited_message": {"message_id": n1, "chat": env.CHAT, "from": EMRE, "text": "1. kıta"}})
    env.command(CAN, "/notsil", reply_to_message={"message_id": n2})
    assert "Sadece kendi" in env.tg.sent()[-1]
    env.command(EMRE, "/notsil", reply_to_message={"message_id": n2})
    assert env.main.note_text(env.fs.store["recordings"][rid]) == "Wonderwall\n1. kıta"

    # Removing the caption in Telegram clears it
    env.main.handle_update({"edited_message": {"message_id": vid, "chat": env.CHAT, "from": EMRE, "voice": {"file_id": "f"}}})
    assert env.main.note_text(env.fs.store["recordings"][rid]) == "1. kıta"

    env.command(EMRE, "/notsil", reply_to_message={"message_id": vid})
    assert env.main.note_text(env.fs.store["recordings"][rid]) == ""


def test_getfile_timeout_is_retried(env, plain_wav, monkeypatch):
    import requests
    monkeypatch.setattr(env.main.time, "sleep", lambda s: None)
    orig, failed = env.tg.post, []

    def post(url, json=None, timeout=None):
        if url.endswith("/getFile") and not failed:
            failed.append(1)
            raise requests.ReadTimeout("read timed out")
        if url.endswith("/sendMessage") and json and json.get("text") == "boom":
            raise requests.ReadTimeout("read timed out")
        return orig(url, json=json, timeout=timeout)

    monkeypatch.setattr(env.main.requests, "post", post)
    join_all(env, EMRE)
    vid = env.voice(EMRE, plain_wav)
    assert f"-1001_{vid}" in env.fs.store["recordings"]
    # A send that timed out may have arrived, so it is not repeated
    calls = []
    monkeypatch.setattr(env.main.requests, "post", lambda url, json=None, timeout=None: (calls.append(url), post(url, json, timeout))[1])
    assert env.main.tg("sendMessage", chat_id=1, text="boom") is None
    assert sum(u.endswith("/sendMessage") for u in calls) == 1


def test_redelivered_update_during_processing_is_skipped(env, plain_wav):
    join_all(env, EMRE)
    env.fs.store.setdefault("processing", {})["-1001_99"] = {"started": dt.datetime.now(env.main.TZ)}
    env.tg.next_file = plain_wav
    env.main.handle_update({"message": {"message_id": 99, "chat": env.CHAT, "from": EMRE,
                                        "date": int(__import__("time").time()),
                                        "voice": {"file_id": "f", "duration": 20}}})
    assert "-1001_99" not in env.fs.store.get("recordings", {})
    del env.fs.store["processing"]["-1001_99"]
    env.main.handle_update({"message": {"message_id": 99, "chat": env.CHAT, "from": EMRE,
                                        "date": int(__import__("time").time()),
                                        "voice": {"file_id": "f", "duration": 20}}})
    assert "-1001_99" in env.fs.store["recordings"] and "-1001_99" not in env.fs.store["processing"]


def test_rehearsal_poll(env):
    env.command(EMRE, "/prova bitir")
    assert "Açık bir prova anketi yok" in env.tg.sent()[-1]
    env.command(EMRE, "/prova")
    polls = [p for m, p in env.tg.calls if m == "sendPoll"]
    assert polls and len(polls[0]["options"]) == 7 and polls[0]["allows_multiple_answers"]
    env.command(CAN, "/prova")
    assert "Zaten açık" in env.tg.sent()[-1]
    days = env.fs.store["config"]["state"]["rehearsal_poll"]["days"]
    env.tg.results = {"stopPoll": {"options": [{"voter_count": v} for v in (1, 3, 0, 3, 0, 0, 0)]}}
    env.command(EMRE, "/prova bitir")
    assert "Prova günü" in env.tg.sent()[-1] and "3 kişi" in env.tg.sent()[-1]
    assert env.fs.store["config"]["state"]["rehearsals"] == [days[1]]
    assert ("pinChatMessage", {"chat_id": -1001, "message_id": 500, "disable_notification": True}) in env.tg.calls
    assert ("unpinChatMessage", {"chat_id": -1001, "message_id": 500}) in env.tg.calls
    env.client.get("/?t=tok")
    assert "🎸" in env.client.get("/?m=" + days[1][:7]).data.decode()


def test_rehearsal_includes_today_before_1730(env, monkeypatch):
    m = env.main
    monkeypatch.setattr(m, "now_local", lambda: dt.datetime(2026, 10, 8, 17, 0, tzinfo=m.TZ))
    env.command(EMRE, "/prova")
    days = env.fs.store["config"]["state"]["rehearsal_poll"]["days"]
    assert days[0] == "2026-10-08" and len(days) == 7
    env.fs.store["config"]["state"]["rehearsal_poll"] = None
    monkeypatch.setattr(m, "now_local", lambda: dt.datetime(2026, 10, 8, 17, 30, tzinfo=m.TZ))
    env.command(EMRE, "/prova")
    assert env.fs.store["config"]["state"]["rehearsal_poll"]["days"][0] == "2026-10-09"


def test_manual_metronome_override(env, plain_wav):
    join_all(env, EMRE, CAN)
    vid = env.voice(EMRE, plain_wav)
    rid = f"-1001_{vid}"
    env.command(CAN, "/metronomvar", reply_to_message={"message_id": vid})
    assert "Sadece kendi" in env.tg.sent()[-1]
    env.command(EMRE, "/metronomvar 120", reply_to_message={"message_id": vid})
    rec = env.fs.store["recordings"][rid]
    assert rec["metronome"] and rec["bpm"] == 120 and "120 bpm" in env.tg.sent()[-1]
    assert ("setMessageReaction", {"chat_id": -1001, "message_id": vid,
                                   "reaction": [{"type": "emoji", "emoji": "🔥"}]}) in env.tg.calls
    env.command(EMRE, "/metronomyok", reply_to_message={"message_id": vid})
    rec = env.fs.store["recordings"][rid]
    assert not rec["metronome"] and rec["bpm"] is None


def test_reminder_lists_poll_non_voters(env):
    join_all(env, EMRE, CAN)
    env.tg.results = {"sendPoll": {"message_id": 500, "poll": {"id": "P1"}}}
    env.command(EMRE, "/prova")
    env.main.handle_update({"poll_answer": {"poll_id": "P1", "user": EMRE, "option_ids": [0, 2]}})
    env.main.handle_update({"poll_answer": {"poll_id": "P1", "user": CAN, "option_ids": [1]}})
    env.main.handle_update({"poll_answer": {"poll_id": "P1", "user": CAN, "option_ids": []}})
    env.client.post("/cron/reminder", headers={"X-Cron-Secret": "cron"})
    out = env.tg.sent()[-1]
    assert "Prova anketine</a> oy vermeyenler" in out and "Can</a>" in out.split("oy vermeyenler")[1]
    assert "Emre</a>" not in out.split("oy vermeyenler")[1]
    assert "https://t.me/c/" not in out or "/500" in out


def test_rehearsal_announces_top_cover_song(env, monkeypatch):
    from unittest import mock as _m
    data = {"songs": [{"id": "a", "title": "Creep", "artist": "Radiohead", "createdAt": 1},
                      {"id": "b", "title": "Goodbye Stranger", "artist": "Supertramp", "createdAt": 2}],
            "votes": {"a|u1": 1, "a|u2": 0, "b|u1": 1, "b|u2": 1, "b|u3": 0}}
    monkeypatch.setattr(env.main.requests, "get",
                        lambda url, **kw: _m.Mock(json=lambda: data, raise_for_status=lambda: None))
    env.command(EMRE, "/prova")
    env.tg.results = {"stopPoll": {"options": [{"voter_count": 2}] + [{"voter_count": 0}] * 6}}
    env.command(EMRE, "/prova bitir")
    out = env.tg.sent()[-1]
    assert "Çalınacak: <b>Goodbye Stranger – Supertramp</b> (2 oy)" in out
    day = env.fs.store["config"]["state"]["rehearsals"][0]
    assert env.fs.store["config"]["state"]["rehearsal_songs"][day] == "Goodbye Stranger – Supertramp"


def test_rehearsal_without_cover_app(env, monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(env.main.requests, "get", boom)
    env.command(EMRE, "/prova")
    env.tg.results = {"stopPoll": {"options": [{"voter_count": 1}] + [{"voter_count": 0}] * 6}}
    env.command(EMRE, "/prova bitir")
    assert "Prova günü" in env.tg.sent()[-1] and "Çalınacak" not in env.tg.sent()[-1]
