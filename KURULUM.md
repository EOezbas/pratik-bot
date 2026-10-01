# Pratik Zinciri – Kurulum

Telegram grubunuza atılan sesli mesajları Google Cloud'a kaydeden, her gün 21:00'de kayıt atmayanları hatırlatan ve tüm kayıtları takvimde gösteren bot.

## 1. Telegram botunu oluştur

1. Telegram'da **@BotFather**'a yaz: `/newbot` → isim ver → **token**'ı kopyala.
2. Aynı yerde: `/setprivacy` → botunu seç → **Disable**.
   Bu adım şart. Yoksa bot gruptaki sesli mesajları göremez.
3. Botu pratik grubunuza ekle.
   Privacy ayarını botu ekledikten sonra değiştirdiysen botu gruptan çıkarıp tekrar ekle.

## 2. Google Cloud'a deploy

Bilgisayarında `gcloud` CLI kurulu ve `gcloud auth login` yapılmış olmalı. Projede faturalandırma açık olmalı; bu kullanım ücretsiz katmanda kalır.

```bash
cd pratik-bot
chmod +x deploy.sh
PROJECT_ID=senin-proje-id BOT_TOKEN=123456:ABC... ./deploy.sh
```

Script şunları yapar:

- Gerekli API'leri açar.
- Firestore veritabanını ve kayıtlar için bir Cloud Storage bucket'ı oluşturur.
- Botu Cloud Run'a kurar.
- Telegram webhook'unu bağlar.
- Her gün 21:00 (Europe/Berlin) hatırlatmasını ayarlar.

Sonunda takvim linkini yazar. Gizli anahtarlar `.secrets.env` dosyasında saklanır; bu dosyayı silme ve kimseyle paylaşma.

Kodu değiştirdikten sonra aynı komutu tekrar çalıştırman yeterli.

## 3. Kullanım

Gruba **ilk mesaj** atıldığında bot o grubu kendine kaydeder ve sadece orada çalışır.

- **Sesli mesaj at:** Bot kaydeder ve ❤ ile işaretler. Ses dosyası, video ve yuvarlak video mesajı da olur (en fazla 20 MB).
- **Metronomla çal:** Metronom kayıtta duyuluyorsa (hoparlörden) bot 🔥 koyar ve BPM'i takvime yazar. Algılama tahmindir; kulaklıktan dinlenen metronom duyulmaz.
- **Haftalık özet:** Pazar 21:30'da haftanın gün sayıları ve "metronom ustası" gruba gönderilir.
- **Not ekle:** Sesli mesaja açıklama yaz ya da kendi kaydına metinle yanıt ver.
- `/katil`: Kayıt atmadan gruba katıl. İlk kayıtta zaten otomatik eklenirsin.
- `/bugun`: Bugün kim kaydetti.
- `/seri`: Son 21 gün: 🔥 metronomlu, ❤ kaydetti, 🃏 joker, 💔 atladı. Haftada atlanan ilk gün joker sayılır ve seriyi bozmaz.
- `/detay`: Herkesin en uzun serisi, bu ayki günleri, metronomlu günleri ve toplam kayıt süresi.
- `/takvim`: Takvim sayfasının linki. Linki açan tarayıcı bir yıl boyunca hatırlanır.
- `/ayril`: Hatırlatmalardan çık.
- `/sil`: Kendi kaydına yanıt olarak yazınca kayıt takvimden ve depodan silinir.
- `/yenilink`: Takvim linkini yeniler.
- **Yönetici:** Bota özelden `/yonetici` yaz; hata uyarıları sana gelir.

Gece 04:00'e kadar atılan kayıtlar önceki güne sayılır. Bu saati `DAY_START_HOUR` ile değiştirebilirsin.

## Ayarlar

`deploy.sh` çalıştırılırken ortam değişkeni olarak verilir.

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `REMINDER_CRON` | `0 21 * * *` | Hatırlatma saati. İki hatırlatma için örnek: `0 21,23 * * *` |
| `DAY_START_HOUR` | `4` | Günün başladığı saat |
| `REGION` | `europe-west3` | Frankfurt |
| `ALLOWED_CHAT_ID` | boş | Botu belirli bir gruba kilitlemek için |

## Sorun olursa

- **Bot sesli mesajlara tepki vermiyor:** Privacy mode açık kalmıştır. 1.2. adımı yap, sonra botu gruptan çıkarıp tekrar ekle.
- **Loglara bakmak için:** `gcloud run services logs read pratik-bot --region europe-west3`
- **Firestore veya Storage için izin hatası:** Cloud Run'ın servis hesabına `Cloud Datastore User` ve bucket üzerinde `Storage Object Admin` rollerini ver.
- **iPhone'da ses çalmıyor:** Telegram sesli mesajları `.ogg` formatında. Eski iOS sürümleri bunu oynatamaz. iOS 17 ve üzeri oynatır; her durumda "İndir" ile indirilebilir.
