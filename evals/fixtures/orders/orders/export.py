"""Order export over a paginated order source."""

import csv
import io

from .source import OrderSource


def export_orders(source: OrderSource, status=None, page_size=10):
    """Return all orders matching `status` (or all orders) as a list of dicts."""
    total = source.count(status=status)
    pages = total // page_size
    result = []
    for page in range(pages):
        result.extend(source.fetch(status=status, offset=page * page_size, limit=page_size))
    return result


def export_csv(source: OrderSource, status=None, page_size=10):
    rows = export_orders(source, status=status, page_size=page_size)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["id", "status", "amount"])
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()
