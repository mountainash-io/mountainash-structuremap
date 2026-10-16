from dataclasses import dataclass

from mountainash_structuremap import MappingSpec, MappingUse, decompose, derive, prepare, reconstruct
from mountainash_structuremap.definitions import Alternatives, Map, Nullable, Positional, RecordRef, Scalar, Sequence
from tests.fixtures.topology import Containers, Detail, Nested


@dataclass
class FamilyDispatcher:
    identity: str = "test.families"
    directions: frozenset[str] = frozenset({"decompose", "reconstruct"})

    def prepare(self, use, context):
        shape = context.shape
        while isinstance(shape, Nullable):
            shape = shape.value
        children = use.children
        if isinstance(shape, Scalar):
            context.value()
        elif isinstance(shape, RecordRef):
            context.record(children)
        elif isinstance(shape, Sequence):
            context.sequence(children.get("item"))
        elif isinstance(shape, Map):
            context.map(key=children.get("key"), value=children.get("value"))
        elif isinstance(shape, Positional):
            context.positional({int(key): value for key, value in children.items()})
        elif isinstance(shape, Alternatives):
            context.alternatives(children)


def component(**children):
    return MappingUse(component="test.families", children=children)


def test_custom_component_composes_nested_sequence_map_record_and_positional_families():
    value = Nested([[], None, [None, 3]], {"z": [2], "": [], "a/b": [None, 3]}, ("x", 2))
    mapping = MappingSpec(use=component(
        grid=component(item=component(item=component())),
        lookup=component(key=component(), value=component(item=component())),
        coordinates=component(**{"0": component(), "1": component()}),
    ))
    prepared = prepare(derive(Nested), mapping, components=(FamilyDispatcher(),))
    assert reconstruct(decompose(value, prepared), prepared) == value


def test_custom_component_composes_nullable_alternatives_and_record_members():
    value = Containers([{}, None, {"x/y": 2}], [(None, []), None, ("", [1, 2])], {"": None, "a/b": Detail(None)})
    mapping = MappingSpec(use=component(
        maps=component(item=component(key=component(), value=component())),
        tuples=component(item=component(**{"0": component(), "1": component(item=component())})),
        mapped=component(key=component(), value=component(label=component(), tags=component(item=component()))),
    ))
    prepared = prepare(derive(Containers), mapping, components=(FamilyDispatcher(),))
    assert reconstruct(decompose(value, prepared), prepared) == value
