from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ml.training.data_loader import TrainingDataConfig, load_training_data


REQUIRED_COLUMNS = (
    "user_account_age_days",
    "user_lifetime_order_count",
    "user_lifetime_chargeback_rate",
    "user_orders_1h_at_order_time",
    "user_orders_24h_at_order_time",
    "user_spend_24h_pence",
    "device_lifetime_order_count",
    "device_unique_users_lifetime",
    "payment_lifetime_chargeback_rate",
    "ip_unique_users_24h",
    "store_chargeback_rate",
    "merchant_chargeback_rate",
    "email_domain_chargeback_rate",
    "subtotal_pence",
    "total_pence",
    "item_count",
    "delivery_distance_km",
    "ip_to_delivery_distance_km",
    "billing_to_delivery_distance_km",
    "time_to_checkout_seconds",
    "order_channel",
    "order_type",
    "payment_type",
    "card_brand",
    "card_funding_type",
    "device_type",
    "platform",
    "merchant_category",
    "delivery_address_type",
    "cancellation_reason",
    "card_bin",
    "card_issuer_bank",
    "ip_country",
    "store_city",
    "browser_name",
    "user_email_domain",
    "is_first_order_for_user",
    "is_new_payment_method",
    "is_new_delivery_address",
    "is_guest_checkout",
    "is_digital_native_bank",
    "ip_is_proxy",
    "ip_is_vpn",
    "ip_is_tor",
    "ip_is_hosting",
    "is_fraud",
)

STRING_COLUMNS = {
    "order_channel",
    "order_type",
    "payment_type",
    "card_brand",
    "card_funding_type",
    "device_type",
    "platform",
    "merchant_category",
    "delivery_address_type",
    "cancellation_reason",
    "card_bin",
    "card_issuer_bank",
    "ip_country",
    "store_city",
    "browser_name",
    "user_email_domain",
}

BOOLEAN_COLUMNS = {
    "is_first_order_for_user",
    "is_new_payment_method",
    "is_new_delivery_address",
    "is_guest_checkout",
    "is_digital_native_bank",
    "ip_is_proxy",
    "ip_is_vpn",
    "ip_is_tor",
    "ip_is_hosting",
    "is_fraud",
}


def _utc_datetime(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def _synthetic_training_df() -> pd.DataFrame:
    row: dict[str, object] = {}
    for column in REQUIRED_COLUMNS:
        if column in STRING_COLUMNS:
            row[column] = "UNKNOWN"
        elif column in BOOLEAN_COLUMNS:
            row[column] = False
        else:
            row[column] = 0
    return pd.DataFrame([row])


def test_training_data_config_defaults() -> None:
    config = TrainingDataConfig(start_date=_utc_datetime(1), end_date=_utc_datetime(2))

    assert config.label_finalisation_buffer_days == 45
    assert config.max_rows is None


@pytest.mark.parametrize(
    "end_date",
    [
        _utc_datetime(1),
        datetime(2025, 12, 31, tzinfo=timezone.utc),
    ],
)
def test_training_data_config_validation(end_date: datetime) -> None:
    config = TrainingDataConfig(start_date=_utc_datetime(1), end_date=end_date)

    with patch("ml.training.data_loader.get_engine") as mock_get_engine, pytest.raises(
        ValueError,
        match="end_date must be after start_date",
    ):
        load_training_data(config)

    mock_get_engine.assert_not_called()


def test_load_training_data_returns_required_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TrainingDataConfig(start_date=_utc_datetime(1), end_date=_utc_datetime(2))
    synthetic_df = _synthetic_training_df()
    mock_conn = MagicMock()
    monkeypatch.chdir(tmp_path)

    with patch("ml.training.data_loader.get_engine") as mock_get_engine, patch(
        "ml.training.data_loader.pd.read_sql_query",
    ) as mock_read_sql, patch("pandas.DataFrame.to_parquet") as mock_to_parquet:
        mock_get_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_read_sql.return_value = synthetic_df

        result = load_training_data(config)

    mock_get_engine.assert_called_once_with(role="training")
    mock_read_sql.assert_called_once()
    mock_to_parquet.assert_called_once()
    assert all(column in result.columns for column in REQUIRED_COLUMNS)
    assert "is_fraud" in result.columns


def test_load_training_data_max_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TrainingDataConfig(
        start_date=_utc_datetime(1),
        end_date=_utc_datetime(2),
        max_rows=100,
    )
    mock_conn = MagicMock()
    monkeypatch.chdir(tmp_path)

    with patch("ml.training.data_loader.get_engine") as mock_get_engine, patch(
        "ml.training.data_loader.pd.read_sql_query",
    ) as mock_read_sql, patch("pandas.DataFrame.to_parquet"):
        mock_get_engine.return_value.connect.return_value.__enter__.return_value = mock_conn
        mock_read_sql.return_value = _synthetic_training_df()

        load_training_data(config)

    query_text = str(mock_read_sql.call_args.args[0])
    params = mock_read_sql.call_args.kwargs["params"]
    assert "LIMIT" in query_text
    assert params["max_rows"] == 100
