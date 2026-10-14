from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xsdata.models.datatype import XmlDate, XmlDateTime, XmlTime


NS = "urn:structuremap:catalog"


@dataclass(kw_only=True)
class Entry:
    amount: Decimal = field(metadata={
        "type": "Element", "namespace": NS, "total_digits": 20, "fraction_digits": 3,
    })
    serial: int = field(metadata={"type": "Element", "namespace": NS})
    day: XmlDate = field(metadata={"type": "Element", "namespace": NS})
    clock: XmlTime = field(metadata={"type": "Element", "namespace": NS})
    when: XmlDateTime = field(metadata={"type": "Element", "namespace": NS})
    child: list[Entry] = field(default_factory=list, metadata={"type": "Element", "namespace": NS})
    code: str = field(metadata={"type": "Attribute"})


@dataclass(kw_only=True)
class Note:
    content: list[object] = field(default_factory=list, metadata={
        "type": "Wildcard", "namespace": "##any", "mixed": True,
    })


@dataclass(kw_only=True)
class Catalog:
    class Meta:
        name = "catalog"
        namespace = NS

    entry_or_note: list[Entry | Note] = field(default_factory=list, metadata={
        "type": "Elements",
        "choices": (
            {"name": "entry", "type": Entry},
            {"name": "note", "type": Note},
        ),
    })


CATALOG_XML = f'''<catalog xmlns="{NS}" xmlns:q="urn:external">
  <entry code="parent"><amount>9007199254740993.125</amount>
    <serial>1234567890123456789012345678901234567890</serial>
    <day>2024-02-29Z</day><clock>23:59:59+02:00</clock>
    <when>2024-02-03T04:05:06+10:00</when>
    <child code="child"><amount>0.001</amount><serial>-7</serial>
      <day>2024-02-28</day><clock>12:34:56-04:00</clock>
      <when>2024-02-03T04:05:06</when></child>
  </entry>
  <note>lead<q:item q:flag="yes">inside<q:child/>inner-tail</q:item>outer-tail</note>
  <entry code="last"><amount>2.000</amount><serial>42</serial>
    <day>2024-01-01</day><clock>00:00:00Z</clock>
    <when>2024-02-03T04:05:06Z</when></entry>
</catalog>'''

def load_xml_types() -> None:
    # Keep the base-only fixture importable without the optional XML extra.
    from xsdata.models.datatype import XmlDate, XmlDateTime, XmlTime

    globals().update(XmlDate=XmlDate, XmlDateTime=XmlDateTime, XmlTime=XmlTime)
