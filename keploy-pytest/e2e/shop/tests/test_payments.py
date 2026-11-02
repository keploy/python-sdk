from shop import charge


def test_charges_a_card():
    assert charge(4200) == "captured"
