"""Clean Online Retail II and export InsightAgent-compatible CSV files.

The source has two Excel sheets and no separate confirmation timestamp. For the
learning MVP, InvoiceDate is mapped to both order_date and confirmed_date.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def load_source(path: Path) -> pd.DataFrame:
    sheets = pd.read_excel(path, sheet_name=None)
    frames = []
    for sheet_name, frame in sheets.items():
        frame = frame.copy()
        frame["source_sheet"] = sheet_name
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def clean_source(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int | float]]:
    df = raw.rename(
        columns={
            "Invoice": "invoice_no",
            "StockCode": "stock_code",
            "Description": "description",
            "Quantity": "quantity",
            "InvoiceDate": "invoice_date",
            "Price": "unit_price",
            "Customer ID": "customer_id",
            "Country": "country",
        }
    )
    required = [
        "invoice_no", "stock_code", "description", "quantity", "invoice_date",
        "unit_price", "customer_id", "country",
    ]
    missing = [column for column in required if column not in df]
    if missing:
        raise ValueError(f"Missing source columns: {missing}")

    raw_count = len(df)
    df["invoice_no"] = df["invoice_no"].astype("string").str.strip()
    df["stock_code"] = df["stock_code"].astype("string").str.strip()
    df["invoice_date"] = pd.to_datetime(df["invoice_date"], errors="coerce")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["unit_price"] = pd.to_numeric(df["unit_price"], errors="coerce")
    df["customer_id"] = pd.to_numeric(df["customer_id"], errors="coerce")

    cancelled = df["invoice_no"].str.upper().str.startswith("C", na=False)
    invalid = (
        df["invoice_no"].isna()
        | df["stock_code"].isna()
        | df["invoice_date"].isna()
        | df["quantity"].isna()
        | df["unit_price"].isna()
        | df["customer_id"].isna()
        | (df["quantity"] <= 0)
        | (df["unit_price"] < 0)
        | cancelled
    )
    cleaned = df.loc[~invalid].copy()
    cleaned["customer_id"] = cleaned["customer_id"].astype("int64")
    cleaned["quantity"] = cleaned["quantity"].astype("int64")
    cleaned["invoice_date"] = cleaned["invoice_date"].dt.strftime("%Y-%m-%d %H:%M:%S")
    cleaned["sales_amount"] = (cleaned["quantity"] * cleaned["unit_price"]).round(2)
    cleaned["order_status"] = "completed"
    cleaned["order_date"] = cleaned["invoice_date"].str[:10]
    cleaned["confirmed_date"] = cleaned["order_date"]
    cleaned["region_code"] = cleaned["country"].str.strip().str.upper().str.replace(" ", "_", regex=False)
    cleaned["order_number"] = cleaned["invoice_no"].astype(str)
    cleaned["product_code"] = cleaned["stock_code"].astype(str)
    cleaned["customer_code"] = "CUST_" + cleaned["customer_id"].astype(str)
    cleaned["line_number"] = cleaned.groupby("order_number", sort=False).cumcount() + 1
    cleaned["order_id"] = pd.factorize(cleaned["order_number"])[0] + 1
    cleaned["product_id"] = pd.factorize(cleaned["product_code"])[0] + 1
    cleaned["customer_key"] = pd.factorize(cleaned["customer_code"])[0] + 1
    cleaned["region_id"] = pd.factorize(cleaned["region_code"])[0] + 1
    stats: dict[str, int | float] = {
        "raw_rows": int(raw_count),
        "clean_rows": int(len(cleaned)),
        "cancelled_rows": int(cancelled.sum()),
        "invalid_rows": int(invalid.sum()),
        "orders": int(cleaned["order_number"].nunique()),
        "customers": int(cleaned["customer_code"].nunique()),
        "products": int(cleaned["product_code"].nunique()),
        "regions": int(cleaned["region_code"].nunique()),
        "sales_amount": float(cleaned["sales_amount"].sum()),
    }
    return cleaned, stats


def export(cleaned: pd.DataFrame, stats: dict[str, int | float], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    dates = pd.DataFrame({"date_id": sorted(set(cleaned["order_date"]))})
    dates.to_csv(output / "dim_date.csv", index=False)
    cleaned[["region_id", "region_code", "country"]].drop_duplicates().rename(
        columns={"country": "region_name"}
    ).to_csv(output / "dim_region.csv", index=False)
    cleaned[["customer_key", "customer_code", "customer_id", "country"]].drop_duplicates(
        subset=["customer_key", "customer_code"]
    ).rename(
        columns={"customer_key": "customer_id_generated", "country": "region_code"}
    ).to_csv(output / "dim_customer.csv", index=False)
    cleaned[["product_id", "product_code", "description"]].drop_duplicates(
        subset=["product_id", "product_code"]
    ).to_csv(
        output / "dim_product.csv", index=False
    )
    cleaned[[
        "order_id", "order_number", "customer_key", "region_id", "order_date",
        "confirmed_date", "order_status",
    ]].drop_duplicates().to_csv(output / "fact_sales_order.csv", index=False)
    cleaned[[
        "order_id", "line_number", "customer_key", "product_id", "region_id",
        "order_date", "quantity", "unit_price", "sales_amount",
    ]].to_csv(output / "fact_sales_detail.csv", index=False)
    (output / "import_report.json").write_text(
        json.dumps({
            "source": "UCI Online Retail II",
            "confirmed_date_assumption": "InvoiceDate date mapped to confirmed_date",
            **stats,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/generated/online_retail_ii"))
    args = parser.parse_args()
    cleaned, stats = clean_source(load_source(args.input))
    export(cleaned, stats, args.output)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
