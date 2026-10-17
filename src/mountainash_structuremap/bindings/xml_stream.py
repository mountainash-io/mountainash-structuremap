from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any, BinaryIO, Never, cast

from lxml import etree
from xsdata.exceptions import ParserError
from xsdata.formats.dataclass.context import XmlContext
from xsdata.formats.dataclass.models.elements import XmlMeta
from xsdata.formats.dataclass.parsers.bases import NodeParser
from xsdata.formats.dataclass.parsers.config import ParserConfig
from xsdata.formats.dataclass.parsers.nodes import ElementNode, WrapperNode
from xsdata.formats.dataclass.parsers.utils import PendingCollection

from mountainash_structuremap.bindings.xsdata import XsdataBinding, XsdataRecordAdapter
from mountainash_structuremap.errors import StructureMapError
from mountainash_structuremap.execution_session import DecompositionSession, ScopeHandle
from mountainash_structuremap.prepared import (
    LinkProgram,
    PreparedMapping,
    RecordProgram,
    ScalarProgram,
    SequenceProgram,
    VariantProgram,
)


class _RejectExternalResources(etree.Resolver):
    def resolve(self, system_url: object, public_id: object, context: object = None) -> Never:
        raise ParserError(f"External XML resources are not supported: {system_url!r}; supply self-contained XML")


def _unsupported(path: str, message: str) -> StructureMapError:
    return StructureMapError(
        "unavailable_xml_stream",
        message,
        path=path,
        owner="executor restriction",
        remedy="Select a compatible record boundary or use in-memory decomposition",
    )


@dataclass(frozen=True)
class _Envelope:
    use: int
    record: RecordProgram
    ancestry: tuple[str, ...]
    meta: XmlMeta
    member: str
    edge: int
    targets: tuple[tuple[str, ...], ...]
    table: str
    role: str | None
    dependencies: tuple[str, ...] = ()


@dataclass
class _LiveEnvelope:
    plan: _Envelope
    node: ElementNode
    scope: ScopeHandle
    params: dict[str, Any] = field(default_factory=dict)
    sent: set[str] = field(default_factory=set)
    position: int = 0


