import datetime as dt
from decimal import Decimal, localcontext

import pyarrow as pa
import pytest
from mountainash import DecimalDtype
from mountainash.typespec.converters import to_arrow_schema
from mountainash.typespec.spec import TypeSpec
from xsdata.models.datatype import XmlBase64Binary, XmlDate, XmlDateTime, XmlDuration, XmlHexBinary, XmlPeriod, XmlTime

from mountainash_structuremap import (
    MappingSpec,
    StructureMapError,
    decompose,
    derive,
    parse,
    prepare,
    reconstruct,
    serialize,
)
from mountainash_structuremap.codecs import CellLocation
from tests.fixtures.xml_scalar_models import (
    Calendar,
    ConstructedScalarSibling,
    DecimalState,
    EnumScalar,
    FacetedAmount,
    IntegerValue,
    LaterScalarValidation,
    NativeAtomicValues,
    NumericBoundaries,
    PhysicalDocument,
    PhysicalValues,
    PrecisionBoundary,
    TemporalValues,
    Timed,
    UnboundedAmount,
)


@pytest.mark.parametrize(
    "native,lexical",
    [
        (Decimal("2.00"), "2"),
        (Decimal("-0.000"), "0"),
        (Decimal("9007199254740993.125"), "9007199254740993.125"),
    ],
)
def test_exact_lexical_decimal_is_context_independent(native, lexical):
    prepared = prepare(derive(UnboundedAmount))
    with localcontext() as context:
        context.prec = 3
        rows = decompose(UnboundedAmount(native), prepared)
        assert rows["root"][0]["amount"] == lexical
        rebuilt = reconstruct({"root": [{"_sm_key": "external", "amount": lexical}]}, prepare(derive(UnboundedAmount)))
    assert rebuilt.amount == native


def test_decimal_facets_cover_whole_source_domain_without_rounding():
    prepared = prepare(derive(FacetedAmount))
    assert prepared.tables["root"].typespec.get_field("amount").dtype == DecimalDtype(5, 2)
    result = reconstruct({"root": [{"_sm_key": "external", "amount": Decimal("999")}]}, prepared)
    assert result.amount == Decimal("999")
    with pytest.raises(StructureMapError):
        reconstruct({"root": [{"_sm_key": "external", "amount": Decimal("999.99")}]}, prepared)


@pytest.mark.parametrize(
    "text",
    [
        "2024-02-03T04:05:06",
        "2024-02-03T04:05:06Z",
        "2024-02-03T04:05:06.123456+10:00",
        "2024-02-03T04:05:06-05:30",
    ],
)
def test_temporal_offset_presence_survives(text):
    source = Timed(XmlDateTime.from_string(text))
    rows = decompose(source, prepare(derive(Timed)))
    result = reconstruct(rows, prepare(derive(Timed)))
    assert result.when == source.when
    assert result.when.offset == source.when.offset


def test_submicrosecond_temporal_requires_explicit_lexical_policy():
    native = Timed(XmlDateTime.from_string("2024-02-03T04:05:06.123456789+10:00"))
    with pytest.raises(StructureMapError) as caught:
        decompose(native, prepare(derive(Timed)))
    assert "lexical" in caught.value.remedy.lower()
    mapping = MappingSpec(overrides={"when": {"value_space": "lexical"}})
    rows = decompose(native, prepare(derive(Timed), mapping))
    assert rows["root"][0]["when"] == "2024-02-03T04:05:06.123456789+10:00"
    result = reconstruct(rows, prepare(derive(Timed), mapping))
    assert result.when == native.when
    assert result.when.offset == native.when.offset
    assert (result.when.year, result.when.month, result.when.day, result.when.hour) == (
        native.when.year,
        native.when.month,
        native.when.day,
        native.when.hour,
    )


@pytest.mark.parametrize("text", ["+01", "01", "-0", "1e2"])
def test_noncanonical_independent_integer_text_is_rejected(text):
    with pytest.raises(StructureMapError) as caught:
        reconstruct({"root": [{"_sm_key": "external", "value": text}]}, prepare(derive(IntegerValue)))
    assert caught.value.column == "value"


def test_unbounded_integer_exceeds_python_default_digit_conversion_limit():
    value = 10**5000 + 123
    prepared = prepare(derive(IntegerValue))
    rows = decompose(IntegerValue(value), prepared)
    lexical = rows["root"][0]["value"]
    assert lexical == "1" + "0" * 4997 + "123"
    assert reconstruct(rows, prepared).value == value


