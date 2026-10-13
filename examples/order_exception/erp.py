"""Mock ERP order system. Every write calls agcp.require_grant(): it cannot be driven outside a guard."""
from __future__ import annotations

import copy
import csv
from pathlib import Path

from agcp import require_grant


class MockOrderERP:
    def __init__(self, csv_path: Path):
        self.orders: dict[str, dict] = {}
        self.writes: list[tuple] = []
        self.fail_promise_for: set[str] = set()   # fault injection for the saga demo
        with open(csv_path) as f:
            for r in csv.DictReader(f):
                oid = f"SO-{r['Order Id']}"
                self.orders[oid] = {
                    "order_id": oid, "status": r["Order Status"], "sales": float(r["Sales"]),
                    "qty": int(r["Order Item Quantity"]), "shipping_mode": r["Shipping Mode"],
                    "late_delivery_risk": int(r["Late_delivery_risk"]),
                    "days_scheduled": int(r["Days for shipment (scheduled)"]),
                    "segment": r["Customer Segment"], "region": r["Order Region"],
                    "reservation": None, "promise_offset_days": 0}

    def get(self, oid: str) -> dict:
        return copy.deepcopy(self.orders[oid])

    def _write(self, action, oid, **changes):
        g = require_grant(action)
        self.orders[oid].update(changes)
        self.writes.append((g.request_id, action, oid, changes))

    # --- protected writes ---
    def release_hold(self, oid):
        self._write("erp.release_credit_hold", oid, status="PENDING")

    def set_shipping(self, oid, mode):
        self._write("erp.upgrade_shipping", oid, shipping_mode=mode)

    def reserve(self, oid, warehouse, qty):
        self._write("erp.reserve_stock", oid, reservation={"warehouse": warehouse, "qty": qty})

    def set_promise(self, oid, offset_days):
        if oid in self.fail_promise_for:
            raise RuntimeError("ERP timeout on promise-date update")
        self._write("erp.update_promise_date", oid, promise_offset_days=offset_days)

    def cancel(self, oid):
        self._write("erp.cancel_order", oid, status="CANCELED")

    def restore(self, oid, snapshot: dict):
        """Compensating write: put the order back to its before-image."""
        g = require_grant()
        self.orders[oid] = copy.deepcopy(snapshot)
        self.writes.append((g.request_id, "compensate", oid, {}))
