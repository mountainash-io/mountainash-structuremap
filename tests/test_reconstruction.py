import pytest

from mountainash_structuremap import StructureMapError, decompose, derive, prepare, reconstruct
from tests.fixtures.models import Address, Document, Person


def test_position_outside_prepared_i64_schema_has_input_location():
    prepared = prepare(derive(Document))
    tables = decompose(Document([Person("Ada", Address("Oslo"), ["x"])]), prepared)
    tables["people__tags"][0]["_sm_position"] = 2**63

    with pytest.raises(StructureMapError) as caught:
        reconstruct(tables, prepared)

    assert caught.value.code == "invalid_position"
    assert caught.value.table == "people__tags"
    assert caught.value.row == 0
    assert caught.value.column == "_sm_position"
