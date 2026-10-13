"""Generate the synthetic sample data used by the examples (deterministic, seed=7).

orders_sample.csv  : column names follow the public DataCo Smart Supply Chain schema
                     (Kaggle: shashwatwork/dataco-smart-supply-chain-for-big-data-analysis).
                     Values are randomly generated; no rows are copied from the dataset.
invoices_sample.csv: fully synthetic AP invoices with 3-way-match variance.
"""
import csv
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "sample"


def orders(rng, n=40):
    modes = ["Standard Class", "Second Class", "First Class", "Same Day"]
    regions = ["Western Europe", "Central America", "South Asia", "Oceania", "East of USA"]
    segs = ["Consumer", "Corporate", "Home Office"]
    statuses = ["SUSPECTED_FRAUD"] * 2 + ["ON_HOLD"] * 14 + ["PENDING"] * 14 + ["PROCESSING"] * 10
    for i in range(n):
        sched = rng.choice([2, 4, 5])
        yield {"Order Id": 70000 + i, "Order Status": statuses[i % len(statuses)],
               "Sales": round(rng.choice([rng.uniform(80, 1900), rng.uniform(2000, 14000)]), 2),
               "Order Item Quantity": rng.randint(1, 5), "Shipping Mode": rng.choice(modes),
               "Days for shipment (scheduled)": sched,
               "Days for shipping (real)": sched + rng.choice([-1, 0, 1, 2, 3]),
               "Late_delivery_risk": rng.choice([0, 1, 1]), "Order Region": rng.choice(regions),
               "Customer Segment": rng.choice(segs), "Customer Id": 9000 + rng.randint(1, 300)}


def invoices(rng, n=25):
    for i in range(n):
        amt = round(rng.choice([rng.uniform(300, 4900), rng.uniform(5200, 60000)]), 2)
        var = rng.choice([0.0, 0.4, 1.1, 1.8, 3.5, 12.0])
        yield {"invoice_id": f"INV-{5000 + i}", "vendor_id": f"V-{100 + i % 9}",
               "vendor_status": "BLOCKED" if i % 9 == 8 else "ACTIVE", "amount": amt,
               "po_amount": round(amt / (1 + var / 100), 2), "match_variance_pct": var,
               "due_in_days": rng.randint(-2, 30)}


def write(name, rows):
    rows = list(rows)
    with (OUT / name).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {name}: {len(rows)} rows")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(7)
    write("orders_sample.csv", orders(rng))
    write("invoices_sample.csv", invoices(rng))
