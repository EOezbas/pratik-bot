# Pratik Zinciri

Arkadaş grubunun günlük müzik pratiği alışkanlığı için bir Telegram botu. Gruba atılan sesli mesajları ve videoları Google Cloud'a kaydeder, kimin hangi gün çalıştığını takip eder, kayıtta metronom olup olmadığını algılar ve tüm kayıtları bir takvim sayfasında gösterir.

## Özellikler

- **Kayıt:** Sesli mesaj, yuvarlak video, normal video ve "Dosya" olarak gönderilen ses ya da video dosyaları kaydedilir (en fazla 20 MB). Bot kaydı ❤ ile işaretler.
- **Metronom algılama:** Kayıtta duyulabilir bir metronom varsa bot 🔥 koyar ve BPM'i kaydeder. Metronomsuz kayıtlarda tempo çalınan notalardan tahmin edilir ve takvimde gri "♩ ~84 bpm" olarak gösterilir; tempo düzensizse (rubato, serbest çalım) gösterilmez. Tahmin bazen yarım ya da çift tempo bulabilir. Bu bilgi takibi etkilemez.
- **Seri ve joker:** Haftada (Pazartesi–Pazar) atlanan ilk gün 🃏 joker sayılır ve seriyi bozmaz.
- **Gün başlangıcı:** Gece 04:00'e kadar atılan kayıtlar önceki güne sayılır.
- **Hatırlatma:** Her gün 21:00'de o gün kayıt atmayanlar grupta etiketlenir.
- **Haftalık özet:** Pazar 21:30'da haftanın özeti ve "metronom ustası" gruba gönderilir.
- **Kilometre taşları:** Belirli seri ve kayıt sayılarına ulaşıldığında gruba kutlama mesajı gider.
- **Kim dinledi:** Bir kayda Telegram'da tepki bırakan ya da yanıt veren, ya da takvimde kim olduğunu seçip kaydı oynatan kişi, takvimde kaydın altında "👂 ... dinledi" olarak görünür. Tepkileri görebilmesi için bot grupta yönetici olmalı.
- **Kutlama sticker'ları:** Yönetici bota özelden sticker ya da GIF atar, çıkan butonlardan hangi kilometre taşı için olduğunu seçer. `/kutlamalar` atananları gösterir, birine yanıt verip `/sil` yazınca kaldırılır. Atanmamış kutlamalarda büyük animasyonlu emoji gider. Bunların hepsi sadece yöneticinin özel sohbetinde çalışır, grup göremez.
- **Hata uyarısı:** Bota özelden `/yonetici` yazan ilk grup üyesi yönetici olur; bot bir mesajı gönderemezse ya da bir hata olursa ona özelden haber verir.
- **Takvim:** Ay görünümü, kişi bazında kaydetti, metronomla kaydetti veya kaçırdı bilgisi, gün gün kayıtlar; kayıtlar dinlenebilir ve indirilebilir.

## Komutlar

| Komut | Açıklama |
|---|---|
| `/bugun` | Bugün kim kaydetti |
| `/seri` | Son 21 gün: 🔥 metronomlu, ❤ kaydetti, 🃏 joker, 💔 atladı |
| `/detay` | En uzun seri, joker durumu, bu ayki günler, metronomlu günler, toplam kayıt süresi |
| `/takvim` | Takvim sayfasının linki |
| `/katil` | Kayıt atmadan gruba katıl |
| `/ayril` | Hatırlatmalardan çık |
| `/sil` | Kendi kaydına yanıt olarak yazınca kayıt takvimden ve Telegram'dan silinir (Telegram'dan silme için bot "Mesajları sil" iznine sahip olmalı; 48 saatten eski mesajlar elle silinir); yönetici herkesin kaydını silebilir |
| `/yenilink` | Takvim linkini yeniler, eski link çalışmaz olur |
| `/yardim` | Nasıl çalışır |

Komutlar Türkçe karakterle de çalışır (`/bugün`, `/katıl`, `/yardım`).

## Mimari

```
Telegram grubu ──webhook──▶ Cloud Run (Flask, main.py)
                                │
                                ├── Cloud Storage   ses ve video dosyaları
                                ├── Firestore       üyeler ve kayıt bilgileri
                                └── metronome.py    ffmpeg + numpy/scipy ile metronom algılama

Cloud Scheduler ──▶ /cron/reminder  (her gün 21:00)
                ──▶ /cron/weekly    (Pazar 21:30)
```

Her şey `europe-west3` (Frankfurt) bölgesinde çalışır.

## Dosyalar

| Dosya | İçerik |
|---|---|
| `main.py` | Telegram webhook, komutlar, hatırlatmalar, takvim sayfası |
| `metronome.py` | Kayıtta metronom algılama ve BPM tahmini |
| `Dockerfile` | Python 3.12 ve ffmpeg içeren imaj |
| `deploy.sh` | İlk kurulum: API'ler, Firestore, bucket, Cloud Run, webhook, zamanlanmış işler |
| `tests/` | Firestore, Storage ve Telegram'ı taklit eden testler |
| `KURULUM.md` | Adım adım kurulum rehberi |

## Geliştirme ve deploy

`main` dalına yapılan her push, Cloud Build üzerinden otomatik olarak Cloud Run'a deploy edilir. Docker imajı oluşturulurken `tests/` içindeki testler çalışır; bir test başarısız olursa build durur ve çalışan sürüm değişmez.

Testleri yerelde çalıştırmak için:

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q tests
```

Token, gizli anahtarlar ve saat ayarları kodda değil, Cloud Run'ın ortam değişkenlerinde durur. Bunları değiştirmek için:

```bash
gcloud run services update pratik-bot --region europe-west3 --update-env-vars DEGISKEN=deger
```

| Değişken | Açıklama |
|---|---|
| `BOT_TOKEN` | BotFather'dan alınan token |
| `WEBHOOK_SECRET`, `CRON_SECRET`, `WEB_TOKEN` | `deploy.sh`'ın ürettiği gizli anahtarlar |
| `BUCKET` | Kayıtların tutulduğu Cloud Storage bucket'ı |
| `DAY_START_HOUR` | Günün başladığı saat (varsayılan 4) |
| `TZ_NAME` | Saat dilimi (varsayılan Europe/Berlin) |
| `ALLOWED_CHAT_ID` | Botu tek bir gruba kilitlemek için (isteğe bağlı) |
| `PUBLIC_URL` | Takvim linki için servis adresi |

Sıfırdan kurulum için [KURULUM.md](KURULUM.md) dosyasına bak.