@dataclass(frozen=True)
class XmlStreamPlan:
    prepared: PreparedMapping[Any]
    envelopes: tuple[_Envelope, ...]
    context: XmlContext

    def run(self, source: BinaryIO, session: DecompositionSession) -> None:
        """Push native XML nodes, diverting only live envelope contributions.

        xsdata owns conversion and complete-record construction. Envelope nodes
        never construct a partial native object. Their completed native members
        enter the same decomposition session used for whole documents.
        """
        parser = NodeParser(
            context=self.context,
            config=ParserConfig(
                fail_on_converter_warnings=True,
                fail_on_unknown_properties=True,
            ),
        )
        queue: list[Any] = []
        objects: list[Any] = []
        ancestry: list[str] = []
        live: list[_LiveEnvelope] = []
        plans = {item.ancestry: item for item in self.envelopes}
        boundaries = set(plans)
        boundaries.update(path for plan in self.envelopes for path in plan.targets)

        def native_events(
            events: Iterable[tuple[str, Any]],
        ) -> Iterator[tuple[str, Any]]:
            # lxml may report an end tag before a later read supplies its tail.
            # Hold one interior end event until the following structural event,
            # which makes that tail complete. A selected record's end remains
            # immediate: its interior is complete and its outside tail is not
            # part of that native record. No document-end lookahead is needed.
            pending: tuple[str, Any] | None = None
            for event, element in events:
                if pending is not None:
                    yield pending
                    pending = None
                if event == "end" and tuple(ancestry) not in boundaries:
                    pending = (event, element)
                else:
                    yield event, element
            if pending is not None:
                yield pending

        def send(envelope: _LiveEnvelope, *, closing: bool = False) -> None:
            variables = {var.name: var for var in envelope.node.meta.get_all_vars()}
            for name in tuple(envelope.params):
                value = envelope.params[name]
                if name in envelope.sent:
                    continue
                var = variables[name]
                # Nonattribute header context remains native until scope close.
                # Its child rows may need a key supplied by a later inline scope.
                if not closing and not (var.is_attribute or var.is_attributes):
                    continue
                if isinstance(value, PendingCollection):
                    value = value.evaluate()
                session.member(envelope.scope, name, value)
                envelope.sent.add(name)
                del envelope.params[name]

        def release(element: Any) -> None:
            element.clear()
            # Converted native values and namespace maps no longer need the
            # source XML siblings retained by their lxml parent.
            parent_element = element.getparent()
            if parent_element is not None:
                while element.getprevious() is not None:
                    del parent_element[0]

        try:
            events = etree.iterparse(
                source,
                events=("start", "end"),
                recover=False,
                remove_comments=True,
                remove_pis=True,
                load_dtd=False,
                resolve_entities=True,
                no_network=True,
            )
            # Match the ElementTree-backed native binding: internal entities
            # contribute text, but external files and URLs are never resolved.
            events.resolvers.add(_RejectExternalResources())
            for event, element in native_events(events):
                if event == "start":
                    parser.start(
                        self.prepared.root_type, queue, objects, element.tag, dict(element.attrib), element.nsmap
                    )
                    # Native wrappers proxy their parent's field dispatch; bare
                    # and wrapped contributions belong to the same native field.
                    # Keep the parser event, but not a fictitious record ancestor.
                    if isinstance(queue[-1], WrapperNode):
                        continue
                    # An explicitly supplied xsdata root class does not require
                    # the document element to have its Meta name. Only descendant
                    # field QNames participate in structural path selection.
                    ancestry.append(element.tag if ancestry else self.envelopes[0].ancestry[0])
                    if live and tuple(ancestry) in live[-1].plan.targets:
                        envelope = live[-1]
                        program = envelope.plan.record
                        adapter = cast(XsdataRecordAdapter, self.prepared.record_adapters[program.definition])
                        for name in envelope.plan.dependencies:
                            if name in envelope.sent:
                                continue
                            dependency = self.prepared.programs[dict(program.members)[name]]
                            supplied, value = adapter.omitted(name, path=dependency.path)
                            if not supplied:
                                raise _unsupported(
                                    dependency.path,
                                    "Ancestor key dependency is absent before the record boundary",
                                )
                            session.member(envelope.scope, name, value)
                            envelope.sent.add(name)
                    plan = plans.get(tuple(ancestry))
                    if plan is not None:
                        node = queue[-1]
                        if not isinstance(node, ElementNode):
                            raise _unsupported(
                                plan.record.path,
                                "The selected ancestor is not a native record node",
                            )
                        parent = live[-1] if live else None
                        if parent is None and node.xsi_nil and not node.meta.nillable:
                            raise ParserError(f"Failed to create target class `{self.prepared.root_type.__name__}`")
                        if parent is not None and node.xsi_nil and not node.meta.nillable:
                            # Match ElementNode.bind: native None has no live
                            # envelope and must not acquire required members.
                            continue
                        position = None
                        if parent is not None and isinstance(self.prepared.programs[plan.use], SequenceProgram):
                            position = parent.position
                            parent.position += 1
                        scope = session.open_scope(plan.use, parent.scope if parent else None, position, None)
                        if parent is not None:
                            parent.sent.add(parent.plan.member)
                        envelope = _LiveEnvelope(plan, node, scope)
                        live.append(envelope)
                        node.bind_attrs(envelope.params)
                        # Missing attributes are final at their start event, including
                        # native defaults; element defaults are not final until EOF.
                        adapter = cast(XsdataRecordAdapter, self.prepared.record_adapters[plan.record.definition])
                        for var in node.meta.get_all_vars():
                            if (var.is_attribute or var.is_attributes) and var.name not in envelope.params:
                                supplied, value = adapter.omitted(var.name, path=plan.record.path)
                                if supplied:
                                    envelope.params[var.name] = value
                        send(envelope)
                    continue

                if isinstance(queue[-1], WrapperNode):
                    parser.end(queue, objects, element.tag, element.text, element.tail)
                    release(element)
                    continue

                path = tuple(ancestry)
                envelope = live[-1]
                if path == envelope.plan.ancestry:
                    queue.pop()
                    envelope.node.bind_content(envelope.params, element.text, element.tail, objects)
                    send(envelope, closing=True)
                    adapter = cast(XsdataRecordAdapter, self.prepared.record_adapters[envelope.plan.record.definition])
                    for name, child in envelope.plan.record.members:
                        if name in envelope.sent:
                            continue
                        supplied, value = adapter.omitted(name, path=self.prepared.programs[child].path)
                        if not supplied:
                            raise StructureMapError(
                                "native_parse_failed",
                                f"Required native XML member {name!r} was not supplied",
                                path=self.prepared.programs[child].path,
                                owner="native limitation",
                                remedy="Provide the required native XML member",
                            )
                        session.member(envelope.scope, name, value)
                        envelope.sent.add(name)
                    session.close_scope(envelope.scope)
                    live.pop()
                else:
                    parser.end(queue, objects, element.tag, element.text, element.tail)
                    if path in envelope.plan.targets:
                        # Native parent binding selects compound choices and retains
                        # DerivedElement wrappers exactly as whole-document parsing.
                        params: dict[str, Any] = {}
                        envelope.node.bind_objects(params, objects)
                        value = params[envelope.plan.member]
                        if isinstance(value, PendingCollection):
                            value = value.evaluate()
                        edge = self.prepared.programs[envelope.plan.edge]
                        if isinstance(edge, SequenceProgram) or (isinstance(edge, VariantProgram) and edge.repeated):
                            for item in value:
                                session.occurrence(envelope.scope, envelope.plan.edge, item, envelope.position)
                                envelope.position += 1
                        else:
                            session.member(envelope.scope, envelope.plan.member, value)
                        envelope.sent.add(envelope.plan.member)
                    elif len(path) == len(envelope.plan.ancestry) + 1:
                        envelope.node.bind_objects(envelope.params, objects)
                        send(envelope)
                ancestry.pop()
                release(element)
        except StructureMapError:
            raise
        except Exception as exc:
            raise StructureMapError(
                "native_parse_failed",
                "xsdata could not parse the XML document",
                path=self.prepared.programs[self.envelopes[-1].edge].path,
                owner="native limitation",
                remedy="Provide a valid document matching the prepared native XML model",
            ) from exc


