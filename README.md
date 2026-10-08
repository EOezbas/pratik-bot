# Pratik Zinciri

Arkadaş grubunun günlük müzik pratiği alışkanlığı için bir Telegram botu. Gruba atılan sesli mesajları ve videoları Google Cloud'a kaydeder, kimin hangi gün çalıştığını takip eder, kayıtta metronom olup olmadığını algılar ve tüm kayıtları bir takvim sayfasında gösterir.

## Özellikler

- **Kayıt:** Sesli mesaj, yuvarlak video, normal video ve "Dosya" olarak gönderilen ses ya da video dosyaları kaydedilir. Bot kaydı ❤ ile işaretler. 20 MB'tan büyük dosyalar (1 GB'a kadar) arka planda indirilir; bu sırada bot 👀 koyar. Videolar 720p'ye, sesler 160 kbps Opus'a küçültülerek saklanır. Bunun için `TG_API_ID` ve `TG_API_HASH` ayarlı olmalı.
- **Müzik kontrolü:** İçinde müzik (çalma ya da şarkı söyleme) olmayan, sadece konuşma olan sesli mesajlar ve videolar pratik sayılmaz; bot bunlara dokunmaz. Bunu YAMNet adlı küçük bir ses sınıflandırma modeli yapar; kayıtta birkaç saniye müzik duyması yeterli, emin olamazsa kaydı pratik sayar. Bot bir pratiği yanlışlıkla atlarsa kayda yanıt verip `/kaydet` yazmak yeterli.
- **Metronom algılama:** Kayıtta duyulabilir bir metronom varsa bot 🔥 koyar ve BPM'i kaydeder. Mekanik metronomlar gibi biraz kayan tıklar da kabul edilir; bunun bedeli, çok düzenli çalan (zamanlama sapması ~10 ms altı) birinin metronomsuz kaydının da 🔥 alabilmesi. Metronomsuz kayıtlarda tempo çalınan notalardan tahmin edilir ve takvimde gri "♩ ~84 bpm" olarak gösterilir; tempo düzensizse (rubato, serbest çalım) gösterilmez. Notaların hızı vuruşun iki katıysa bot önce vurgulara bakar, karar veremezse 125 bpm üstünde yavaş olan vuruşu seçer; bu yüzden gerçekten çok hızlı (125+) çalınan parçalar yarı tempo görünebilir. Bu bilgi takibi etkilemez.
- **Seri ve joker:** Haftada (Pazartesi–Pazar) atlanan ilk gün 🃏 joker sayılır ve seriyi bozmaz.
- **Gün sınırı:** Gün gece 00:00'te kapanır; bundan sonra atılan kayıtlar ertesi güne sayılır.
- **Hatırlatma:** Her gün 21:00'de o gün kayıt atmayanlar grupta etiketlenir.
- **Haftalık özet:** Pazar 21:30'da haftanın özeti ve "metronom ustası" gruba gönderilir.
- **Kilometre taşları:** Belirli seri ve kayıt sayılarına ulaşıldığında gruba kutlama mesajı gider.
- **Kim dinledi:** Bir kayda Telegram'da tepki bırakan ya da yanıt veren, ya da takvimde kim olduğunu seçip kaydı oynatan kişi, takvimde kaydın altında "👂 ... dinledi" olarak görünür. Tepkileri görebilmesi için bot grupta yönetici olmalı.
- **Kutlama sticker'ları:** Yönetici bota özelden sticker ya da GIF atar, çıkan butonlardan hangi kilometre taşı için olduğunu seçer. `/kutlamalar` atananları gösterir, birine yanıt verip `/sil` yazınca kaldırılır. Hangi seri ve kayıt sayılarının kutlanacağını `/ekle` ve `/cikar` ile değiştirir. Atanmamış kutlamalarda büyük animasyonlu emoji gider. Bunların hepsi sadece yöneticinin özel sohbetinde çalışır, grup göremez.
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
| `/kaydet` | Bot kaydı konuşma sanıp kaydetmediyse kayda yanıt olarak yazınca kaydedilir (kendi kaydın; yönetici herkesinkini) |
| `/cikar` | Pratik sayılmaması gereken bir kayda yanıt olarak yazınca kayıt takvimden çıkarılır, mesaj grupta kalır (kendi kaydın; yönetici herkesinkini) |
| `/sohbet`, `/pratik` | `/sohbet` sonrası attığın ses ve videolar takvime eklenmez; `/pratik` ile ya da gece gün başlangıcında biter |
| `/sarkilarim` | Yazan kişinin bitmeyen şarkıları. Şarkı adı dosya adından (yoksa notun ilk satırından), nereye kadar çalındığı nottan alınır; dosya adında ya da notta "bitti", "tamamı", "komple", "full", "complete", ✅ gibi bir ifade geçerse şarkı bitmiş sayılır |
| `/bitti` | Şarkının bir kaydına yanıt olarak yazınca şarkı bitmiş sayılır |
| `/notsil` | Bir nota yanıt olarak yazınca o not, kaydın kendisine yanıt olarak yazınca kaydın tüm notları takvimden silinir |
| `/prova` | 7 günlük çok seçimli bir prova anketi açar (17:30'dan önce açılırsa bugün de dahil); `/prova bitir` anketi kapatır, en çok seçilen günü duyurur ve takvimde 🎸 ile işaretler |
| `/atesle`, `/alkis` | Yanıt verilen mesaja 🔥 ya da 👏 bırakır, başka bir şeyi etkilemez |
| `/zar`, `/ilham` | Zar atar · rastgele bir pratik fikri yazar |
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
| `music.py`, `models/yamnet.tflite` | Kayıtta müzik olup olmadığının kontrolü (YAMNet, Apache 2.0) |
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
| `DAY_START_HOUR` | Günün başladığı saat: 4 ise gece 04:00'e kadar atılanlar önceki güne, 23 ise 23:00'ten sonra atılanlar ertesi güne sayılır (varsayılan 4, şu an 0) |
| `TZ_NAME` | Saat dilimi (varsayılan Europe/Berlin) |
| `ALLOWED_CHAT_ID` | Botu tek bir gruba kilitlemek için (isteğe bağlı) |
| `TG_API_ID`, `TG_API_HASH` | my.telegram.org'dan; 20 MB üstü dosyalar için (isteğe bağlı) |
| `PUBLIC_URL` | Takvim linki için servis adresi |

Sıfırdan kurulum için [KURULUM.md](KURULUM.md) dosyasına bak.
