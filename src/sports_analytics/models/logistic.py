"""Multinomial logistic regression fit (sklearn) and NumPy inference."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from importlib.metadata import version as package_version

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from sports_analytics.core.exceptions import ModelError
from sports_analytics.core.settings import MAX_DETERMINISTIC_SEED
from sports_analytics.features.contracts import OutcomeSpace

SUPPORTED_SOLVER_PENALTIES: frozenset[tuple[str, str]] = frozenset(
    {
        ("lbfgs", "l2"),
        ("lbfgs", "none"),
        ("newton-cg", "l2"),
        ("newton-cg", "none"),
        ("sag", "l2"),
        ("sag", "none"),
        ("saga", "l1"),
        ("saga", "l2"),
        ("saga", "none"),
        ("liblinear", "l1"),
        ("liblinear", "l2"),
    }
)


@dataclass(frozen=True, slots=True)
class LogisticConfiguration:
    """Explicit, versioned multinomial logistic regression settings."""

    configuration_version: str = "logistic-configuration-v1"
    solver: str = "lbfgs"
    penalty: str = "l2"
    regularization_strength: float = 1.0
    tolerance: float = 1e-4
    maximum_iterations: int = 2000
    fit_intercept: bool = True
    random_seed: int = 42
    feature_scaler_policy: str = "standard-zero-scale-to-one"

    def validate(self, *, outcome_count: int | None = None) -> None:
        """Reject unsupported or malformed logistic configuration values."""
        if self.configuration_version != "logistic-configuration-v1":
            msg = f"unsupported logistic configuration version: {self.configuration_version}"
            raise ModelError(msg)
        if (self.solver, self.penalty) not in SUPPORTED_SOLVER_PENALTIES:
            msg = (
                "unsupported logistic solver/penalty combination: "
                f"solver={self.solver!r} penalty={self.penalty!r}"
            )
            raise ModelError(msg)
        if outcome_count is not None and self.solver == "liblinear" and outcome_count != 2:
            msg = "liblinear solver is only supported for binary outcome spaces"
            raise ModelError(msg)
        if not np.isfinite(self.regularization_strength) or self.regularization_strength <= 0.0:
            msg = "regularization_strength must be a positive finite scalar"
            raise ModelError(msg)
        if not np.isfinite(self.tolerance) or self.tolerance <= 0.0:
            msg = "tolerance must be a positive finite scalar"
            raise ModelError(msg)
        if type(self.maximum_iterations) is not int or isinstance(self.maximum_iterations, bool):
            msg = "maximum_iterations must be a positive integer"
            raise ModelError(msg)
        if self.maximum_iterations < 1:
            msg = "maximum_iterations must be a positive integer"
            raise ModelError(msg)
        if type(self.fit_intercept) is not bool:
            msg = "fit_intercept must be a boolean"
            raise ModelError(msg)
        if type(self.random_seed) is not int or isinstance(self.random_seed, bool):
            msg = "random_seed must be an integer"
            raise ModelError(msg)
        if self.random_seed < 0 or self.random_seed > MAX_DETERMINISTIC_SEED:
            msg = (
                "random_seed must be within the supported deterministic range "
                f"[0, {MAX_DETERMINISTIC_SEED}]"
            )
            raise ModelError(msg)
        if self.feature_scaler_policy != "standard-zero-scale-to-one":
            msg = f"unsupported feature scaler policy: {self.feature_scaler_policy}"
            raise ModelError(msg)


@dataclass(frozen=True, slots=True)
class FittedLogisticParameters:
    """Explicit logistic parameters in canonical outcome order."""

    feature_names: tuple[str, ...]
    outcome_labels: tuple[str, ...]
    scaler_mean: tuple[float, ...]
    scaler_scale: tuple[float, ...]
    coefficients: tuple[tuple[float, ...], ...]
    intercepts: tuple[float, ...]
    configuration: LogisticConfiguration
    sklearn_version: str
    numpy_version: str
    convergence_iterations: tuple[int, ...]


def fit_multinomial_logistic(
    *,
    feature_matrix: np.ndarray,
    labels: tuple[str, ...],
    feature_names: tuple[str, ...],
    outcome_space: OutcomeSpace,
    configuration: LogisticConfiguration | None = None,
) -> FittedLogisticParameters:
    """Fit a multinomial logistic model and return explicit parameters."""
    config = configuration or LogisticConfiguration(random_seed=42)
    ordered_labels = outcome_space.ordered_labels
    config.validate(outcome_count=len(ordered_labels))
    if feature_matrix.ndim != 2:
        msg = "feature matrix must be 2-dimensional"
        raise ModelError(msg)
    if feature_matrix.shape[0] != len(labels):
        msg = "feature matrix and labels length mismatch"
        raise ModelError(msg)
    if feature_matrix.shape[1] != len(feature_names):
        msg = "feature matrix width does not match feature_names"
        raise ModelError(msg)
    unique = set(labels)
    if unique != set(ordered_labels):
        msg = (
            "training labels must include every outcome in the supplied outcome space; "
            f"expected {ordered_labels}, found {sorted(unique)}"
        )
        raise ModelError(msg)
    if not np.isfinite(feature_matrix).all():
        msg = "feature matrix contains non-finite values"
        raise ModelError(msg)

    scaler = StandardScaler()
    scaled = scaler.fit_transform(feature_matrix)
    scale = np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
    if not np.isfinite(scaler.mean_).all():
        msg = "fitted scaler means contain non-finite values"
        raise ModelError(msg)
    if not np.isfinite(scale).all() or np.any(scale <= 0):
        msg = "fitted scaler scales must be positive and finite"
        raise ModelError(msg)

    penalty = None if config.penalty == "none" else config.penalty
    model = LogisticRegression(
        solver=config.solver,
        penalty=penalty,
        C=config.regularization_strength,
        tol=config.tolerance,
        max_iter=config.maximum_iterations,
        fit_intercept=config.fit_intercept,
        random_state=config.random_seed,
    )
    try:
        with warnings.catch_warnings():
            # Persist and pass the configured penalty explicitly, while ignoring the
            # sklearn 1.8 FutureWarning that deprecates setting penalty by name.
            warnings.filterwarnings(
                "ignore",
                message=r".*penalty.*deprecated.*",
                category=FutureWarning,
            )
            warnings.simplefilter("error", ConvergenceWarning)
            model.fit(scaled, np.asarray(labels))
    except ConvergenceWarning as exc:
        msg = "logistic regression did not converge"
        raise ModelError(msg) from exc
    except (TypeError, ValueError) as exc:
        msg = f"logistic regression configuration is invalid: {exc}"
        raise ModelError(msg) from exc
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ConvergenceWarning):
            msg = "logistic regression did not converge"
            raise ModelError(msg) from exc
        msg = f"logistic regression fit failed: {exc}"
        raise ModelError(msg) from exc

    if any(int(iteration) >= config.maximum_iterations for iteration in model.n_iter_):
        msg = "logistic regression did not converge within the configured maximum iterations"
        raise ModelError(msg)
    if not np.isfinite(model.coef_).all() or not np.isfinite(model.intercept_).all():
        msg = "fitted logistic parameters contain non-finite values"
        raise ModelError(msg)

    class_order = tuple(str(item) for item in model.classes_)
    coef_matrix, intercept_vector = _explicit_coefficient_matrix(
        coef=np.asarray(model.coef_, dtype=np.float64),
        intercept=np.asarray(model.intercept_, dtype=np.float64),
        class_order=class_order,
        ordered_labels=ordered_labels,
        feature_count=len(feature_names),
    )
    return FittedLogisticParameters(
        feature_names=feature_names,
        outcome_labels=ordered_labels,
        scaler_mean=tuple(float(value) for value in scaler.mean_),
        scaler_scale=tuple(float(value) for value in scale),
        coefficients=coef_matrix,
        intercepts=intercept_vector,
        configuration=config,
        sklearn_version=package_version("scikit-learn"),
        numpy_version=np.__version__,
        convergence_iterations=tuple(int(value) for value in model.n_iter_),
    )


def logits_from_parameters(
    *,
    feature_vector: np.ndarray,
    parameters: FittedLogisticParameters,
) -> np.ndarray:
    """Compute raw logits for one or more rows using explicit parameters."""
    matrix = np.asarray(feature_vector, dtype=np.float64)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.shape[1] != len(parameters.feature_names):
        msg = "feature vector width does not match model feature_names"
        raise ModelError(msg)
    if not np.isfinite(matrix).all():
        msg = "feature vector contains non-finite values"
        raise ModelError(msg)
    mean = np.asarray(parameters.scaler_mean, dtype=np.float64)
    scale = np.asarray(parameters.scaler_scale, dtype=np.float64)
    if np.any(scale <= 0) or not np.isfinite(scale).all() or not np.isfinite(mean).all():
        msg = "invalid scaler parameters"
        raise ModelError(msg)
    scaled = (matrix - mean) / scale
    coef = np.asarray(parameters.coefficients, dtype=np.float64)
    intercept = np.asarray(parameters.intercepts, dtype=np.float64)
    expected_coef_shape = (len(parameters.outcome_labels), len(parameters.feature_names))
    if coef.ndim != 2 or coef.shape != expected_coef_shape:
        msg = "coefficient matrix has invalid shape"
        raise ModelError(msg)
    if intercept.shape != (len(parameters.outcome_labels),):
        msg = "intercept vector has invalid length"
        raise ModelError(msg)
    if not np.isfinite(coef).all() or not np.isfinite(intercept).all():
        msg = "model coefficients contain non-finite values"
        raise ModelError(msg)
    logits = scaled @ coef.T + intercept
    if not np.isfinite(logits).all():
        msg = "non-finite logits produced during inference"
        raise ModelError(msg)
    return logits


def _explicit_coefficient_matrix(
    *,
    coef: np.ndarray,
    intercept: np.ndarray,
    class_order: tuple[str, ...],
    ordered_labels: tuple[str, ...],
    feature_count: int,
) -> tuple[tuple[tuple[float, ...], ...], tuple[float, ...]]:
    """Expand sklearn coefficients into an explicit outcome × feature matrix."""
    outcome_count = len(ordered_labels)
    if set(class_order) != set(ordered_labels):
        msg = "fitted model is missing a required outcome class"
        raise ModelError(msg)
    if outcome_count == 2:
        if coef.ndim != 2 or coef.shape != (1, feature_count):
            msg = "binary logistic coefficient matrix has unexpected shape"
            raise ModelError(msg)
        if intercept.ndim != 1 or intercept.shape != (1,):
            msg = "binary logistic intercept has unexpected shape"
            raise ModelError(msg)
        # Reproduce sklearn binary decision: class0 logits=0, class1 logits=fitted.
        sklearn_coef = np.vstack(
            [
                np.zeros((1, feature_count), dtype=np.float64),
                coef.astype(np.float64, copy=False),
            ]
        )
        sklearn_intercept = np.asarray([0.0, float(intercept[0])], dtype=np.float64)
    else:
        if coef.ndim != 2 or coef.shape != (outcome_count, feature_count):
            msg = "multiclass logistic coefficient matrix has unexpected shape"
            raise ModelError(msg)
        if intercept.ndim != 1 or intercept.shape != (outcome_count,):
            msg = "multiclass logistic intercept has unexpected shape"
            raise ModelError(msg)
        sklearn_coef = coef.astype(np.float64, copy=False)
        sklearn_intercept = intercept.astype(np.float64, copy=False)

    index_by_label = {label: index for index, label in enumerate(class_order)}
    try:
        ordered_indices = [index_by_label[label] for label in ordered_labels]
    except KeyError as exc:
        msg = "fitted model is missing a required outcome class"
        raise ModelError(msg) from exc
    coefficients = tuple(
        tuple(float(value) for value in sklearn_coef[index]) for index in ordered_indices
    )
    intercepts = tuple(float(sklearn_intercept[index]) for index in ordered_indices)
    return coefficients, intercepts
