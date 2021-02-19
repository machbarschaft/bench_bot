# Telegram Bot mit Cloud Functions

## Google Cloud Platform Projekt vorbereiten

- Abrechnungskonto verknüpfen
- Cloud Build API aktivieren
- Secrets API aktivieren

## Secrets anlegen

Link: <https://dev.to/googlecloud/using-secrets-in-google-cloud-functions-5aem>

### Bot Token

- Bot Token als Secret anlegen:  `gcloud secrets create bot_token --data-file="<datei mit Telegram Bot Token>" --replication-policy="automatic"`
- Berechtigungen von Cloud Function auf Secret einrichten: `gcloud secrets add-iam-policy-binding bot_token --role roles/secretmanager.secretAccessor --member serviceAccount:<Service Account der Cloud Function>`

### Chat ID for error messages

- Chat ID als Secret anlegen:  `gcloud secrets create error_chat_id --data-file="<datei mit der chat id>" --replication-policy="automatic"`
- Berechtigungen von Cloud Function auf Secret einrichten: `gcloud secrets add-iam-policy-binding error_chat_id --role roles/secretmanager.secretAccessor --member serviceAccount:<Service Account der Cloud Function>`

## Deployment

`gcloud functions deploy mbs_bench_bot --runtime python37 --trigger-http --allow-unauthenticated --env-vars-file <file with env vars`

The usage of environment variables in Google Cloud Functions is described here: <https://cloud.google.com/functions/docs/env-var?hl=pl>

## Set Telegram Webhook URL

Python Script

```python
import telegram
bot = telegram.Bot(<bot_token>)
bot.set_webhook(url=<cloud functions url>)
```
