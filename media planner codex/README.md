# Noon Media Planner Backend

This workspace contains an API-backed version of `noon_media_planner.html`.

## Logic

1. Historical delivery is read from the Google Sheet tab `campaign_delivery_data_past_performance`.
   - `ROAS = revenue / spends`
   - `CPM = spends * 1000 / views`
   - `CTR = clicks / views`
   - Old brand: exact brand history is used first. If the brand has no history, the planner falls back to selected comcat history.
   - New brand: selected comcat history is used.
2. Future inventory is read from the Google Sheet tab `forecasting`.
   - Forecast impressions are `slot_sessions`.
   - Booked impressions are subtracted from the `campaign_booking_data_past` tab.
   - `available_views = max(slot_sessions - booked_views, 0)`.
3. The planner ranks slots separately for reach and conversion.
   - Reach score favors high impressions and efficient CPM.
   - Conversion score favors high ROAS, CTR, and revenue.
4. Budget is split by objective.
   - Reach: 100% reach slots.
   - ROAS: 100% conversion slots.
   - Balanced: frontend weight sliders.
5. Each scheduling phase gets budget by its share of total flight days.
6. Every generated plan is stored back into the same Google Sheet.
   - Summary rows are written to `media_plan_runs`.
   - Line items are written to `media_plan_lines`.
   - A generated `sheet_plan_code` and `sheet_plan_link` are returned in the API summary.
7. A local SQLite copy is also stored at `media_plans.db` and can be fetched later with `/api/media-plan/{plan_id}`.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m uvicorn main:app --reload --port 8000
```

Open `http://127.0.0.1:8000`.

Google Sheets auth uses standard Google credentials. For local use, either set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account JSON file that has access to the sheet, or run `gcloud auth application-default login`.

The source spreadsheet is configured through `GOOGLE_SHEET_ID`, defaulting to:

`17J3ymspeVQhEa9v12hb4DMeHE5CEVMcKxAqOAi8hWSE`
