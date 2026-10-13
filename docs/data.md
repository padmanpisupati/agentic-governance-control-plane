# Data notes

All data in this package is **synthetic**. No real customer, supplier, or company data is used.

| File | Rows | Generator | Schema source |
|---|---|---|---|
| `data/sample/orders_sample.csv` | 40 | `scripts/generate_sample.py` (seed 7) | Column names follow the public **DataCo Smart Supply Chain** dataset, Kaggle `shashwatwork/dataco-smart-supply-chain-for-big-data-analysis` (<https://www.kaggle.com/datasets/shashwatwork/dataco-smart-supply-chain-for-big-data-analysis>). Values are randomly generated, and no rows are copied. `Order Status` values (`ON_HOLD`, `SUSPECTED_FRAUD`, `PENDING`, `PROCESSING`) mirror the dataset's status vocabulary. |
| `data/sample/invoices_sample.csv` | 25 | same script | Fully synthetic accounts-payable invoices: amount, PO amount, 3-way-match variance %, vendor status. |

Columns used from the DataCo schema: `Order Id`, `Order Status`, `Sales`, `Order Item Quantity`,
`Shipping Mode`, `Days for shipment (scheduled)`, `Days for shipping (real)`, `Late_delivery_risk`,
`Order Region`, `Customer Segment`, `Customer Id`.

To run against the real public dataset, download it from Kaggle under its license into `data/raw/`
(git-ignored) and map the same columns in `examples/order_exception/erp.py`. The control plane itself
never reads business data. It only sees the action context that adapters build.

**Interpretation:** the example outputs show control mechanics (what was allowed, held, denied,
compensated, and whether the trail verifies). They are not business-outcome benchmarks.
