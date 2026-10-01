#!/usr/bin/env bash
# Deploys the practice bot to Cloud Run and wires Telegram + Cloud Scheduler.
# Usage: PROJECT_ID=my-project BOT_TOKEN=123:abc ./deploy.sh
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID is required}"
: "${BOT_TOKEN:?BOT_TOKEN is required}"
REGION="${REGION:-europe-west3}"
SERVICE="${SERVICE:-pratik-bot}"
BUCKET="${BUCKET:-${PROJECT_ID}-pratik-kayitlari}"
REMINDER_CRON="${REMINDER_CRON:-0 21 * * *}"
WEEKLY_CRON="${WEEKLY_CRON:-30 21 * * 0}"
TZ_NAME="${TZ_NAME:-Europe/Berlin}"
DAY_START_HOUR="${DAY_START_HOUR:-4}"
ALLOWED_CHAT_ID="${ALLOWED_CHAT_ID:-}"

cd "$(dirname "$0")"

# Secrets are generated once and reused on later deploys
if [[ -f .secrets.env ]]; then
  source .secrets.env
else
  WEBHOOK_SECRET="$(openssl rand -hex 24)"
  CRON_SECRET="$(openssl rand -hex 24)"
  WEB_TOKEN="$(openssl rand -hex 16)"
  printf 'WEBHOOK_SECRET=%s\nCRON_SECRET=%s\nWEB_TOKEN=%s\n' "$WEBHOOK_SECRET" "$CRON_SECRET" "$WEB_TOKEN" > .secrets.env
  chmod 600 .secrets.env
fi

gcloud config set project "$PROJECT_ID" >/dev/null

echo "== Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com storage.googleapis.com cloudscheduler.googleapis.com

echo "== IAM for default compute service account"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for ROLE in roles/cloudbuild.builds.builder roles/datastore.user roles/storage.objectAdmin roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" \
    --role="$ROLE" --condition=None >/dev/null
done

echo "== Firestore"
if ! gcloud firestore databases describe --database="(default)" >/dev/null 2>&1; then
  gcloud firestore databases create --location="$REGION" --type=firestore-native
fi

echo "== Storage bucket"
if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access \
    --public-access-prevention
fi

ENV_VARS="BOT_TOKEN=$BOT_TOKEN,WEBHOOK_SECRET=$WEBHOOK_SECRET,CRON_SECRET=$CRON_SECRET,WEB_TOKEN=$WEB_TOKEN"
ENV_VARS+=",BUCKET=$BUCKET,TZ_NAME=$TZ_NAME,DAY_START_HOUR=$DAY_START_HOUR,ALLOWED_CHAT_ID=$ALLOWED_CHAT_ID"
# Optional: api_id/api_hash from my.telegram.org enable files above 20 MB
ENV_VARS+=",TG_API_ID=${TG_API_ID:-},TG_API_HASH=${TG_API_HASH:-}"

echo "== Cloud Run deploy"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
  --memory 2Gi --no-cpu-throttling --max-instances 2 --set-env-vars "$ENV_VARS"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
gcloud run services update "$SERVICE" --region "$REGION" --update-env-vars "PUBLIC_URL=$URL" >/dev/null

echo "== Telegram webhook"
RESP="$(curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"${URL}/telegram\",\"secret_token\":\"${WEBHOOK_SECRET}\",\"allowed_updates\":[\"message\",\"edited_message\",\"message_reaction\"]}")"
echo "$RESP"
if [[ "$RESP" != *'"ok":true'* ]]; then
  echo "Webhook could not be set. Check BOT_TOKEN." >&2
  exit 1
fi
curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/setMyCommands" \
  -H 'Content-Type: application/json' \
  -d '{"commands":[{"command":"bugun","description":"Bugün kim kaydetti"},{"command":"seri","description":"Seriler ve bu hafta metronom"},{"command":"takvim","description":"Tüm kayıtların takvimi"},{"command":"katil","description":"Gruba katıl"},{"command":"ayril","description":"Hatırlatmalardan çık"},{"command":"sil","description":"Kendi kaydına yanıt vererek sil"},{"command":"yardim","description":"Nasıl çalışır"}]}' >/dev/null

schedule_job() {
  local job="$1" cron="$2" path="$3"
  if gcloud scheduler jobs describe "$job" --location "$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$job" --location "$REGION" --schedule "$cron" \
      --time-zone "$TZ_NAME" --uri "${URL}${path}" --http-method POST \
      --update-headers "X-Cron-Secret=${CRON_SECRET}"
  else
    gcloud scheduler jobs create http "$job" --location "$REGION" --schedule "$cron" \
      --time-zone "$TZ_NAME" --uri "${URL}${path}" --http-method POST \
      --headers "X-Cron-Secret=${CRON_SECRET}"
  fi
}

echo "== Scheduled jobs"
schedule_job "${SERVICE}-hatirlatma" "$REMINDER_CRON" "/cron/reminder"
schedule_job "${SERVICE}-haftalik" "$WEEKLY_CRON" "/cron/weekly"

echo
echo "Done."
echo "Calendar: ${URL}/?t=${WEB_TOKEN}"