def prepare_xml_stream(prepared: PreparedMapping[Any], record_path: str) -> XmlStreamPlan:
    """Resolve authored member paths and prove ancestor key readiness before I/O."""
    if not isinstance(prepared.binding, XsdataBinding):
        raise _unsupported(record_path, "Incremental XML requires the xsdata native binding")
    if not isinstance(record_path, str) or not record_path or any(not part for part in record_path.split("/")):
        raise _unsupported(str(record_path), "Use an exact authored mapping member path")
    context = XmlContext()
    current = prepared.root
    use = current
    root = prepared.programs[current]
    if not isinstance(root, RecordProgram):
        raise _unsupported(record_path, "Incremental XML requires a record root")
    meta = context.build(prepared.native_types[root.definition])
    ancestry: tuple[str, ...] = (meta.qname,)
    table, role = prepared.root_table, None
    envelopes: list[_Envelope] = []
    parts = record_path.split("/")
    for index, name in enumerate(parts):
        program = prepared.programs[current]
        if not isinstance(program, RecordProgram) or name not in dict(program.members):
            raise _unsupported(record_path, f"No authored member {name!r} in this record ancestry")
        edge = dict(program.members)[name]
        child = prepared.programs[edge]
        variables = {var.name: var for var in meta.get_all_vars()}
        variable = variables[name]
        names = tuple(variable.elements) if variable.elements else (variable.qname,)
        # WrapperNode is transparent in xsdata's native field binding. The
        # canonical ancestry follows record fields, not optional XML wrappers.
        targets = tuple(ancestry + (qname,) for qname in names)
        envelopes.append(_Envelope(use, program, ancestry, meta, name, edge, targets, table, role))
        if index == len(parts) - 1:
            if not (
                isinstance(child, SequenceProgram)
                or isinstance(child, VariantProgram)
                and child.repeated
                and child.layout == "split"
            ):
                raise _unsupported(record_path, "The selected record path must be a repeated collection")
            if not variable.list_element or variable.is_wildcard:
                raise _unsupported(record_path, "The selected boundary needs declared native XML elements")
            break
        use = edge
        if isinstance(child, (SequenceProgram, LinkProgram)):
            table, role = child.table, child.role
            current = prepared.tables[table].roles[role].program
        elif isinstance(child, RecordProgram):
            current = edge
        else:
            raise _unsupported(child.path, "The selected ancestry must resolve to a native record scope")
        next_program = prepared.programs[current]
        if not isinstance(next_program, RecordProgram) or len(targets) != 1:
            raise _unsupported(child.path, "The selected ancestry must resolve to one native record scope")
        ancestry = targets[0]
        meta = context.build(prepared.native_types[next_program.definition], parent_ns=meta.namespace)
    envelopes = _check_dependencies(prepared, envelopes)
    return XmlStreamPlan(prepared, tuple(envelopes), context)


