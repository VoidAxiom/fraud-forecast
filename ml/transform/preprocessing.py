import tensorflow as tf
import tensorflow_transform as tft


def preprocessing_fn(inputs: dict) -> dict:
    """
    inputs: dict[feature_name, Tensor] with raw values
    returns: dict[feature_name, Tensor] with preprocessed values ready for model input
    """
    outputs = {}

    # === Numerical (z-score) ===
    NUMERICAL_FEATURES = [
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
    ]
    for f in NUMERICAL_FEATURES:
        # Log1p before z-score for heavy-tailed features
        if f in {
            "total_pence",
            "subtotal_pence",
            "user_lifetime_order_count",
            "device_lifetime_order_count",
        }:
            x = tf.math.log1p(tf.cast(inputs[f], tf.float32))
        else:
            x = tf.cast(inputs[f], tf.float32)
        outputs[f] = tft.scale_to_z_score(x)

    # === Categorical (one-hot) ===
    LOW_CARD_CATEGORICAL = [
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
    ]
    NULLABLE_CATEGORICALS = {"delivery_address_type", "cancellation_reason"}
    for f in LOW_CARD_CATEGORICAL:
        x = inputs[f]
        if f in NULLABLE_CATEGORICALS:
            x = tf.where(tf.equal(x, b""), tf.constant(b"UNKNOWN"), x)
        outputs[f] = tft.compute_and_apply_vocabulary(
            x, top_k=20, num_oov_buckets=1, vocab_filename=f"vocab_{f}"
        )

    # === High-cardinality categorical (hash-embed) ===
    HIGH_CARD_HASH_FEATURES = {
        "card_bin": 1000,
        "card_issuer_bank": 100,
        "ip_country": 50,
        "store_city": 100,
        "browser_name": 30,
        "user_email_domain": 200,
    }
    for f, buckets in HIGH_CARD_HASH_FEATURES.items():
        outputs[f] = tft.hash_strings(inputs[f], hash_buckets=buckets)

    # === Booleans (pass through as int) ===
    BOOLEAN_FEATURES = [
        "is_first_order_for_user",
        "is_new_payment_method",
        "is_new_delivery_address",
        "is_guest_checkout",
        "is_digital_native_bank",
        "ip_is_proxy",
        "ip_is_vpn",
        "ip_is_tor",
        "ip_is_hosting",
    ]
    for f in BOOLEAN_FEATURES:
        outputs[f] = tf.cast(inputs[f], tf.int64)

    # === Engineered ===
    # Geo-mismatch composite
    outputs["geo_mismatch_score"] = tft.scale_to_z_score(
        tf.cast(inputs["ip_to_delivery_distance_km"], tf.float32)
        + tf.cast(inputs["billing_to_delivery_distance_km"], tf.float32)
    )
    # Card country mismatch
    outputs["card_country_mismatch"] = tf.cast(
        tf.not_equal(inputs["card_issuer_country"], inputs["ip_country"]), tf.int64
    )
    # Velocity ratio
    outputs["velocity_ratio_1h_vs_lifetime"] = tft.scale_to_z_score(
        tf.cast(inputs["user_orders_1h_at_order_time"], tf.float32)
        / (tf.cast(inputs["user_lifetime_order_count"], tf.float32) + 1.0)
    )

    # gt_is_fraud: simulator ground truth (bool) -> binary label
    outputs["label"] = tf.cast(inputs["gt_is_fraud"], tf.int64)
    return outputs
