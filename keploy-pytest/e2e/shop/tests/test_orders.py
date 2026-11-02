import os

from shop import price, reserve

# SHOP_CHANGE: the change a replay of e2e/lane.sh makes to the code under
# test. miss: the price test asks for a pen, which no recorded call answers.
# unconsumed: the reserve test no longer makes its recorded call.
CHANGE = os.environ.get("SHOP_CHANGE", "")


class TestOrders:
    def test_prices_a_book(self):
        assert price("pen" if CHANGE == "miss" else "book") == (150 if CHANGE == "miss" else 4200)

    def test_reserves_a_book(self):
        if CHANGE == "unconsumed":
            return
        assert reserve("book") is True
