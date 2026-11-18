class OrderSource:
    """In-memory stand-in for a paginated order API."""

    def __init__(self, orders):
        self._orders = list(orders)

    def _filtered(self, status):
        if status is None:
            return self._orders
        return [o for o in self._orders if o["status"] == status]

    def count(self, status=None):
        return len(self._filtered(status))

    def fetch(self, status=None, offset=0, limit=10):
        return self._filtered(status)[offset:offset + limit]
