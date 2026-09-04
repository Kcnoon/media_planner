from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    booking_table: str = Field(
        "noonbiadmon.admon_business_analytics.campaign_booking_data_past",
        validation_alias="BOOKING_TABLE",
    )
    adgroup_booked_delivered_table: str = Field(
        "noonbiadmon.admon_business_analytics.adgroup_booked_delivered",
        validation_alias="ADGROUP_BOOKED_DELIVERED_TABLE",
    )
    delivery_table: str = Field(
        "noonbiadmon.admon_business_analytics.campaign_delivery_data_past_performance",
        validation_alias="DELIVERY_TABLE",
    )
    forecast_table: str = Field(
        "noonbiadmon.admon_business_analytics.forecasting",
        validation_alias="FORECAST_TABLE",
    )
    slot_data_table: str = Field(
        "noonbiadmon.admon_business_analytics.slot_data",
        validation_alias="SLOT_DATA_TABLE",
    )
    slot_rate_card_table: str = Field(
        "noonbiadmon.admon_business_analytics.slot_rate_card_base",
        validation_alias="SLOT_RATE_CARD_TABLE",
    )
    offdeck_slots_table: str = Field(
        "noonbiadmon.admon_business_analytics.offdeck_slots",
        validation_alias="OFFDECK_SLOTS_TABLE",
    )
    brand_code_table: str = Field(
        "noonbiadmon.assortment_noon.catalog",
        validation_alias="BRAND_CODE_TABLE",
    )
    plan_runs_table: str = Field(
        "noonbiadmon.dwh_admon_business_analytics.media_plan_runs",
        validation_alias="PLAN_RUNS_TABLE",
    )
    plan_lines_table: str = Field(
        "noonbiadmon.dwh_admon_business_analytics.media_plan_lines",
        validation_alias="PLAN_LINES_TABLE",
    )
    bigquery_project: str = Field("noonbiadmon", validation_alias="BIGQUERY_PROJECT")
    bigquery_location: str = Field("", validation_alias="BIGQUERY_LOCATION")
    public_app_url: str = Field(
        "https://bq-test-app-464570346642.europe-west1.run.app/",
        validation_alias="PUBLIC_APP_URL",
    )
    historical_lookback_days: int = Field(365, validation_alias="HISTORICAL_LOOKBACK_DAYS")
    min_slot_views: int = Field(1000, validation_alias="MIN_SLOT_VIEWS")
    default_cpm: float = Field(10.0, validation_alias="DEFAULT_CPM")
    default_cpd: float = Field(1000.0, validation_alias="DEFAULT_CPD")
    max_lines_per_phase: int = Field(6, validation_alias="MAX_LINES_PER_PHASE")
    min_total_lines: int = Field(6, validation_alias="MIN_TOTAL_LINES")

    # RoAS-efficiency refinement (see refine.py). Trades budget spend for blended RoAS by
    # keeping the highest-RoAS slots up to a target utilisation and trimming the dilutive
    # tail. Enabled with a "balanced" 0.5 target (keep the efficient inventory, drop the
    # low-RoAS tail); both are env-overridable.
    roas_refine_enabled: bool = Field(True, validation_alias="ROAS_REFINE_ENABLED")
    roas_target_utilization: float = Field(0.5, validation_alias="ROAS_TARGET_UTILIZATION")
    roas_refine_min_lines: int = Field(4, validation_alias="ROAS_REFINE_MIN_LINES")

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
