# Noon Media Planner Backend

This app now reads source data from BigQuery and writes generated plans back to BigQuery.

## Data flow

Source tables:

- `noonbiadmon.admon_business_analytics.slot_base_data`
- `noonbiadmon.admon_business_analytics.slot_rate_card_base`
- `noonbiadmon.admon_business_analytics.campaign_booking_data_past`
- `noonbiadmon.admon_business_analytics.adgroup_booked_delivered` (six-month slot eligibility)
- `noonbiadmon.admon_business_analytics.campaign_delivery_data_past_performance`
- `noonbiadmon.admon_business_analytics.forecasting`

Generated plan tables:

- `noonbiadmon.admon_business_analytics.media_plan_runs`
- `noonbiadmon.admon_business_analytics.media_plan_lines`

The app uses Application Default Credentials, so on Cloud Run it will authenticate through the runtime service account. Locally, use `gcloud auth application-default login` or a service-account JSON.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m uvicorn main:app --reload --port 8000
```

Open `http://127.0.0.1:8000`.

Useful env vars:

```bash
export BIGQUERY_PROJECT=noonbiadmon
export BOOKING_TABLE=noonbiadmon.admon_business_analytics.campaign_booking_data_past
export ADGROUP_BOOKED_DELIVERED_TABLE=noonbiadmon.admon_business_analytics.adgroup_booked_delivered
export DELIVERY_TABLE=noonbiadmon.admon_business_analytics.campaign_delivery_data_past_performance
export FORECAST_TABLE=noonbiadmon.admon_business_analytics.forecasting
export SLOT_DATA_TABLE=noonbiadmon.admon_business_analytics.slot_base_data
export SLOT_RATE_CARD_TABLE=noonbiadmon.admon_business_analytics.slot_rate_card_base
export PLAN_RUNS_TABLE=noonbiadmon.admon_business_analytics.media_plan_runs
export PLAN_LINES_TABLE=noonbiadmon.admon_business_analytics.media_plan_lines
export PUBLIC_APP_URL=http://127.0.0.1:8000
```

If your BigQuery dataset has a fixed location and jobs complain about location mismatch, also set:

```bash
export BIGQUERY_LOCATION=EU
```

## Cloud Run target setup

Deployment project:

- `noonprd-biadmon`

BigQuery billing/query project:

- `noonbiadmon`

Cloud Run region:

- `europe-west1`

Artifact Registry region:

- `asia-south1`

Runtime service account:

- `admon-analytics@noonprd-biadmon.iam.gserviceaccount.com`

## Required Google Cloud setup

1. Set the deployment project:

```bash
gcloud config set project noonprd-biadmon
```

2. Enable APIs:

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

Also make sure `bigquery.googleapis.com` is enabled on project `noonbiadmon`.

3. Create the Artifact Registry repo if it does not exist:

```bash
gcloud artifacts repositories create cloud-run-source-deploy \
  --repository-format=docker \
  --location=asia-south1
```

4. Grant the runtime service account access to BigQuery in `noonbiadmon`.

At minimum, grant on dataset `admon_business_analytics`:

- read access to source tables
- write access to `media_plan_runs`
- write access to `media_plan_lines`

Typical roles:

- `roles/bigquery.dataViewer` on the source dataset
- `roles/bigquery.dataEditor` on the dataset containing the plan tables
- `roles/bigquery.jobUser` on project `noonbiadmon`

Example:

```bash
gcloud projects add-iam-policy-binding noonbiadmon \
  --member="serviceAccount:admon-analytics@noonprd-biadmon.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
```

Then grant dataset-level viewer/editor access in BigQuery for dataset `admon_business_analytics`.

## Create the output tables

The app will create `media_plan_runs` and `media_plan_lines` automatically if the service account has permission to create or update tables in the dataset.

If your organization blocks automatic table creation, create these two empty tables first and then deploy.

## Deploy with Cloud Build

From the repo root:

```bash
gcloud builds submit \
  --config cloudbuild.yaml \
  --substitutions=_SERVICE_NAME=noon-media-planner,_REGION=europe-west1,_IMAGE=asia-south1-docker.pkg.dev/noonprd-biadmon/cloud-run-source-deploy/noon-media-planner,_BIGQUERY_PROJECT=noonbiadmon,_BOOKING_TABLE=noonbiadmon.admon_business_analytics.campaign_booking_data_past,_DELIVERY_TABLE=noonbiadmon.admon_business_analytics.campaign_delivery_data_past_performance,_FORECAST_TABLE=noonbiadmon.admon_business_analytics.forecasting,_SLOT_DATA_TABLE=noonbiadmon.admon_business_analytics.slot_base_data,_SLOT_RATE_CARD_TABLE=noonbiadmon.admon_business_analytics.slot_rate_card_base,_PLAN_RUNS_TABLE=noonbiadmon.admon_business_analytics.media_plan_runs,_PLAN_LINES_TABLE=noonbiadmon.admon_business_analytics.media_plan_lines,_PUBLIC_APP_URL=https://YOUR_CLOUD_RUN_URL,_RUNTIME_SERVICE_ACCOUNT=admon-analytics@noonprd-biadmon.iam.gserviceaccount.com
```

For the first deploy, `_PUBLIC_APP_URL` can be left empty. After Cloud Run gives you the live URL, redeploy once with `_PUBLIC_APP_URL=https://...` so copied plan links open the hosted app directly.

## Deploy manually without `cloudbuild.yaml`

```bash
gcloud builds submit --tag asia-south1-docker.pkg.dev/noonprd-biadmon/cloud-run-source-deploy/noon-media-planner

gcloud run deploy noon-media-planner \
  --image asia-south1-docker.pkg.dev/noonprd-biadmon/cloud-run-source-deploy/noon-media-planner \
  --region europe-west1 \
  --platform managed \
  --allow-unauthenticated \
  --service-account admon-analytics@noonprd-biadmon.iam.gserviceaccount.com \
  --set-env-vars BIGQUERY_PROJECT=noonbiadmon,BOOKING_TABLE=noonbiadmon.admon_business_analytics.campaign_booking_data_past,DELIVERY_TABLE=noonbiadmon.admon_business_analytics.campaign_delivery_data_past_performance,FORECAST_TABLE=noonbiadmon.admon_business_analytics.forecasting,SLOT_DATA_TABLE=noonbiadmon.admon_business_analytics.slot_base_data,SLOT_RATE_CARD_TABLE=noonbiadmon.admon_business_analytics.slot_rate_card_base,PLAN_RUNS_TABLE=noonbiadmon.admon_business_analytics.media_plan_runs,PLAN_LINES_TABLE=noonbiadmon.admon_business_analytics.media_plan_lines
```

## Notes

- Saved-plan reload now comes from BigQuery, not local SQLite.
- `sheet_plan_code` in the API/UI is now the generated BigQuery-backed media plan code.
- `sheet_plan_link` is now the hosted planner URL when `PUBLIC_APP_URL` is set.
