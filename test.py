import numpy as np, pandas as pd
from pathlib import Path
folder = Path.home() / "AlexaWorkspace"
folder.mkdir(exist_ok=True)
rng = np.random.default_rng(1); n = 240
pd.DataFrame({
    "date": pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d"),
    "region": rng.choice(["South", "North", "East", "West"], n),
    "product": rng.choice(list("ABCDE"), n),
    "units": rng.integers(1, 20, n),
    "revenue": rng.normal(5000, 1500, n).round(),
}).to_csv(folder / "sales.csv", index=False)