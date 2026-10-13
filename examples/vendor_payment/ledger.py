"""Mock AP sub-ledger and bank-transfer gateway. All writes require an agcp execution grant."""
from __future__ import annotations

import copy
import csv
from pathlib import Path

from agcp import require_grant


class MockPayables:
    def __init__(self, csv_path: Path):
        self.invoices: dict[str, dict] = {}
        with open(csv_path) as f:
            for r in csv.DictReader(f):
                self.invoices[r["invoice_id"]] = {
                    "invoice_id": r["invoice_id"], "vendor_id": r["vendor_id"],
                    "vendor_status": r["vendor_status"], "amount": float(r["amount"]),
                    "match_variance_pct": float(r["match_variance_pct"]), "status": "OPEN", "hold": None}
        self.ledger: list[dict] = []
        self.transfers: dict[str, dict] = {}
        self.bank_down_for: set[str] = set()

    def get(self, inv: str) -> dict:
        return copy.deepcopy(self.invoices[inv])

    def state(self, inv: str) -> dict:
        return {"invoice": self.get(inv), "ledger_net": round(sum(e["amount"] for e in self.ledger
                                                                  if e["invoice_id"] == inv), 2),
                "transfer": copy.deepcopy(self.transfers.get(inv))}

    def post(self, inv, amount, memo):
        require_grant()
        self.ledger.append({"invoice_id": inv, "amount": amount, "memo": memo})

    def schedule(self, inv, amount):
        require_grant("ap.schedule_transfer")
        if inv in self.bank_down_for:
            raise ConnectionError("bank gateway unavailable")
        self.transfers[inv] = {"amount": amount, "status": "SCHEDULED"}

    def cancel_transfer(self, inv):
        require_grant()
        self.transfers[inv]["status"] = "CANCELED"

    def set_status(self, inv, status, hold=None):
        require_grant()
        self.invoices[inv].update(status=status, hold=hold)