def test_unbounded_integer_has_no_implicit_i64_narrowing():
    native = IntegerValue(10**99 + 123)
    prepared = prepare(derive(IntegerValue))
    rows = decompose(native, prepared)
    assert rows["root"][0]["value"] == "1" + "0" * 96 + "123"
    assert reconstruct(rows, prepared).value == 10**99 + 123


def test_decimal_facets_enforce_bounds():
    prepared = prepare(derive(NumericBoundaries))
    for value in (Decimal("-9.99"), Decimal("9.99")):
        assert reconstruct({"root": [{"_sm_key": "external", "amount": value}]}, prepared).amount == value
    for value in (Decimal("-10"), Decimal("10")):
        with pytest.raises(StructureMapError):
            reconstruct({"root": [{"_sm_key": "external", "amount": value}]}, prepared)


@pytest.mark.parametrize("text", ["2024-02+05:30", "--02-29Z", "---31-05:00", "2024Z", "--02Z", "--02--"])
def test_xml_period_lexical_roundtrip(text):
    source = TemporalValues(
        XmlDate.from_string("2024-02-29Z"),
        XmlTime.from_string("23:59:59+02:00"),
        XmlPeriod(text),
        XmlDuration("P1Y2M3DT4H"),
    )
    prepared = prepare(derive(TemporalValues))
    assert reconstruct(decompose(source, prepared), prepared) == source


@pytest.mark.parametrize(
    "duration,canonical",
    [
        ("P1Y12M", "P2Y"),
        ("PT90M", "PT1H30M"),
        ("P0D", "PT0S"),
        ("-P1M2DT3S", "-P1M2DT3S"),
        ("PT0.1250S", "PT0.125S"),
        ("PT10S", "PT10S"),
        ("PT90S", "PT1M30S"),
        ("PT20.00S", "PT20S"),
    ],
)
def test_duration_months_and_seconds_canonical_inverse(duration, canonical):
    source = Calendar(
        XmlDate.from_string("2024-01-01"),
        XmlTime.from_string("00:00:00"),
        XmlPeriod("2024"),
        XmlDuration(duration),
    )
    prepared = prepare(derive(Calendar))
    rows = decompose(source, prepared)
    assert rows["root"][0]["duration"] == canonical
    rows["root"][0]["duration"] = canonical
    assert str(reconstruct(rows, prepare(derive(Calendar))).duration) == canonical


def test_typed_temporal_date_and_time_keep_local_fields_and_offset_companions():
    source = Calendar(
        XmlDate.from_string("2024-02-29+05:30"),
        XmlTime.from_string("12:34:56.123000-04:00"),
        XmlPeriod("--02-29"),
        XmlDuration("P1M"),
    )
    mapping = MappingSpec(defaults={"naming": {"metadata_namespace": "meta_"}})
    prepared = prepare(derive(Calendar), mapping)
    rows = decompose(source, prepared)
    assert rows["root"][0]["meta_date_offset"] == 330
    assert rows["root"][0]["meta_time_offset"] == -240
    assert reconstruct(rows, prepared) == source


def test_typed_temporal_null_rejects_nonnull_offset_companion():
    prepared = prepare(derive(Timed))
    with pytest.raises(StructureMapError):
        reconstruct({"root": [{"_sm_key": "external", "when": None, "_sm_when_offset": 60}]}, prepared)


def test_decimal_lexical_rejects_noncanonical_and_malformed_table_text():
    prepared = prepare(derive(UnboundedAmount))
    for text in ("1.00", "1e2", "bad", "-0"):
        with pytest.raises(StructureMapError):
            reconstruct({"root": [{"_sm_key": "external", "amount": text}]}, prepared)


def test_bounded_integer_binary_formats_and_expanded_qname_roundtrip():
    from xml.etree.ElementTree import QName

    source = NativeAtomicValues(100, b"\x00\xff", b"\x00\xff", QName("{urn:example}item"))
    prepared = prepare(derive(NativeAtomicValues))
    rows = decompose(source, prepared)
    assert rows["root"][0]["bounded"] == 100
    assert rows["root"][0]["hex_binary"] == "00FF"
    assert rows["root"][0]["base64_binary"] == "AP8="
    assert rows["root"][0]["qname"] == "{urn:example}item"
    authored = {
        "root": [
            {
                "_sm_key": "external",
                "bounded": 100,
                "hex_binary": "00FF",
                "base64_binary": "AP8=",
                "qname": "{urn:example}item",
            }
        ]
    }
    assert reconstruct(authored, prepared) == source


