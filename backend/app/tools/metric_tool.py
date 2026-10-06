from backend.app.data_management.metric_versions import pinned_execution
from backend.app.semantic.metrics import get_metric


@pinned_execution
def resolve_metric(metric_code: str) -> dict:
    metric = get_metric(metric_code)

    if metric is None:
        raise ValueError(f"Unknown metric: {metric_code}")

    return metric