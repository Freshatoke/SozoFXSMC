"""
Task 12 Phase 10 -- Persistent experiment registry.

Every large research run gets ONE JSON record under
`reports/robustness/registry/<experiment_id>.json`, containing every
field this task's brief names explicitly. This is what makes an
experiment reproducible: given only the registry record, a later run can
reconstruct the exact dataset slice, parameter space, seed, and
correction method used -- no experiment's provenance depends on anyone's
memory of what they ran.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import pandas as pd

try:
    import config as _config_pkg
    _SOFTWARE_VERSION = getattr(_config_pkg, "__version__", "unversioned")
except Exception:
    _SOFTWARE_VERSION = "unversioned"


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else "unknown (git command failed)"
    except Exception as exc:
        return f"unknown ({exc})"


@dataclass
class ExperimentRecord:
    experiment_id: str
    experiment_name: str
    timestamp: str
    git_commit: str
    software_version: str
    dataset_identity: str          # e.g. "EURUSD_M1.parquet"
    symbols: list
    date_range: tuple
    parameter_search_space: dict
    n_configurations: int
    random_seed: int
    train_dates: tuple
    validation_dates: tuple
    out_of_sample_dates: tuple
    statistical_correction_method: list    # e.g. ["bonferroni", "benjamini_hochberg"]
    results_location: str
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def new_experiment_id(experiment_name: str) -> str:
    return f"EXP_{experiment_name}_{uuid.uuid4().hex[:8]}"


def register_experiment(
    experiment_name: str, dataset_identity: str, symbols: list, date_range: tuple,
    parameter_search_space: dict, n_configurations: int, random_seed: int,
    train_dates: tuple, validation_dates: tuple, out_of_sample_dates: tuple,
    statistical_correction_method: list, results_location: str, notes: str = "",
    registry_dir: str = "reports/robustness/registry",
) -> ExperimentRecord:
    record = ExperimentRecord(
        experiment_id=new_experiment_id(experiment_name), experiment_name=experiment_name,
        timestamp=str(pd.Timestamp.now(tz="UTC")), git_commit=_git_commit(), software_version=_SOFTWARE_VERSION,
        dataset_identity=dataset_identity, symbols=symbols, date_range=tuple(str(d) for d in date_range),
        parameter_search_space=parameter_search_space, n_configurations=n_configurations, random_seed=random_seed,
        train_dates=tuple(str(d) for d in train_dates), validation_dates=tuple(str(d) for d in validation_dates),
        out_of_sample_dates=tuple(str(d) for d in out_of_sample_dates),
        statistical_correction_method=statistical_correction_method, results_location=results_location, notes=notes,
    )
    path = Path(registry_dir) / f"{record.experiment_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), indent=2, default=str), encoding="utf-8")
    return record


def load_experiment(experiment_id: str, registry_dir: str = "reports/robustness/registry") -> Optional[dict]:
    path = Path(registry_dir) / f"{experiment_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_experiments(registry_dir: str = "reports/robustness/registry") -> list:
    d = Path(registry_dir)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("EXP_*.json"))