def test_xml_decimal_enum_uses_canonical_scalar_codec():
    prepared = prepare(derive(EnumScalar))
    rows = decompose(EnumScalar(DecimalState.LARGE), prepared)
    assert rows["root"][0]["value"] == "9007199254740993.125"
    assert (
        reconstruct({"root": [{"_sm_key": "external", "value": "9007199254740993.125"}]}, prepared).value
        is DecimalState.LARGE
    )


def test_typed_datetime_utc_underflow_requires_lexical_policy():
    source = Timed(XmlDateTime.from_string("0001-01-01T00:00:00+14:00"))
    with pytest.raises(StructureMapError):
        decompose(source, prepare(derive(Timed)))
    mapping = MappingSpec(overrides={"when": {"value_space": "lexical"}})
    assert (
        reconstruct(decompose(source, prepare(derive(Timed), mapping)), prepare(derive(Timed), mapping)).when
        == source.when
    )


def test_explicit_typed_decimal_without_facets_is_rejected():
    mapping = MappingSpec(overrides={"amount": {"value_space": "typed"}})
    with pytest.raises(StructureMapError):
        prepare(derive(UnboundedAmount), mapping)


def test_decimal_carrier_precision_38_boundary():
    prepared = prepare(derive(PrecisionBoundary))
    spec = prepared.tables["root"].typespec
    assert spec.get_field("wide").dtype == DecimalDtype(38, 0)
    assert spec.get_field("wider").type.name == "STRING"
    assert spec.get_field("bounded").dtype == DecimalDtype(2, 1)
    largest = Decimal("9" * 38)
    source = PrecisionBoundary(largest, largest, Decimal("9"))
    rows = decompose(source, prepared)
    arrow = pa.Table.from_pylist(rows["root"], schema=to_arrow_schema(spec))
    assert reconstruct({"root": arrow.to_pylist()}, prepared) == source
    with pytest.raises(StructureMapError):
        prepare(derive(PrecisionBoundary), MappingSpec(overrides={"wider": {"value_space": "typed"}}))
    invalid = {"root": [{**rows["root"][0], "wide": Decimal("1" + "0" * 38)}]}
    with pytest.raises(StructureMapError):
        reconstruct(invalid, prepared)


def test_decimal_value_space_rejects_nonfinite_native_values():
    prepared = prepare(derive(UnboundedAmount))
    for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(StructureMapError):
            decompose(UnboundedAmount(value), prepared)


def test_lexical_datetime_canonicalizes_zero_offset_and_24_hour():
    mapping = MappingSpec(overrides={"when": {"value_space": "lexical"}})
    prepared = prepare(derive(Timed), mapping)
    source = Timed(XmlDateTime.from_string("2024-02-03T24:00:00Z"))
    rows = decompose(source, prepared)
    assert rows["root"][0]["when"] == "2024-02-04T00:00:00Z"
    assert reconstruct(rows, prepared).when == source.when


def test_lexical_date_and_time_retain_local_fields_and_offsets():
    source = Calendar(
        XmlDate.from_string("2024-02-29+05:30"),
        XmlTime.from_string("12:34:56.123000-04:00"),
        XmlPeriod("--02-29"),
        XmlDuration("P1M"),
    )
    mapping = MappingSpec(
        overrides={
            "date": {"value_space": "lexical"},
            "time": {"value_space": "lexical"},
        }
    )
    prepared = prepare(derive(Calendar), mapping)
    rows = decompose(source, prepared)
    assert rows["root"][0]["date"] == "2024-02-29+05:30"
    assert rows["root"][0]["time"] == "12:34:56.123-04:00"
    rebuilt = reconstruct(rows, prepared)
    assert rebuilt.date.offset == source.date.offset
    assert rebuilt.date.year == source.date.year
    assert rebuilt.time.offset == source.time.offset
    assert rebuilt.time.hour == source.time.hour


