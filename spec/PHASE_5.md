# PHASE 5: ML Training Pipeline (TF Transform + XGBoost + Keras DNN)

## Goal of This Phase

Build the offline training pipeline. Pull labeled historical data from the archive + ground truth, run TF Transform preprocessing, train two complementary models (XGBoost for tabular signal, Keras DNN for combinations), evaluate rigorously with proper time-series splits, and save versioned model artefacts ready for the scoring service in Phase 6.

## Prerequisites

- Phases 1-4 complete.
- System has been running for **at least 14 simulated days** (run accelerated time if needed) so there's enough labeled data: at least 100K orders with finalised `fraud_outcome` (i.e. delivered + chargeback window elapsed + chargebacks generated).

## Stack Recap

- TensorFlow 2.3.0
- TFX 0.22 components (specifically `tensorflow_transform`)
- Apache Beam 2.23 with DirectRunner (local execution; in real prod you'd use DataflowRunner)
- XGBoost 1.2.0
- scikit-learn 0.23 (metrics, splits)
- pandas 1.1, numpy 1.19
- MLflow 1.10 for experiment tracking (lightweight — local file backend)

## Deliverables

1. `ml/training/data_loader.py` — pulls labeled training data from Postgres
2. `ml/transform/preprocessing.py` — TF Transform preprocessing fn
3. `ml/transform/run_transform.py` — runs the Beam pipeline producing TFRecords
4. `ml/training/train_xgboost.py`
5. `ml/training/train_dnn.py`
6. `ml/training/evaluate.py`
7. `ml/training/promote.py` — promotes a candidate to "production" symlink
8. `ml/registry/model_registry.py` — versioned storage abstraction
9. `Makefile` targets: `train`, `evaluate`, `promote`
10. `tests/test_training_pipeline.py` — smoke tests on synthetic small dataset

## Training Data Definition

### Source query

Pull from `orders_archive` (and recent `orders`) joined to `simulator_ground_truth` and `chargebacks`. Use a connection as `training_user` — create this role in a new migration:

```sql
-- db/migrations/005_training_user.sql
CREATE ROLE training_user WITH LOGIN PASSWORD 'training_dev_password';
GRANT CONNECT ON DATABASE fraud_platform TO training_user;
GRANT USAGE ON SCHEMA public TO training_user;
GRANT SELECT ON orders, orders_archive, order_items, order_items_archive,
                 order_events, order_events_archive, chargebacks, refunds,
                 simulator_ground_truth, users, devices, payment_methods,
                 stores, merchants, user_addresses
TO training_user;
```

### Label definition

```python
def compute_label(order_row) -> int:
    """
    Returns 1 if fraud, 0 if legit.
    Uses simulator_ground_truth as the gold standard.
    In production this would be:
      label = (chargeback_received_at IS NOT NULL AND chargeback.reason_category = 'FRAUD')
              OR (fraud_outcome IN ('FRAUD', 'CHARGEBACK', 'REFUND_ABUSE', 'PROMO_ABUSE'))
              OR (analyst_review_outcome = 'FRAUD')
    
    For training simulation, we use ground truth directly.
    """
    return 1 if order_row['gt_is_fraud'] else 0
```

### Eligibility filter

Only include orders where the label is **finalised**:
- Order is in a terminal state
- Either >60 days post-delivery (chargeback window closed) OR has an explicit chargeback/refund_abuse outcome

This excludes recent orders where the chargeback might still arrive. Without this filter, recent legit orders would be mislabeled as legit when they're actually pending fraud.

### Time-aware split

Time-series cross-validation — no random shuffling. Specifically:

```python
def time_series_split(df, train_end, val_end):
    """
    train: placed_at < train_end
    val:   train_end <= placed_at < val_end
    test:  placed_at >= val_end
    """
```

For first training run (assuming 30 days of data):
- Train: days 1-21
- Val: days 22-25
- Test: days 26-30

## ml/training/data_loader.py

```python
@dataclass
class TrainingDataConfig:
    start_date: datetime
    end_date: datetime
    label_finalisation_buffer_days: int = 45   # exclude orders <45d old
    max_rows: int | None = None                # None = no cap

def load_training_data(config: TrainingDataConfig) -> pd.DataFrame:
    """
    Returns a DataFrame with all order columns, joined features from the feature store
    (loaded at training time — see note below), and labels.
    """
```

**Critical: time-correct features.** When training, you need the feature values **as they were at the time of the order**, not current values. Two options:

1. **Use the snapshot fields already in the order row** (Phase 2 baked in many of these — `user_account_age_days`, `user_total_orders_lifetime`, etc.). These are point-in-time correct.
2. **For features not in the snapshot** (like `user_orders_1h`), recompute them from history rather than reading current Redis state. This means: for each training row, look back at orders BEFORE this order's `placed_at` and compute the velocity values.

Approach (2) is expensive but correct. Approach (1) is faster but limited to whatever was snapshotted. For Phase 5, use a hybrid:

- Snapshot fields directly from the order row (already point-in-time correct)
- For velocity features: compute on the fly via SQL window functions in the loader:
  ```sql
  COUNT(*) OVER (
      PARTITION BY user_id 
      ORDER BY placed_at 
      RANGE BETWEEN INTERVAL '1 hour' PRECEDING AND INTERVAL '1 second' PRECEDING
  ) as user_orders_1h_at_order_time
  ```
- For batch-historical features: compute via a self-join on orders prior to the row's `placed_at`

This guarantees no future leakage — every feature value reflects only data available at order placement time.

Save the loaded DataFrame to Parquet at `ml/data/training_{run_id}.parquet` for reproducibility.

## ml/transform/preprocessing.py

TF Transform preprocessing function. This compiles into the SavedModel graph at training time AND at serving time — guarantees zero training/serving skew.

```python
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
        'user_account_age_days',
        'user_lifetime_order_count',
        'user_lifetime_chargeback_rate',
        'user_orders_1h_at_order_time',
        'user_orders_24h_at_order_time',
        'user_spend_24h_pence',
        'device_lifetime_order_count',
        'device_unique_users_lifetime',
        'payment_lifetime_chargeback_rate',
        'ip_unique_users_24h',
        'store_chargeback_rate',
        'merchant_chargeback_rate',
        'email_domain_chargeback_rate',
        'subtotal_pence',
        'total_pence',
        'item_count',
        'delivery_distance_km',
        'ip_to_delivery_distance_km',
        'billing_to_delivery_distance_km',
        'time_to_checkout_seconds',
    ]
    for f in NUMERICAL_FEATURES:
        # Log1p before z-score for heavy-tailed features
        if f in {'total_pence', 'subtotal_pence', 'user_lifetime_order_count',
                 'device_lifetime_order_count'}:
            x = tf.math.log1p(tf.cast(inputs[f], tf.float32))
        else:
            x = tf.cast(inputs[f], tf.float32)
        outputs[f] = tft.scale_to_z_score(x)
    
    # === Categorical (one-hot) ===
    LOW_CARD_CATEGORICAL = [
        'order_channel', 'order_type', 'payment_type', 'card_brand', 
        'card_funding_type', 'device_type', 'platform', 'merchant_category',
        'delivery_address_type', 'cancellation_reason',  # mostly null at scoring time
    ]
    for f in LOW_CARD_CATEGORICAL:
        outputs[f] = tft.compute_and_apply_vocabulary(
            inputs[f], top_k=20, num_oov_buckets=1, vocab_filename=f'vocab_{f}'
        )
    
    # === High-cardinality categorical (hash-embed) ===
    HIGH_CARD_HASH_FEATURES = {
        'card_bin': 1000,
        'card_issuer_bank': 100,
        'ip_country': 50,
        'store_city': 100,
        'browser_name': 30,
        'user_email_domain': 200,
    }
    for f, buckets in HIGH_CARD_HASH_FEATURES.items():
        outputs[f] = tft.hash_strings(inputs[f], hash_buckets=buckets)
    
    # === Booleans (pass through as int) ===
    BOOLEAN_FEATURES = [
        'is_first_order_for_user', 'is_new_payment_method', 'is_new_delivery_address',
        'is_guest_checkout', 'is_digital_native_bank', 'ip_is_proxy', 'ip_is_vpn',
        'ip_is_tor', 'ip_is_hosting',
    ]
    for f in BOOLEAN_FEATURES:
        outputs[f] = tf.cast(inputs[f], tf.int64)
    
    # === Engineered ===
    # Geo-mismatch composite
    outputs['geo_mismatch_score'] = tft.scale_to_z_score(
        tf.cast(inputs['ip_to_delivery_distance_km'], tf.float32) +
        tf.cast(inputs['billing_to_delivery_distance_km'], tf.float32)
    )
    # Card country mismatch
    outputs['card_country_mismatch'] = tf.cast(
        tf.not_equal(inputs['card_issuer_country'], inputs['ip_country']), tf.int64
    )
    # Velocity ratio
    outputs['velocity_ratio_1h_vs_lifetime'] = tft.scale_to_z_score(
        tf.cast(inputs['user_orders_1h_at_order_time'], tf.float32) /
        (tf.cast(inputs['user_lifetime_order_count'], tf.float32) + 1.0)
    )
    
    return outputs
```

### ml/transform/run_transform.py

Apache Beam pipeline. Reads the Parquet from data_loader, applies `preprocessing_fn`, writes TFRecord files + a transform_fn (preprocessing graph) saved to disk.

```python
def run_pipeline(input_parquet, output_dir, preprocessing_fn):
    with beam.Pipeline(runner='DirectRunner') as p:
        raw_data = (p 
            | 'ReadParquet' >> beam.io.ReadFromParquet(input_parquet)
            | 'ToDict' >> beam.Map(row_to_dict))
        
        with tft_beam.Context(temp_dir=temp_dir):
            transformed_dataset, transform_fn = (
                (raw_data, raw_metadata)
                | tft_beam.AnalyzeAndTransformDataset(preprocessing_fn)
            )
            
            transformed_data, transformed_metadata = transformed_dataset
            
            _ = (transformed_data
                | 'WriteTFRecord' >> tft_beam.WriteTFExample(
                    f'{output_dir}/train', transformed_metadata.schema))
            
            _ = (transform_fn 
                | 'WriteTransformFn' >> tft_beam.WriteTransformFn(output_dir))
```

Output: `ml/data/transformed/{run_id}/` containing TFRecords + transform_fn (SavedModel for preprocessing).

## ml/training/train_xgboost.py

```python
def train_xgboost(train_data, val_data, hyperparams):
    """
    Reads TFRecords (uses tf.data → numpy), trains XGBoost.
    """
    X_train, y_train = tfrecords_to_numpy(train_data)
    X_val, y_val = tfrecords_to_numpy(val_data)
    
    # Class imbalance: ~2% positives. Use scale_pos_weight.
    scale_pos_weight = (y_train == 0).sum() / (y_train == 1).sum()
    
    params = {
        'objective': 'binary:logistic',
        'eval_metric': ['aucpr', 'logloss'],
        'max_depth': hyperparams.get('max_depth', 8),
        'learning_rate': hyperparams.get('learning_rate', 0.05),
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'min_child_weight': 5,
        'scale_pos_weight': scale_pos_weight,
        'tree_method': 'hist',
        'n_jobs': -1,
    }
    
    dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=feature_names)
    dval = xgb.DMatrix(X_val, label=y_val, feature_names=feature_names)
    
    model = xgb.train(
        params, dtrain, 
        num_boost_round=500,
        evals=[(dtrain, 'train'), (dval, 'val')],
        early_stopping_rounds=30,
        verbose_eval=10,
    )
    
    # Feature importance (gain) for monitoring
    importance = model.get_score(importance_type='gain')
    
    return model, importance
```

Save as `models/xgboost/{version}/model.bin` plus a `feature_names.json` and `metadata.json` (training config, best iteration, val metrics).

## ml/training/train_dnn.py

```python
def build_dnn_model(input_dim):
    inputs = tf.keras.Input(shape=(input_dim,))
    x = tf.keras.layers.Dense(256, activation='relu')(inputs)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.Dropout(0.3)(x)
    
    x = tf.keras.layers.Dense(128, activation='relu')(x)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.Dropout(0.3)(x)
    
    x = tf.keras.layers.Dense(64, activation='relu')(x)
    x = tf.keras.layers.Dropout(0.2)(x)
    
    x = tf.keras.layers.Dense(32, activation='relu')(x)
    
    output = tf.keras.layers.Dense(1, activation='sigmoid')(x)
    
    model = tf.keras.Model(inputs, output)
    
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
        loss='binary_crossentropy',
        metrics=[
            tf.keras.metrics.AUC(curve='PR', name='auprc'),
            tf.keras.metrics.AUC(curve='ROC', name='auroc'),
            tf.keras.metrics.Precision(name='precision'),
            tf.keras.metrics.Recall(name='recall'),
        ]
    )
    return model

def train_dnn(train_ds, val_ds, hyperparams):
    # train_ds is tf.data.Dataset from TFRecords
    model = build_dnn_model(input_dim=NUM_FEATURES)
    
    # Class weight: ~50x for fraud
    pos_weight = 50.0
    class_weight = {0: 1.0, 1: pos_weight}
    
    callbacks = [
        tf.keras.callbacks.EarlyStopping(
            monitor='val_auprc', mode='max', patience=5, restore_best_weights=True
        ),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor='val_auprc', mode='max', factor=0.5, patience=3, min_lr=1e-6
        ),
        tf.keras.callbacks.ModelCheckpoint(
            filepath=f'models/dnn/{version}/checkpoint',
            save_best_only=True, monitor='val_auprc', mode='max'
        ),
    ]
    
    history = model.fit(
        train_ds.batch(512).prefetch(tf.data.AUTOTUNE),
        validation_data=val_ds.batch(512),
        epochs=30,
        class_weight=class_weight,
        callbacks=callbacks,
    )
    
    return model, history
```

**Wrapped SavedModel for serving:** The TF Serving graph needs to apply `preprocessing_fn` before the DNN, so raw order data can be POSTed and the graph handles the transform. Build a serving model:

```python
def build_serving_model(transform_fn_path, dnn_model):
    transform_layer = tft.TFTransformOutput(transform_fn_path).transform_features_layer()
    
    @tf.function
    def serve(raw_features):
        transformed = transform_layer(raw_features)
        # Concatenate transformed features into model input vector
        x = tf.concat([tf.cast(transformed[f], tf.float32) for f in FEATURE_ORDER], axis=-1)
        return dnn_model(x)
    
    # Wrap and save as a SavedModel with serving signature
    ...
```

Save to `models/dnn/{version}/saved_model/` ready for TF Serving.

## ml/training/evaluate.py

```python
def evaluate(model_path, test_data, ground_truth_categories):
    """
    Computes metrics on held-out test set.
    Produces a report saved to ml/training/reports/{version}/.
    """
    
    # Headline metrics
    metrics = {
        'auprc': average_precision_score(y_test, y_pred),
        'auroc': roc_auc_score(y_test, y_pred),
        'precision_at_95_recall': precision_at_recall(y_test, y_pred, 0.95),
        'recall_at_99_precision': recall_at_precision(y_test, y_pred, 0.99),
        'brier_score': brier_score_loss(y_test, y_pred),
    }
    
    # Confusion matrices at multiple thresholds
    for threshold in [0.3, 0.5, 0.7, 0.85]:
        cm = confusion_matrix(y_test, y_pred >= threshold)
        metrics[f'cm_at_{threshold}'] = cm.tolist()
    
    # Per-category recall (using ground truth categories)
    for category in ['stolen_card', 'account_takeover', 'promo_abuse', 
                     'refund_abuse', 'collusive_merchant', 'triangulation', 'reseller']:
        mask = ground_truth_categories == category
        if mask.sum() > 0:
            cat_recall = (y_pred[mask] >= 0.5).mean()
            metrics[f'recall_{category}'] = float(cat_recall)
    
    # Score distribution plots (matplotlib → PNG)
    plot_score_distributions(y_test, y_pred, save_to=f'{report_dir}/score_dist.png')
    plot_pr_curve(y_test, y_pred, save_to=f'{report_dir}/pr_curve.png')
    plot_roc_curve(y_test, y_pred, save_to=f'{report_dir}/roc_curve.png')
    
    # Calibration plot
    plot_calibration(y_test, y_pred, save_to=f'{report_dir}/calibration.png')
    
    # Save metrics JSON + a human-readable markdown report
    save_report(metrics, report_dir)
    
    return metrics
```

### Ensemble evaluation

After both XGBoost and DNN train, also evaluate the ensemble:

```python
def evaluate_ensemble(xgb_scores, dnn_scores, y_test, weights=(0.6, 0.4)):
    ensemble_scores = weights[0] * xgb_scores + weights[1] * dnn_scores
    return compute_metrics(y_test, ensemble_scores)
```

Try weight grids (0.3/0.7, 0.4/0.6, 0.5/0.5, 0.6/0.4, 0.7/0.3) and pick the one with best val AUPRC.

## ml/registry/model_registry.py

```python
class ModelRegistry:
    """Versioned local filesystem registry. In prod, this would be S3/GCS + a DB index."""
    
    def __init__(self, root='/var/lib/models'):
        self.root = Path(root)
    
    def register(self, model_type: str, version: str, 
                 artifacts: dict[str, Path], metadata: dict):
        """
        model_type: 'xgboost' | 'dnn' | 'ensemble_config'
        version: e.g. 'v20260524_153000'
        artifacts: {filename: source_path}
        Copies artifacts to {root}/{model_type}/{version}/, writes metadata.json
        """
    
    def list_versions(self, model_type: str) -> list[str]:
        ...
    
    def get_latest(self, model_type: str) -> str:
        ...
    
    def get_production(self, model_type: str) -> str:
        """Reads the 'production' symlink under {root}/{model_type}/"""
    
    def promote_to_production(self, model_type: str, version: str):
        """Updates the 'production' symlink atomically."""
```

Production symlink: `models/dnn/production -> v20260524_153000/`. TF Serving is configured to watch the `production` symlink and auto-reload when it changes.

## ml/training/promote.py

```python
def promote(version, model_type, force=False):
    """
    Promotion gate:
    1. Load eval metrics for candidate version
    2. Load eval metrics for current production
    3. Promote only if:
       - candidate AUPRC >= production AUPRC - 0.01 (small regression allowed)
       - AND candidate per-category recall doesn't drop >10% on any category
       - OR --force flag is set
    4. Run a small live A/B shadow test (optional, log only)
    5. Update production symlink
    6. Log promotion event
    """
```

## Orchestration: full training pipeline

`Makefile` target:

```make
train: ## Run full training pipeline
	python -m ml.training.data_loader --output ml/data/raw.parquet
	python -m ml.transform.run_transform \
	    --input ml/data/raw.parquet \
	    --output ml/data/transformed/$(VERSION)
	python -m ml.training.train_xgboost --version $(VERSION)
	python -m ml.training.train_dnn --version $(VERSION)
	python -m ml.training.evaluate --version $(VERSION)

promote: ## Promote a version to production
	python -m ml.training.promote --version $(VERSION) --model-type dnn
	python -m ml.training.promote --version $(VERSION) --model-type xgboost
```

VERSION is autogenerated if not set: `v$(date +%Y%m%d_%H%M%S)`.

## Tests

**tests/test_training_pipeline.py**

Use a small synthetic dataset (10K orders, 200 fraud, generated in fixture).

- `test_data_loader_excludes_unfinalised`: orders <45 days old are excluded
- `test_data_loader_no_future_leakage`: velocity columns for an order only reflect data BEFORE that order's placed_at (verify by creating two orders with known timing and checking values)
- `test_preprocessing_fn_handles_oov`: pass an unseen `card_brand` value, assert it goes to OOV bucket
- `test_xgboost_trains_and_predicts`: trains on small data, predict on val, returns floats in [0,1]
- `test_dnn_trains_one_epoch`: minimal training run completes, model produces predictions
- `test_evaluate_produces_all_metrics`: report dict contains all expected keys
- `test_ensemble_weights_search`: tries all weight combos, returns best
- `test_promote_blocks_regression`: candidate with worse AUPRC fails promotion (without --force)
- `test_savedmodel_serving_signature_works`: load SavedModel, call with raw inputs, assert output shape correct

## Acceptance Criteria for Phase 5

- Full training pipeline (`make train VERSION=test1`) completes end-to-end on real (simulated) data
- XGBoost test AUPRC ≥ 0.70
- DNN test AUPRC ≥ 0.65
- Ensemble test AUPRC ≥ 0.75
- Per-category recall meets targets:
  - stolen_card ≥ 0.70
  - account_takeover ≥ 0.60
  - promo_abuse ≥ 0.80
  - others ≥ 0.40
- Model registry produces versioned artefacts; `promote` correctly gates regressions
- SavedModel includes the TF Transform preprocessing layer (verify by inspecting `saved_model_cli show`)
- Training reports include all required plots and JSON
- All tests pass

## Out of Scope for Phase 5

- Online scoring (Phase 6)
- TF Serving runtime (Phase 6)
- Dashboards (Phase 7)
- Automated retraining schedule (Phase 7)
