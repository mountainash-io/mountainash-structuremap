import subprocess
import sys
from dataclasses import dataclass

import pytest

from mountainash_structuremap import MappingSpec, MappingUse, UseOverride, decompose, derive, prepare, reconstruct
from mountainash_structuremap.mapping import TypeCast
from tests.fixtures.topology import Address, Branch, Uses
from tests.test_shared_alternatives import Counted, SharedDocument


@dataclass
class OptionalAmount:
    amount: str | None


@pytest.mark.parametrize('value,cell', [(None, None), ('01', 1)])
def test_nullable_scalar_cast_retains_null_and_native_type(value, cell):
    plan = prepare(derive(OptionalAmount), MappingSpec(overrides={
        'amount': UseOverride(cast=TypeCast(to='i64')),
    }))
    rows = decompose(OptionalAmount(value), plan)
    assert rows['root'][0]['amount'] == cell
    assert reconstruct(rows, plan) == OptionalAmount(None if cell is None else '1')


@pytest.mark.parametrize('path,expected', [
    ('left/label', ['0', 1, '2', '3']),
    ('left/left/label', ['0', '1', 2, '3']),
    ('@Branch/left/left/label', ['0', '1', 2, 3]),
])
def test_recursive_cast_override_is_local_to_its_authored_path(path, expected):
    plan = prepare(derive(Branch), MappingSpec(overrides={path: UseOverride(cast=TypeCast(to='i64'))}))
    native = Branch('0', Branch('1', Branch('2', Branch('3'))))
    rows = decompose(native, plan)
    cells = [row['label'] for table in rows.values() for row in table]
    assert sorted((str(cell), type(cell).__name__) for cell in cells) == sorted(
        (str(cell), type(cell).__name__) for cell in expected)
    assert reconstruct(rows, plan) == native


def test_definition_selector_suffix_survives_nested_record_boundary():
    plan = prepare(derive(Uses), MappingSpec(overrides={
        '@Uses/home/city': UseOverride(column='selected_city'),
    }))
    rows = decompose(Uses(Address('Oslo', []), Address('Kyoto', [])), plan)
    assert rows['root'] == [{'_sm_key': 'r0', 'selected_city': 'Oslo', 'work_city': 'Kyoto'}]


def test_sequence_component_options_reach_alternative_payload():
    plan = prepare(derive(SharedDocument), MappingSpec(use=MappingUse(
        component='core.record', children={'items': MappingUse(
            component='core.sequence', config={'variant_layout': 'shared'},
        )},
    )))
    rows = decompose(SharedDocument([Counted('shared', 7)]), plan)
    assert rows['items'] == [{'_sm_key': 'r1', '_sm_parent': 'r0', '_sm_position': 0,
                             '_sm_variant': 'Counted', 'common': 'shared', 'count': 7, 'flag': None}]


def test_component_apply_alias_cycle_fails_without_hanging():
    source = '''
from mountainash_structuremap import MappingRef, MappingSpec, MappingUse, StructureMapError, derive, prepare
from tests.fixtures.topology import Address
class Loop:
    identity = 'test.loop'
    directions = frozenset({'decompose', 'reconstruct'})
    def prepare(self, use, context):
        context.apply(MappingRef(ref='loop'))
try:
    prepare(derive(Address), MappingSpec(use=MappingRef(ref='loop'),
        definitions={'loop': MappingUse(component='test.loop')}), components=(Loop(),))
except StructureMapError as error:
    assert error.code == 'mapping_reference_cycle', error
else:
    raise AssertionError('delegation cycle accepted')
'''
    subprocess.run([sys.executable, '-c', source], check=True, timeout=10)
