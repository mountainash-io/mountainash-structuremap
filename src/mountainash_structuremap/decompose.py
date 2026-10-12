from __future__ import annotations

from typing import TypeVar, cast

from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.prepared import (
    Cell,
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
)

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1
T = TypeVar("T")
_PYTHON_TYPES = {"str": str, "int": int, "float": float, "bool": bool}


def _data_error(code: str, message: str, *, path: str, remedy: str) -> StructureMapError:
    return StructureMapError(code, message, path=path, owner="executor restriction", remedy=remedy)


def _native_scalar(value: object, program: ScalarProgram) -> Cell:
    if value is None:
        if program.nullable:
            return None
        raise _data_error(
            "null_not_allowed",
            "Non-nullable scalar has a null value",
            path=program.path,
            remedy="Supply a value or declare the member nullable",
        )
    expected = _PYTHON_TYPES[program.scalar_type]
    if type(value) is not expected:
        raise _data_error(
            "native_type_mismatch",
            f"Expected {expected.__name__}, got {type(value).__name__}",
            path=program.path,
            remedy=f"Supply a native {expected.__name__} value",
        )
    if isinstance(value, int) and not _I64_MIN <= value <= _I64_MAX:
        raise _data_error(
            "value_out_of_range",
            "Integer does not fit signed i64",
            path=program.path,
            remedy="Supply an integer in the signed i64 range",
        )
    return cast(Cell, value)


def decompose(value: T, prepared: PreparedMapping[T]) -> dict[str, list[dict[str, Cell]]]:
    """Project one native record occurrence into its prepared in-memory tables."""
    tables: dict[str, list[dict[str, Cell]]] = {name: [] for name in prepared.tables}
    counter = 0

    def new_key() -> str:
        nonlocal counter
        result = f"r{counter}"
        counter += 1
        return result

    def require_record(instance: object, program: RecordProgram) -> None:
        expected = prepared.native_types.get(program.definition)
        if expected is None or type(instance) is not expected:
            name = expected.__qualname__ if expected is not None else program.definition
            actual = type(instance).__qualname__
            raise _data_error(
                "native_type_mismatch",
                f"Expected record {name}, got {actual}",
                path=program.path,
                remedy="Supply an instance of the declared dataclass",
            )

    def populate(program: RecordProgram, instance: object, row: dict[str, Cell], row_key: str) -> None:
        require_record(instance, program)
        for member, child in program.members:
            member_path = child.path
            native_value = prepared.binding.read(instance, member, path=member_path)
            if isinstance(child, ScalarProgram):
                row[child.column] = _native_scalar(native_value, child)
            elif isinstance(child, RecordProgram):
                if native_value is None:
                    if not child.nullable:
                        raise _data_error(
                            "null_not_allowed",
                            "Required inline record has a null value",
                            path=member_path,
                            remedy="Supply a record instance",
                        )
                    _null_inline(child, row)
                else:
                    populate(child, native_value, row, row_key)
            elif isinstance(child, SequenceProgram):
                if native_value is None:
                    if not child.nullable:
                        raise _data_error(
                            "null_not_allowed",
                            "Required collection has a null value",
                            path=member_path,
                            remedy="Supply a collection",
                        )
                    members = []
                elif isinstance(native_value, list):
                    members = native_value
                else:
                    raise _data_error(
                        "native_shape_mismatch", "Expected a list", path=member_path, remedy="Supply a list"
                    )
                for position, item in enumerate(members):
                    child_key = new_key()
                    child_row: dict[str, Cell] = {
                        prepared.key_column: child_key,
                        prepared.parent_column: row_key,
                        prepared.position_column: position,
                    }
                    if isinstance(child.item, ScalarProgram):
                        child_row[child.item.column] = _native_scalar(item, child.item)
                    elif isinstance(child.item, RecordProgram):
                        if item is None:
                            raise _data_error(
                                "unsupported_null_member",
                                "Nullable record collection members require an explicit null marker not available in this checkpoint",
                                path=child.item.path,
                                remedy="Use non-null record members",
                            )
                        populate(child.item, item, child_row, child_key)
                    else:
                        raise _data_error(
                            "unsupported_program",
                            "Nested collections are outside this executor checkpoint",
                            path=child.path,
                            remedy="Use scalar or record list items",
                        )
                    tables[child.table].append(child_row)
            else:
                raise _data_error(
                    "unsupported_program",
                    "Prepared value program is unsupported",
                    path=member_path,
                    remedy="Prepare a scalar, inline record, or list",
                )

    def _null_inline(program: RecordProgram, row: dict[str, Cell]) -> None:
        for _, child in program.members:
            if isinstance(child, ScalarProgram):
                row[child.column] = None
            elif isinstance(child, RecordProgram):
                _null_inline(child, row)
            elif isinstance(child, SequenceProgram):
                continue

    if not isinstance(prepared.root, RecordProgram):
        raise _data_error(
            "invalid_prepared_mapping",
            "Prepared root is not a record",
            path="",
            remedy="Prepare a dataclass record root",
        )
    root_key = new_key()
    root_row: dict[str, Cell] = {prepared.key_column: root_key}
    populate(prepared.root, value, root_row, root_key)
    tables["root"].append(root_row)
    return tables