def _check_dependencies(
    prepared: PreparedMapping[Any],
    envelopes: list[_Envelope],
) -> list[_Envelope]:
    # Each ancestor key is needed before any descendant row can leave the sink.
    # Scalar projections own only their scalar's fields, so the same readiness
    # proof also covers casted key dependencies without inspecting expressions.
    def scalar_leaves(use: int) -> list[ScalarProgram]:
        program = prepared.programs[use]
        if isinstance(program, ScalarProgram):
            return [program]
        if isinstance(program, RecordProgram):
            return [leaf for _, child in program.members for leaf in scalar_leaves(child)]
        if isinstance(program, VariantProgram) and program.layout != "split":
            return [leaf for branch in program.alternatives for leaf in scalar_leaves(branch.program)]
        return []

    result: list[_Envelope] = []
    for envelope in envelopes:
        table = prepared.tables[envelope.table]
        required = set(table.layout.key) if not table.layout.generated else set()
        for other in prepared.tables.values():
            for role in other.roles.values():
                required.update(
                    source.source_column
                    for source in role.key_sources
                    if source.source_table == envelope.table and source.source_role == envelope.role
                )
        if not required:
            result.append(envelope)
            continue
        variables = {var.name: var for var in envelope.meta.get_all_vars()}
        program = envelope.record
        dependencies: list[str] = []
        for name, use in program.members:
            child = prepared.programs[use]
            leaves = [
                leaf for leaf in scalar_leaves(use) if required.intersection(field.name for field in leaf.codec.fields)
            ]
            if not leaves:
                continue
            if name == envelope.member and isinstance(child, RecordProgram):
                # This inline member is itself the next live scope; its members
                # are checked at that scope's actual record boundary.
                continue
            var = variables[name]
            # xsdata accepts element fields independently of declaration order.
            # Only attributes are irrevocably available at the ancestor start;
            # a preceding dataclass field is not an XML ordering guarantee.
            if not (var.is_attribute or var.is_attributes):
                dependency = leaves[0]
                raise _unsupported(
                    dependency.path,
                    f"Ancestor key/projection dependency {dependency.path!r} is not ready "
                    f"before {prepared.programs[envelope.edge].path!r}",
                )
            dependencies.append(name)
        result.append(replace(envelope, dependencies=tuple(dependencies)))
    return result