def test_declared_xml_period_format_restricts_partial_date_subtype():
    from mountainash_structuremap.bindings.xml_scalars import prepare_xml_scalar
    from mountainash_structuremap.definitions import Scalar

    codec = prepare_xml_scalar(Scalar(type="period", format="xs:gYear"), XmlPeriod, "period", "period")
    assert codec.encode(XmlPeriod("2024")) == ("2024",)
    with pytest.raises(StructureMapError):
        codec.encode(XmlPeriod("--02"))


def test_enum_numeric_facets_are_forwarded_to_value_codec():
    from mountainash_structuremap.bindings.xml_scalars import prepare_xml_scalar
    from mountainash_structuremap.definitions import NumericFacets, Scalar

    codec = prepare_xml_scalar(
        Scalar(
            type="enum",
            facets=NumericFacets(
                total_digits=20,
                fraction_digits=3,
                max_inclusive="100",
            ),
        ),
        DecimalState,
        "value",
        "value",
        policy="lexical",
    )
    assert codec.encode(DecimalState.SMALL) == ("1.25",)
    with pytest.raises(StructureMapError):
        codec.encode(DecimalState.LARGE)


def test_native_binary_wrapper_values_follow_declared_byte_encoding():
    from xml.etree.ElementTree import QName

    prepared = prepare(derive(NativeAtomicValues))
    source = NativeAtomicValues(1, XmlHexBinary(b"\x00\xff"), XmlBase64Binary(b"\x00\xff"), QName("{urn:q}name"))
    rows = decompose(source, prepared)
    assert rows["root"][0]["hex_binary"] == "00FF"
    assert rows["root"][0]["base64_binary"] == "AP8="
    result = reconstruct(rows, prepared)
    assert result == source
    assert parse(serialize(result, prepared), prepared) == source


@pytest.mark.parametrize("offset", ["0", True, 841, -841])
def test_typed_temporal_rejects_invalid_physical_offset_cells(offset):
    prepared = prepare(derive(Timed))
    with pytest.raises(StructureMapError):
        reconstruct(
            {
                "root": [
                    {
                        "_sm_key": "external",
                        "when": dt.datetime(2024, 1, 2),
                        "_sm_when_offset": offset,
                    }
                ]
            },
            prepared,
        )


def test_typed_temporal_rejects_aware_physical_cells():
    prepared = prepare(derive(Timed))
    with pytest.raises(StructureMapError):
        reconstruct(
            {
                "root": [
                    {
                        "_sm_key": "external",
                        "when": dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc),
                        "_sm_when_offset": None,
                    }
                ]
            },
            prepared,
        )


def test_later_bad_scalar_is_rejected_before_any_object_construction():
    prepared = prepare(derive(LaterScalarValidation))
    LaterScalarValidation.constructions = 0
    ConstructedScalarSibling.constructions = 0
    with pytest.raises(StructureMapError):
        reconstruct(
            {
                "root": [
                    {
                        "_sm_key": "external",
                        "first_value": "valid sibling",
                        "later": "not-an-integer",
                    }
                ]
            },
            prepared,
        )
    assert LaterScalarValidation.constructions == 0
    assert ConstructedScalarSibling.constructions == 0


def test_nullable_temporal_all_null_roundtrip():
    prepared = prepare(derive(Timed))
    rows = decompose(Timed(None), prepared)
    assert rows["root"][0]["when"] is None
    assert rows["root"][0]["_sm_when_offset"] is None
    assert reconstruct(rows, prepared).when is None


def test_lexical_integer_storage_still_enforces_numeric_facets():
    from mountainash_structuremap.bindings.xml_scalars import prepare_xml_scalar
    from mountainash_structuremap.definitions import NumericFacets, Scalar

    codec = prepare_xml_scalar(
        Scalar(type="int", facets=NumericFacets(min_inclusive="-100", max_exclusive="100")),
        int,
        "value",
        "value",
        policy="lexical",
    )
    assert codec.encode(-100) == ("-100",)
    with pytest.raises(StructureMapError):
        codec.encode(100)
    with pytest.raises(StructureMapError):
        codec.check(("100",), CellLocation("value"))


