from __future__ import annotations

import datetime as dt
import re
from typing import NoReturn, cast

from mountainash.typespec.spec import FieldSpec
from mountainash.typespec.universal_types import UniversalType
from xsdata.models.datatype import XmlDate, XmlDateTime, XmlDuration, XmlPeriod, XmlTime

from mountainash_structuremap.codecs import Cell, CellLocation
from mountainash_structuremap.errors import StructureMapError

_DURATION = re.compile(r"^(-)?P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def canonical_duration(value: XmlDuration) -> str:
    """Normalize calendar months and exact decimal seconds independently."""
    match = _DURATION.fullmatch(str(value))
    if match is None:
        raise ValueError("Malformed xs:duration")
    sign, years, months, days, hours, minutes, seconds = match.groups()
    if not any((years, months, days, hours, minutes, seconds)):
        raise ValueError("Duration has no component")
    if "T" in str(value) and not any((hours, minutes, seconds)):
        raise ValueError("Duration has an empty time component")
    whole_seconds, _, fraction = (seconds or "0").partition(".")
    fraction = fraction.rstrip("0")
    total_months = int(years or 0) * 12 + int(months or 0)
    total_seconds = int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(whole_seconds)
    negative = bool(sign) and bool(total_months or total_seconds or fraction)
    canonical_years, canonical_months = divmod(total_months, 12)
    canonical_days, remaining = divmod(total_seconds, 86400)
    canonical_hours, remaining = divmod(remaining, 3600)
    canonical_minutes, canonical_seconds = divmod(remaining, 60)
    parts = ["-P" if negative else "P"]
    for amount, suffix in ((canonical_years, "Y"), (canonical_months, "M"), (canonical_days, "D")):
        if amount:
            parts.append(f"{amount}{suffix}")
    if canonical_hours or canonical_minutes or canonical_seconds or fraction:
        parts.append("T")
        if canonical_hours:
            parts.append(f"{canonical_hours}H")
        if canonical_minutes:
            parts.append(f"{canonical_minutes}M")
        if canonical_seconds or fraction:
            parts.append(f"{canonical_seconds}" + ("." + fraction if fraction else "") + "S")
    return "".join(parts) if len(parts) > 1 else "PT0S"


def _civil_days(year: int, month: int, day: int) -> int:
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    shifted_month = month + (-3 if month > 2 else 9)
    doy = (153 * shifted_month + 2) // 5 + day - 1
    return era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy


def _civil_from_days(days: int) -> tuple[int, int, int]:
    era = days // 146097
    doe = days - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + (3 if mp < 10 else -9)
    year += month <= 2
    return year, month, day


def _year_text(year: int) -> str:
    return ("-" if year < 0 else "") + f"{abs(year):04d}"


def _date_text(year: int, month: int, day: int) -> str:
    return f"{_year_text(year)}-{month:02d}-{day:02d}"


def _fraction_text(fractional_second: int) -> str:
    return "" if not fractional_second else "." + f"{fractional_second:09d}".rstrip("0")


def _offset_text(offset: int | None) -> str:
    if offset is None:
        return ""
    if offset == 0:
        return "Z"
    hours, minutes = divmod(abs(offset), 60)
    return f"{'+' if offset > 0 else '-'}{hours:02d}:{minutes:02d}"


def canonical_temporal(value: object, family: str) -> str:
    """XSD 1.1 retains local fields and the timezoneOffset property."""
    if isinstance(value, XmlDateTime):
        year, month, day = value.year, value.month, value.day
        hour = value.hour
        if hour == 24:
            year, month, day = _civil_from_days(_civil_days(year, month, day) + 1)
            hour = 0
        return (
            f"{_date_text(year, month, day)}T{hour:02d}:{value.minute:02d}:{value.second:02d}"
            f"{_fraction_text(value.fractional_second)}{_offset_text(value.offset)}"
        )
    if isinstance(value, XmlDate):
        return f"{_date_text(value.year, value.month, value.day)}{_offset_text(value.offset)}"
    if isinstance(value, XmlTime):
        hour = 0 if value.hour == 24 else value.hour
        return (
            f"{hour:02d}:{value.minute:02d}:{value.second:02d}"
            f"{_fraction_text(value.fractional_second)}{_offset_text(value.offset)}"
        )
    if isinstance(value, XmlPeriod):
        if value.year is not None:
            text = _year_text(value.year)
            if value.month is not None:
                text += f"-{value.month:02d}"
        elif value.month is not None:
            text = f"--{value.month:02d}"
            if value.day is not None:
                text += f"-{value.day:02d}"
        elif value.day is not None:
            text = f"---{value.day:02d}"
        else:
            raise ValueError("Partial date has no date component")
        return text + _offset_text(value.offset)
    raise TypeError(f"Unsupported temporal family {family}")


def _period_kind(value: XmlPeriod) -> str:
    if value.year is not None:
        return "gYearMonth" if value.month is not None else "gYear"
    if value.month is not None:
        return "gMonthDay" if value.day is not None else "gMonth"
    return "gDay"


class XmlTemporalCodec:
    fields: tuple[FieldSpec, ...]

    def __init__(
        self,
        family: str,
        native_type: type | None,
        column: str,
        path: str,
        policy: str | None,
        metadata_namespace: str,
        format: str | None = None,
    ):
        self.family, self.column, self.path = family, column, path
        self.native_type = {
            "datetime": XmlDateTime,
            "date": XmlDate,
            "time": XmlTime,
            "period": XmlPeriod,
            "duration": XmlDuration,
        }[family]
        if native_type is not None and native_type is not self.native_type:
            self._fail("unsupported_native_scalar", "Unsupported native XML temporal class", path)
        declared_format = (format or "").rsplit("}", 1)[-1].rsplit(":", 1)[-1]
        self.period_format = declared_format if family == "period" and format else None
        if self.period_format and self.period_format not in {"gYear", "gYearMonth", "gMonth", "gMonthDay", "gDay"}:
            self._fail(
                "invalid_scalar_format",
                "Unknown XML partial-date format",
                path,
                remedy="Declare gYear, gYearMonth, gMonth, gMonthDay or gDay",
            )
        if policy == "typed" and family in ("period", "duration"):
            self._fail(
                "unrepresentable_scalar",
                "This calendar datatype requires lexical storage",
                path,
                remedy="Use lexical value_space for partial dates and calendar durations",
            )
        self.typed = policy != "lexical" and family in ("datetime", "date", "time")
        self.offset_column = f"{metadata_namespace}{column}_offset"
        if self.typed:
            physical = {"datetime": UniversalType.DATETIME, "date": UniversalType.DATE, "time": UniversalType.TIME}[
                family
            ]
            primary = FieldSpec(
                name=column,
                type=physical,
                missing_values=[],
                backend_type={"datetime": "timestamp[us]", "time": "time64[us]"}.get(family),
            )
            self.fields = (primary, FieldSpec(name=self.offset_column, type=UniversalType.INTEGER, missing_values=[]))
        else:
            self.fields = (FieldSpec(name=column, type=UniversalType.STRING, missing_values=[]),)

    def _parse(self, text: str) -> XmlDateTime | XmlDate | XmlTime | XmlPeriod | XmlDuration:
        if self.family == "datetime":
            return XmlDateTime.from_string(text)
        if self.family == "date":
            return XmlDate.from_string(text)
        if self.family == "time":
            return XmlTime.from_string(text)
        if self.family == "period":
            return XmlPeriod(text)
        return XmlDuration(text)

    def _canonical(self, value: object) -> str:
        return canonical_duration(value) if isinstance(value, XmlDuration) else canonical_temporal(value, self.family)

    def encode(self, value: object) -> tuple[Cell, ...]:
        if value is None:
            return (None, None) if self.typed else (None,)
        if type(value) is not self.native_type:
            self._fail("invalid_scalar", f"Expected native XML {self.family}", self.path)
        offset = getattr(value, "offset", None)
        if offset is not None and (type(offset) is not int or not -840 <= offset <= 840):
            self._fail(
                "invalid_temporal_offset", "XML timezone offset must be integer minutes within ±14 hours", self.path
            )
        if isinstance(value, (XmlDateTime, XmlTime)):
            if not 0 <= value.fractional_second < 1_000_000_000:
                self._fail("invalid_scalar", "Invalid native fractional-second component", self.path)
            if value.hour == 24 and (value.minute or value.second or value.fractional_second):
                self._fail("invalid_scalar", "Hour 24 requires exact midnight", self.path)
        self._check_period(value)
        if not self.typed:
            try:
                text = self._canonical(value)
                self._parse(text)
            except (ValueError, TypeError) as exc:
                raise StructureMapError(
                    "invalid_scalar",
                    "Malformed native XML temporal value",
                    path=self.path,
                    remedy="Supply a valid value of the declared XML temporal datatype",
                ) from exc
            return (text,)
        try:
            if isinstance(value, XmlDateTime):
                self._check_typed_clock(value)
                wall = dt.datetime(
                    value.year, value.month, value.day, value.hour, value.minute, value.second, value.microsecond
                )
                if value.offset is not None:
                    wall -= dt.timedelta(minutes=value.offset)
                return wall, value.offset
            if isinstance(value, XmlDate):
                return dt.date(value.year, value.month, value.day), value.offset
            clock = cast(XmlTime, value)
            self._check_typed_clock(clock)
            return dt.time(clock.hour, clock.minute, clock.second, clock.microsecond), clock.offset
        except (ValueError, OverflowError) as exc:
            raise StructureMapError(
                "unrepresentable_temporal",
                "XML temporal is outside the typed carrier range",
                path=self.path,
                remedy="Use lexical value_space",
            ) from exc

    def _check_typed_clock(self, value: XmlDateTime | XmlTime) -> None:
        if value.hour == 24 or value.fractional_second % 1000:
            self._fail(
                "unrepresentable_temporal",
                "Typed clocks require hour 0–23 and microsecond precision",
                self.path,
                remedy="Use lexical value_space",
            )

    def check(self, cells: tuple[Cell, ...], location: CellLocation) -> object:
        value = cells[0]
        if not self.typed:
            if value is None:
                return None
            if not isinstance(value, str):
                self._fail("cell_type_mismatch", "Expected canonical XML temporal text", location.path, location)
            try:
                parsed = self._parse(value)
                parsed_offset = getattr(parsed, "offset", None)
                if parsed_offset is not None and not -840 <= parsed_offset <= 840:
                    self._fail(
                        "invalid_temporal_offset",
                        "XML timezone offset must be within ±14 hours",
                        location.path,
                        location,
                    )
                canonical = self._canonical(parsed)
            except (ValueError, TypeError) as exc:
                raise StructureMapError(
                    "invalid_scalar",
                    "Malformed XML temporal text",
                    path=location.path,
                    table=location.table,
                    row=location.row,
                    column=location.column,
                    remedy="Supply canonical text for the declared XML temporal datatype",
                ) from exc
            self._check_period(parsed, location)
            if canonical != value:
                self._fail("noncanonical_scalar", "XML temporal text must be canonical", location.path, location)
            return parsed
        offset = cells[1]
        offset_location = CellLocation(location.path, table=location.table, row=location.row, column=self.offset_column)
        if offset is not None and (type(offset) is not int or not -840 <= offset <= 840):
            self._fail(
                "invalid_temporal_offset",
                "Offset must be null or integer minutes within ±14 hours",
                location.path,
                offset_location,
            )
        if value is None:
            if offset is not None:
                self._fail(
                    "invalid_temporal_offset", "Null temporal has non-null offset", location.path, offset_location
                )
            return None
        expected = {"datetime": dt.datetime, "date": dt.date, "time": dt.time}[self.family]
        if type(value) is not expected:
            self._fail("cell_type_mismatch", f"Expected {expected.__name__} cell", location.path, location)
        if isinstance(value, (dt.datetime, dt.time)) and value.tzinfo is not None:
            self._fail(
                "invalid_temporal_offset", "Typed temporal carriers must be timezone-naive", location.path, location
            )
        if isinstance(value, dt.datetime) and offset is not None:
            try:
                value += dt.timedelta(minutes=offset)
            except OverflowError:
                self._fail(
                    "unrepresentable_temporal",
                    "UTC timestamp and offset exceed the local datetime range",
                    location.path,
                    location,
                    remedy="Supply a representable pair or use lexical value_space",
                )
        return value, offset

    def _check_period(self, value: object, location: CellLocation | None = None) -> None:
        if isinstance(value, XmlPeriod) and self.period_format and _period_kind(value) != self.period_format:
            self._fail(
                "invalid_scalar",
                f"Expected declared XML partial-date type {self.period_format}",
                location.path if location else self.path,
                location,
            )

    def decode(self, checked: object) -> object:
        if not self.typed or checked is None:
            return checked
        value, offset = cast(tuple[dt.datetime | dt.date | dt.time, int | None], checked)
        if isinstance(value, dt.datetime):
            return XmlDateTime(
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond * 1000,
                offset,
            )
        if isinstance(value, dt.date):
            return XmlDate(value.year, value.month, value.day, offset)
        return XmlTime(value.hour, value.minute, value.second, value.microsecond * 1000, offset)

    @staticmethod
    def _fail(
        code: str,
        message: str,
        path: str,
        location: CellLocation | None = None,
        remedy: str = "Supply a valid XML temporal value",
    ) -> NoReturn:
        raise StructureMapError(
            code,
            message,
            path=path,
            table=location.table if location else None,
            row=location.row if location else None,
            column=location.column if location else None,
            owner="executor restriction",
            remedy=remedy,
        )
