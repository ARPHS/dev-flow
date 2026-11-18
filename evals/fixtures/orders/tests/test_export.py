import unittest

from orders.export import export_csv, export_orders
from orders.source import OrderSource


def make_orders(n, status="paid"):
    return [{"id": i, "status": status, "amount": i * 10} for i in range(n)]


class ExportTest(unittest.TestCase):
    def test_export_all(self):
        source = OrderSource(make_orders(20))
        self.assertEqual(len(export_orders(source)), 20)

    def test_export_filtered(self):
        source = OrderSource(make_orders(10, "paid") + make_orders(10, "refunded"))
        rows = export_orders(source, status="refunded")
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(r["status"] == "refunded" for r in rows))

    def test_export_csv_header(self):
        source = OrderSource(make_orders(10))
        text = export_csv(source)
        self.assertTrue(text.startswith("id,status,amount"))


if __name__ == "__main__":
    unittest.main()