@pytest.mark.parametrize("include_values", [False, True])
def test_prepared_numeric_temporal_rows_survive_mountainash_persistence_and_arrow(include_values):
    model = derive(PhysicalDocument)
    prepared = prepare(model)
    values = (
        [
            PhysicalValues(
                Decimal("9007199254740993.125"),
                XmlDateTime.from_string("2024-02-03T04:05:06.123456+10:00"),
                XmlTime.from_string("12:34:56.654321-05:30"),
            ),
            PhysicalValues(Decimal("999"), XmlDateTime.from_string("2024-02-03T04:05:06"), None),
            PhysicalValues(None, XmlDateTime.from_string("2024-02-03T04:05:06Z"), None),
            PhysicalValues(None, None, None),
        ]
        if include_values
        else []
    )
    source = PhysicalDocument(values)
    emitted = decompose(source, prepared)
    delivered = {}
    for name, table in prepared.tables.items():
        persisted = TypeSpec.from_frictionless(table.typespec.to_frictionless())
        schema = to_arrow_schema(persisted)
        if name == "values":
            assert persisted.get_field("amount").dtype == DecimalDtype(23, 3)
            assert schema.field("stamp").type == pa.timestamp("us")
            assert schema.field("clock").type == pa.time64("us")
        delivered[name] = pa.Table.from_pylist(emitted[name], schema=schema).to_pylist()
    rebuilt = reconstruct(delivered, prepare(model))
    assert rebuilt == source
    assert [None if value.stamp is None else tuple(value.stamp) for value in rebuilt.values] == [
        None if value.stamp is None else tuple(value.stamp) for value in source.values
    ]
    assert [None if value.clock is None else tuple(value.clock) for value in rebuilt.values] == [
        None if value.clock is None else tuple(value.clock) for value in source.values
    ]


@pytest.mark.parametrize(
    "value,offset",
    [
        (dt.datetime(9999, 12, 31, 23, 59), 840),
        (dt.datetime(1, 1, 1), -840),
    ],
)
def test_temporal_inverse_range_is_checked_before_earlier_sibling_construction(value, offset):
    from tests.fixtures.xml_scalar_models import LaterTemporalValidation

    prepared = prepare(derive(LaterTemporalValidation))
    ConstructedScalarSibling.constructions = 0
    with pytest.raises(StructureMapError) as caught:
        reconstruct(
            {
                "root": [
                    {
                        "_sm_key": "external",
                        "first_value": "valid",
                        "later": value,
                        "_sm_later_offset": offset,
                    }
                ]
            },
            prepared,
        )
    assert caught.value.column == "later"
    assert ConstructedScalarSibling.constructions == 0


@pytest.mark.parametrize(
    "text",
    [
        "2024-01-01T00:00:00+15:00",
        "2024-01-01T00:00:00-14:01",
    ],
)
def test_lexical_temporal_rejects_invalid_timezone_range(text):
    mapping = MappingSpec(overrides={"when": {"value_space": "lexical"}})
    with pytest.raises(StructureMapError) as caught:
        reconstruct({"root": [{"_sm_key": "external", "when": text}]}, prepare(derive(Timed), mapping))
    assert caught.value.column == "when"


def test_hour_24_requires_lexical_and_normalizes_valid_midnight():
    source = Timed(XmlDateTime.from_string("2024-02-29T24:00:00Z"))
    with pytest.raises(StructureMapError) as caught:
        decompose(source, prepare(derive(Timed)))
    assert "lexical" in caught.value.remedy.lower()
    mapping = MappingSpec(overrides={"when": {"value_space": "lexical"}})
    rows = decompose(source, prepare(derive(Timed), mapping))
    assert rows["root"][0]["when"] == "2024-03-01T00:00:00Z"
    assert tuple(reconstruct(rows, prepare(derive(Timed), mapping)).when) == (2024, 3, 1, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "format,text,canonical",
    [
        ("xs:gYear", "2024-05:00", "2024-05:00"),
        ("xs:gMonth", "--02-05:00", "--02-05:00"),
        ("xs:gYear", "2024+00:00", "2024Z"),
        ("xs:gMonthDay", "--02-29-00:00", "--02-29Z"),
    ],
)
def test_partial_date_subtype_and_canonical_offset(format, text, canonical):
    from mountainash_structuremap.bindings.xml_scalars import prepare_xml_scalar
    from mountainash_structuremap.definitions import Scalar

    codec = prepare_xml_scalar(Scalar(type="period", format=format), XmlPeriod, "value", "value")
    assert codec.encode(XmlPeriod(text)) == (canonical,)
    assert codec.decode(codec.check((canonical,), CellLocation("value"))) == XmlPeriod(text)
    if text != canonical:
        with pytest.raises(StructureMapError):
            codec.check((text,), CellLocation("value"))


