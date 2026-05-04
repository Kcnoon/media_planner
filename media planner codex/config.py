from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    booking_table: str = Field(
        "noonbigmerchsandbox.keshav.campaign_booking_data_past",
        validation_alias="BOOKING_TABLE",
    )
    delivery_table: str = Field(
        "noonbigmerchsandbox.keshav.campaign_delivery_data_past_performance",
        validation_alias="DELIVERY_TABLE",
    )
    forecast_table: str = Field(
        "noonbiadmon.admon_business_analytics.display_cpm_forecasting_overall",
        validation_alias="FORECAST_TABLE",
    )
    google_sheet_id: str = Field(
        "17J3ymspeVQhEa9v12hb4DMeHE5CEVMcKxAqOAi8hWSE",
        validation_alias="GOOGLE_SHEET_ID",
    )
    booking_tab: str = Field("campaign_booking_data_past", validation_alias="BOOKING_TAB")
    delivery_tab: str = Field(
        "campaign_delivery_data_past_performance",
        validation_alias="DELIVERY_TAB",
    )
    forecast_tab: str = Field("forecasting", validation_alias="FORECAST_TAB")
    slot_data_tab: str = Field("slot_data", validation_alias="SLOT_DATA_TAB")
    plan_runs_tab: str = Field("media_plan_runs", validation_alias="PLAN_RUNS_TAB")
    plan_lines_tab: str = Field("media_plan_lines", validation_alias="PLAN_LINES_TAB")
    historical_lookback_days: int = Field(365, validation_alias="HISTORICAL_LOOKBACK_DAYS")
    min_slot_views: int = Field(1000, validation_alias="MIN_SLOT_VIEWS")
    default_cpm: float = Field(12.0, validation_alias="DEFAULT_CPM")
    default_cpd: float = Field(175.0, validation_alias="DEFAULT_CPD")
    max_lines_per_phase: int = Field(6, validation_alias="MAX_LINES_PER_PHASE")
    min_total_lines: int = Field(6, validation_alias="MIN_TOTAL_LINES")

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
