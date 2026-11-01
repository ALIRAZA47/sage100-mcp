# Reconciling the canned views to Sage's reports

The M3 exit gate (SPEC.md §18) is that each canned view for the **ABC sample company** matches Sage's own
report for the same options **to the cent**. `tests/windows/test_m3_reconciliation.py` checks this on the
self-hosted Windows runner. It runs each view with the arguments a *golden* records, then compares every
row and every total with the report. A golden is a CSV export of the report.

ABC is Sage's fictional sample company, so its report exports are not customer data. Never use a real
company's reports for this.

## Without exports: the figures Sage keeps

Sage maintains several of the views' figures on its own master records as it posts and ages.
`scripts/verify_views.py` compares the views with them, row by row and to the cent, through the server's
own code:

| View | Compared with |
| --- | --- |
| `ar_aging`, as of each customer's `DateLastAging` | `AR_Customer.CurrentBalance` and `AgingCategory1-4` |
| `open_sales_orders`, one row per order, charge and comment lines kept | `AR_Customer.OpenOrderAmt` |
| `ap_aging` | `AP_Vendor.BalanceDue` |
| `inventory_availability`, warehouse by warehouse | `CI_Item.TotalQuantityOnHand` |
| `gl_trial_balance`, each period of the current year | debits equal credits wherever the posting history balances |

```bash
uv run python scripts/verify_views.py --company ABC
```

It prints counts and codes only, so it is also safe to run on a real company. Some customers are counted
apart rather than compared:

- customers whose open invoices changed on or after their aging date;
- customers whose stored aging no longer adds up to their open invoices.

The Windows gate runs it on ABC first. The same comparisons, run on MAS_EMP as plain SQL, all agreed
(docs/decisions.md D49). The report exports below then cover what Sage stores nowhere: invoice-level
aging, open purchase orders, and sales history.

## 1. Export the reports

Run each report in Sage 100 for company ABC, export it (**Export** in the report preview, as CSV or Excel),
and save it as one CSV per view. Then reshape the CSV:

- **Header row:** the first row names the columns as the view names them. Run the view once, through
  Claude or any MCP client, to see its column names. The aging buckets are named after ABC's own aging
  categories, such as `current`, `days_1_30` and `days_91_plus`.
- **Rows:** one row per key, and nothing else. Remove report headings, subtotal and grand-total lines,
  and blank lines.
- **Amounts:** keep them as the report prints them. `1,234.56`, `$1,234.56`, `(1,234.56)` and
  `1,234.56-` are all read correctly.
- **Keys:** split combined keys into the view's columns. A customer printed as `01-ABF` becomes
  `ARDivisionNo` `01` and `CustomerNo` `ABF`.

| View | Sage report (menu) | Report options | Key columns | Compare |
| --- | --- | --- | --- | --- |
| `ar_aging` | Accounts Receivable > Reports > Aged Invoice Report | Summary; the aging date; age by the AR Options setting | `ARDivisionNo`, `CustomerNo` | every bucket, `total` |
| `ap_aging` | Accounts Payable > Reports > Aged Invoice Report | Summary; the aging date; age by due date; include invoices on hold | `APDivisionNo`, `VendorNo` | every bucket, `total` |
| `open_sales_orders` | Sales Order > Reports > Open Sales Order Report | Standard and back orders; by order number | `SalesOrderNo` | `open_value` (run with `lines: false`) |
| `open_purchase_orders` | Purchase Order > Reports > Open Purchase Order Report | All open orders; by PO number | `PurchaseOrderNo` | `open_value` (run with `lines: false`) |
| `inventory_availability` | Inventory Management > Reports > Stock Status Report (or Item Inquiry, Quantity tab, for a sample) | All warehouses | `ItemCode`, `WarehouseCode` | `on_hand`, `available` |
| `gl_trial_balance` | General Ledger > Reports > Trial Balance | The fiscal year and period; accounts with balances | `Account` | `debit`, `credit` |
| `sales_history` | Sales Order > Reports > Sales Analysis Report | By customer; the invoice date range | `group_key` | `revenue` |

Use the same aging date, period or date range in the report and in `manifest.yaml`. Balances in the aging
views are current: export the aging reports on the day you run the test, or before anyone posts to ABC.

## 2. Write `manifest.yaml`

Put the CSV files and the manifest in `tests/reconciliation/abc/`, or in a directory named by
`SAGE100_MCP_TEST_GOLDENS`.

```yaml
company: ABC
views:
  ar_aging:
    report: A/R Aged Invoice Report, summary, aging date 2030-03-31
    file: ar_aging.csv
    args: {as_of: "2030-03-31"}
    key: [ARDivisionNo, CustomerNo]
    compare: [current, days_1_30, days_31_60, days_61_90, days_91_plus, total]
  open_sales_orders:
    report: Open Sales Order Report, standard and back orders
    file: open_sales_orders.csv
    args: {lines: false}
    key: [SalesOrderNo]
    compare: [open_value]
  gl_trial_balance:
    report: Trial Balance, fiscal 2030 period 03
    file: gl_trial_balance.csv
    args: {fiscal_year: "2030", period: "3"}
    key: [Account]
    compare: [debit, credit]
  # ... one entry for each of the seven views
```

`args` are the view's own arguments, as its tool takes them. The gate fails if any of the seven views has
no entry.

## 3. Run the gate

On the runner, with the settings `tests/windows/test_m2_gate.py` describes:

```bash
uv run pytest -m windows tests/windows/test_m3_reconciliation.py -ra
```

A difference is reported per row and per total, for example
`01/ABF days_31_60: view 1250.00, report 1251.00`, then `total days_31_60: … off by -1.00`. Before
changing a view, check that the report used the same options. If a difference is a real Sage rule the view
misses, record it in `docs/decisions.md`, fix both plans, and extend the parity test
(`tests/premium/test_view_parity.py`) with a case that shows the rule.

Commit the goldens once they reconcile, so later builds are held to them.