@pytest.mark.parametrize("qname", ["{urn:example}1item", "{urn:example}a/b", "prefix:item", " item "])
def test_independent_qname_cells_require_native_valid_expanded_names(qname):
    with pytest.raises(StructureMapError) as caught:
        reconstruct(
            {
                "root": [
                    {
                        "_sm_key": "external",
                        "bounded": 1,
                        "hex_binary": "00FF",
                        "base64_binary": "AP8=",
                        "qname": qname,
                    }
                ]
            },
            prepare(derive(NativeAtomicValues)),
        )
    assert caught.value.column == "qname"


def test_integer_enum_policies_use_the_finite_native_domain():
    from tests.fixtures.xml_scalar_models import Count, CountValue, HugeCountValue

    lexical = MappingSpec(overrides={"value": {"value_space": "lexical"}})
    prepared = prepare(derive(CountValue), lexical)
    assert decompose(CountValue(Count.ONE), prepared)["root"][0]["value"] == "1"
    assert reconstruct({"root": [{"_sm_key": "external", "value": "1"}]}, prepared).value is Count.ONE
    typed = MappingSpec(overrides={"value": {"value_space": "typed"}})
    assert (
        reconstruct({"root": [{"_sm_key": "external", "value": 1}]}, prepare(derive(CountValue), typed)).value
        is Count.ONE
    )
    with pytest.raises(StructureMapError) as caught:
        prepare(derive(HugeCountValue), typed)
    assert "lexical" in caught.value.remedy.lower()


@pytest.mark.parametrize(
    "flag,number,text",
    [
        (True, 1e-7, "1.0E-7"),
        (False, -0.0, "-0.0E0"),
        (True, float("inf"), "INF"),
        (False, float("-inf"), "-INF"),
        (True, float("nan"), "NaN"),
    ],
)
def test_explicit_lexical_boolean_and_float_keep_native_values(flag, number, text):
    from math import copysign, isnan

    from tests.fixtures.xml_scalar_models import BooleanFloat

    mapping = MappingSpec(defaults={"value_space": "lexical"})
    prepared = prepare(derive(BooleanFloat), mapping)
    source = BooleanFloat(flag, number)
    rows = decompose(source, prepared)
    assert rows["root"][0]["flag"] == ("true" if flag else "false")
    assert rows["root"][0]["number"] == text
    result = reconstruct(
        {
            "root": [
                {
                    "_sm_key": "external",
                    "flag": "true" if flag else "false",
                    "number": text,
                }
            ]
        },
        prepare(derive(BooleanFloat), mapping),
    )
    assert result.flag is flag
    if isnan(number):
        assert isnan(result.number)
    else:
        assert result.number == number
        assert copysign(1, result.number) == copysign(1, number)


@pytest.mark.parametrize("field,text", [("flag", "1"), ("number", "1e0"), ("number", "inf")])
def test_lexical_primitive_cells_must_be_canonical(field, text):
    from tests.fixtures.xml_scalar_models import BooleanFloat

    row = {"_sm_key": "external", "flag": "true", "number": "1.0E0", field: text}
    with pytest.raises(StructureMapError) as caught:
        reconstruct({"root": [row]}, prepare(derive(BooleanFloat), MappingSpec(defaults={"value_space": "lexical"})))
    assert caught.value.column == field


@pytest.mark.parametrize("value", [Decimal("1E1000000000"), Decimal("1E-1000000000")])
def test_typed_decimal_rejects_outside_facets_without_expanding_exponents(value):
    with pytest.raises(StructureMapError) as caught:
        decompose(FacetedAmount(value), prepare(derive(FacetedAmount)))
    assert caught.value.code == "facet_violation"


@pytest.mark.parametrize("bound", ["NaN", "Infinity", "not-a-number"])
def test_invalid_decimal_bound_is_an_attributed_preparation_error(bound):
    from mountainash_structuremap.bindings.xml_scalars import prepare_xml_scalar
    from mountainash_structuremap.definitions import NumericFacets, Scalar

    with pytest.raises(StructureMapError) as caught:
        prepare_xml_scalar(
            Scalar(type="decimal", facets=NumericFacets(min_inclusive=bound)), Decimal, "amount", "amount"
        )
    assert caught.value.code == "invalid_numeric_facets"
    assert caught.value.path == "amount"
