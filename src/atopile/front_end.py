"""
Build faebryk core objects from ato DSL.
"""

import inspect
import itertools
import logging
import operator
import os
from collections import defaultdict
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import chain, pairwise
from pathlib import Path
from typing import (
    Any,
    Iterable,
    Sequence,
    Type,
    cast,
)

from antlr4 import ParserRuleContext
from more_itertools import last
from pint import UndefinedUnitError

import faebryk.library._F as F
import faebryk.libs.library.L as L
from atopile import address, errors
from atopile.attributes import GlobalAttributes, _has_ato_cmp_attrs, shim_map
from atopile.config import config
from atopile.datatypes import (
    FieldRef,
    KeyOptItem,
    KeyOptMap,
    ReferencePartType,
    StackList,
    TypeRef,
    is_int,
)
from atopile.parse import parser
from atopile.parser.AtoParser import AtoParser as ap
from atopile.parser.AtoParserVisitor import AtoParserVisitor
from faebryk.core.node import FieldExistsError, NodeException
from faebryk.core.parameter import (
    Arithmetic,
    ConstrainableExpression,
    GreaterOrEqual,
    Is,
    IsSubset,
    LessOrEqual,
    Max,
    Min,
    Parameter,
)
from faebryk.core.trait import Trait
from faebryk.libs.exceptions import accumulate, downgrade, iter_through_errors
from faebryk.libs.library.L import Range, Single
from faebryk.libs.picker.picker import does_not_require_picker_check
from faebryk.libs.sets.quantity_sets import Quantity_Interval, Quantity_Set
from faebryk.libs.sets.sets import BoolSet
from faebryk.libs.units import (
    HasUnit,
    P,
    Quantity,
    UnitCompatibilityError,
    dimensionless,
)
from faebryk.libs.units import (
    Unit as UnitType,
)
from faebryk.libs.util import (
    FuncDict,
    cast_assert,
    groupby,
    has_attr_or_property,
    has_instance_settable_attr,
    import_from_path,
    is_type_pair,
    not_none,
    partition_as_list,
)

logger = logging.getLogger(__name__)


Numeric = Parameter | Arithmetic | Quantity_Set


class from_dsl(Trait.decless()):
    def __init__(self, src_ctx: ParserRuleContext) -> None:
        super().__init__()
        self.src_ctx = src_ctx


class BasicsMixin:
    def visitName(self, ctx: ap.NameContext) -> str:
        """
        If this is an int, convert it to one (for pins),
        else return the name as a string.
        """
        return ctx.getText()

    def visitTypeReference(self, ctx: ap.Type_referenceContext) -> TypeRef:
        return TypeRef(self.visitName(name) for name in ctx.name())

    def visitArrayIndex(self, ctx: ap.Array_indexContext | None) -> str | int | None:
        if ctx is None:
            return None
        if key := ctx.key():
            out = key.getText()
            if is_int(out):
                return int(out)
            return out
        return None

    def visitFieldReferencePart(
        self, ctx: ap.Field_reference_partContext
    ) -> ReferencePartType:
        return ReferencePartType(
            self.visitName(ctx.name()), self.visitArrayIndex(ctx.array_index())
        )

    def visitFieldReference(self, ctx: ap.Field_referenceContext) -> FieldRef:
        pin = ctx.pin_reference_end()
        if pin is not None:
            pin = int(pin.NUMBER().getText())
        return FieldRef(
            parts=(
                self.visitFieldReferencePart(part)
                for part in ctx.field_reference_part()
            ),
            pin=pin,
        )

    def visitString(self, ctx: ap.StringContext) -> str:
        raw: str = ctx.getText()
        return raw.strip("\"'")

    def visitBoolean_(self, ctx: ap.Boolean_Context) -> bool:
        raw: str = ctx.getText()

        if raw.lower() == "true":
            return True
        elif raw.lower() == "false":
            return False

        raise errors.UserException.from_ctx(ctx, f"Expected a boolean value, got {raw}")


class NOTHING:
    """A sentinel object to represent a "nothing" return value."""


class SkipPriorFailedException(Exception):
    """Raised to skip a statement in case a dependency already failed"""


class DeprecatedException(errors.UserException):
    """
    Raised when a deprecated feature is used.
    """

    def get_frozen(self) -> tuple:
        # TODO: this is a bit of a hack to make the logger de-dup these for us
        return errors._BaseBaseUserException.get_frozen(self)


class SequenceMixin:
    """
    The base translator is responsible for methods common to
    navigating from the top of the AST including how to process
    errors, and commonising return types.
    """

    def defaultResult(self):
        """
        Override the default "None" return type
        (for things that return nothing) with the Sentinel NOTHING
        """
        return NOTHING

    def visit_iterable_helper(self, children: Iterable) -> KeyOptMap:
        """
        Visit multiple children and return a tuple of their results,
        discard any results that are NOTHING and flattening the children's results.
        It is assumed the children are returning their own OptionallyNamedItems.
        """

        def _visit():
            for err_cltr, child in iter_through_errors(
                children,
                errors._BaseBaseUserException,
                SkipPriorFailedException,
            ):
                with err_cltr():
                    # Since we're in a SequenceMixin, we need to cast self to the visitor type # noqa: E501  # pre-existing
                    child_result = cast(AtoParserVisitor, self).visit(child)
                    if child_result is not NOTHING:
                        yield child_result

        child_results = chain.from_iterable(_visit())
        child_results = filter(lambda x: x is not NOTHING, child_results)
        child_results = KeyOptMap(KeyOptItem(cr) for cr in child_results)

        return KeyOptMap(child_results)

    def visitFile_input(self, ctx: ap.File_inputContext) -> KeyOptMap:
        return self.visit_iterable_helper(ctx.stmt())

    def visitSimple_stmts(self, ctx: ap.Simple_stmtsContext) -> KeyOptMap:
        return self.visit_iterable_helper(ctx.simple_stmt())

    def visitBlock(self, ctx: ap.BlockContext) -> KeyOptMap:
        if ctx.stmt():
            return self.visit_iterable_helper(ctx.stmt())
        if ctx.simple_stmts():
            return self.visitSimple_stmts(ctx.simple_stmts())

        raise ValueError  # this should be protected because it shouldn't be parseable


class BlockNotFoundError(errors.UserKeyError):
    """
    Raised when a block doesn't exist.
    """


@dataclass
class Context:
    """~A metaclass to hold context/origin information on ato classes."""

    @dataclass
    class ImportPlaceholder:
        ref: TypeRef
        from_path: str
        original_ctx: ParserRuleContext

    # Location information re. the source of this module
    file_path: Path | None

    # Scope information
    scope_ctx: ap.BlockdefContext | ap.File_inputContext
    refs: dict[TypeRef, Type[L.Node] | ap.BlockdefContext | ImportPlaceholder]


class Wendy(BasicsMixin, SequenceMixin, AtoParserVisitor):  # type: ignore  # Overriding base class makes sense here
    """
    Wendy is Bob's business partner and fellow builder in the children's TV series
    "Bob the Builder." She is a skilled construction worker who often manages the
    business side of their building company while also participating in hands-on
    construction work. Wendy is portrayed as capable, practical and level-headed,
    often helping to keep projects organized and on track. She wears a green safety
    helmet and work clothes, and is known for her competence in operating various
    construction vehicles and equipment.

    Wendy also knows where to find the best building supplies.
    """

    def visitImport_stmt(
        self, ctx: ap.Import_stmtContext
    ) -> KeyOptMap[tuple[Context.ImportPlaceholder, ap.Import_stmtContext]]:
        if from_path := ctx.string():
            lazy_imports = [
                Context.ImportPlaceholder(
                    ref=self.visitTypeReference(reference),
                    from_path=self.visitString(from_path),
                    original_ctx=ctx,
                )
                for reference in ctx.type_reference()
            ]
            return KeyOptMap(
                KeyOptItem.from_kv(li.ref, (li, ctx)) for li in lazy_imports
            )

        else:
            # Standard library imports are special, and don't require a from path
            imports = []
            for collector, reference in iter_through_errors(ctx.type_reference()):
                with collector():
                    ref = self.visitTypeReference(reference)
                    if len(ref) > 1:
                        raise errors.UserKeyError.from_ctx(
                            ctx, "Standard library imports must be single-name"
                        )

                    name = ref[0]
                    if not hasattr(F, name) or not issubclass(
                        getattr(F, name), (L.Module, L.ModuleInterface)
                    ):
                        raise errors.UserKeyError.from_ctx(
                            ctx, f"Unknown standard library module: '{name}'"
                        )

                    imports.append(KeyOptItem.from_kv(ref, (getattr(F, name), ctx)))

            return KeyOptMap(imports)

    def visitDep_import_stmt(
        self, ctx: ap.Dep_import_stmtContext
    ) -> KeyOptMap[tuple[Context.ImportPlaceholder, ap.Dep_import_stmtContext]]:
        lazy_import = Context.ImportPlaceholder(
            ref=self.visitTypeReference(ctx.type_reference()),
            from_path=self.visitString(ctx.string()),
            original_ctx=ctx,
        )
        # TODO: @v0.4 remove this deprecated import form
        with downgrade(DeprecatedException):
            raise DeprecatedException.from_ctx(
                ctx,
                "`import <something> from <path>` is deprecated and"
                " will be removed in a future version. Use "
                f"`from {ctx.string().getText()} import"
                f" {ctx.type_reference().getText()}`"
                " instead.",
            )
        return KeyOptMap.from_kv(lazy_import.ref, (lazy_import, ctx))

    def visitBlockdef(
        self, ctx: ap.BlockdefContext
    ) -> KeyOptMap[tuple[ap.BlockdefContext, ap.BlockdefContext]]:
        ref = TypeRef.from_one(self.visitName(ctx.name()))
        return KeyOptMap.from_kv(ref, (ctx, ctx))

    def visitSimple_stmt(
        self, ctx: ap.Simple_stmtContext | Any
    ) -> (
        KeyOptMap[
            tuple[Context.ImportPlaceholder | ap.BlockdefContext, ParserRuleContext]
        ]
        | type[NOTHING]
    ):
        if ctx.import_stmt() or ctx.dep_import_stmt():
            return super().visitChildren(ctx)
        return NOTHING

    # TODO: @v0.4: remove this shimming
    @staticmethod
    def _find_shim(
        file_path: Path | None, ref: TypeRef
    ) -> tuple[Type[L.Node], str] | None:
        if file_path is None:
            return None

        import_addr = address.AddrStr.from_parts(file_path, str(TypeRef(ref)))

        for shim_addr in shim_map:
            if import_addr.endswith(shim_addr):
                return shim_map[shim_addr]

        return None

    @classmethod
    def survey(
        cls, file_path: Path | None, ctx: ap.BlockdefContext | ap.File_inputContext
    ) -> Context:
        surveyor = cls()
        context = Context(file_path=file_path, scope_ctx=ctx, refs={})
        for ref, (item, item_ctx) in surveyor.visit(ctx):
            assert isinstance(item_ctx, ParserRuleContext)
            if ref in context.refs:
                # Downgrade the error in case we're shadowing things
                # Not limiting the number of times we show this warning
                # because they're pretty important and Wendy is well cached
                with downgrade(errors.UserKeyError):
                    raise errors.UserKeyError.from_ctx(
                        item_ctx,
                        f"`{ref}` already declared. Shadowing original."
                        " In the future this may be an error",
                    )

            # TODO: @v0.4: remove this shimming
            if shim := cls._find_shim(context.file_path, ref):
                shim_cls, preferred = shim

                if hasattr(item_ctx, "name"):
                    dep_ctx = item_ctx.name()  # type: ignore
                elif hasattr(item_ctx, "reference"):
                    dep_ctx = item_ctx.reference()  # type: ignore
                else:
                    dep_ctx = item_ctx

                # TODO: @v0.4 increase the level of this to WARNING
                # when there's an alternative
                with downgrade(DeprecatedException, to_level=logging.DEBUG):
                    raise DeprecatedException.from_ctx(
                        dep_ctx,
                        f"`{ref}` is deprecated and will be removed in a future"
                        f" version. Use `{preferred}` instead.",
                    )

                context.refs[ref] = shim_cls
            else:
                context.refs[ref] = item

        return context


@contextmanager
def ato_error_converter():
    try:
        yield
    except NodeException as ex:
        if from_dsl_ := ex.node.try_get_trait(from_dsl):
            raise errors.UserException.from_ctx(from_dsl_.src_ctx, str(ex)) from ex
        else:
            raise ex


@contextmanager
def _attach_ctx_to_ex(ctx: ParserRuleContext, traceback: Sequence[ParserRuleContext]):
    try:
        yield
    except errors.UserException as ex:
        if ex.origin_start is None:
            ex.attach_origin_from_ctx(ctx)
            # only attach traceback if we're also setting the origin
            if ex.traceback is None:
                ex.traceback = traceback
        raise ex


_declaration_domain_to_unit = {
    "dimensionless": dimensionless,
    "resistance": P.ohm,
    "capacitance": P.farad,
    "inductance": P.henry,
    "voltage": P.volt,
    "current": P.ampere,
    "power": P.watt,
    "frequency": P.hertz,
}


@dataclass
class _ParameterDefinition:
    """
    Holds information about a parameter declaration or assignment.
    We collect those per parameter in the `Bob._param_assignments` dict.
    Multiple assignments are allowed, but they interact with each other in non-trivial
    ways. Thus we need to track them and process them in the end in
    `_merge_parameter_assignments`.
    """

    ctx: ParserRuleContext
    traceback: Sequence[ParserRuleContext]
    ref: FieldRef
    value: Range | Single | None = None

    @property
    def is_declaration(self) -> bool:
        return self.value is None

    @property
    def is_definition(self) -> bool:
        return not self.is_declaration

    def __post_init__(self):
        pass

    @property
    def is_root_assignment(self) -> bool:
        if not self.is_definition:
            return False
        return len(self.ref) == 1


class Bob(BasicsMixin, SequenceMixin, AtoParserVisitor):  # type: ignore  # Overriding base class makes sense here
    """
    Bob is a general contractor who runs his own construction company in the town
    of Fixham Harbour (in earlier episodes, he was based in Bobsville). Recognizable
    by his blue jeans, checked shirt, yellow hard hat, and tool belt, Bob is known
    for his positive catchphrase "Can we fix it? Yes, we can!" He's portrayed as a
    friendly, optimistic problem-solver who takes pride in helping his community
    through various building and repair projects. Bob works closely with his team
    of anthropomorphic construction vehicles and his business partner Wendy,
    tackling each construction challenge with enthusiasm and determination. His
    character embodies values of teamwork, perseverance, and taking pride in one's
    work.
    """

    def __init__(self) -> None:
        super().__init__()
        self._scopes = FuncDict[ParserRuleContext, Context]()
        self._python_classes = FuncDict[ap.BlockdefContext, Type[L.Module]]()
        self._node_stack = StackList[L.Node]()
        self._traceback_stack = StackList[ParserRuleContext]()

        self._param_assignments = defaultdict[Parameter, list[_ParameterDefinition]](
            list
        )
        self.search_paths: list[os.PathLike] = []

        # Keeps track of the nodes whose construction failed,
        # so we don't report dud key errors when it was a higher failure
        # that caused the node not to exist
        self._failed_nodes = FuncDict[L.Node, set[str]]()

    def build_ast(
        self, ast: ap.File_inputContext, ref: TypeRef, file_path: Path | None = None
    ) -> L.Node:
        """Build a Module from an AST and reference."""
        file_path = self._sanitise_path(file_path) if file_path else None
        context = self.index_ast(ast, file_path)
        return self._build(context, ref)

    def build_file(self, path: Path, ref: TypeRef) -> L.Node:
        """Build a Module from a file and reference."""
        context = self.index_file(self._sanitise_path(path))
        return self._build(context, ref)

    @property
    def modules(self) -> dict[address.AddrStr, Type[L.Module]]:
        """Conceptually similar to `sys.modules`"""

        # FIXME: this feels like a shit way to get addresses of the imported modules
        def _get_addr(ctx: ParserRuleContext):
            ref = tuple()
            ctx_ = ctx
            while ctx_ not in self._scopes:
                if isinstance(ctx_, ap.BlockdefContext):
                    ref = (ctx_.name().getText(),) + ref
                ctx_ = ctx_.parentCtx
                if ctx_ is None:
                    return None

            return address.AddrStr.from_parts(
                self._scopes[ctx_].file_path, str(TypeRef(ref))
            )

        return {
            addr: cls
            for ctx, cls in self._python_classes.items()
            if (addr := _get_addr(ctx)) is not None
        }

    def _build(self, context: Context, ref: TypeRef) -> L.Node:
        assert self._is_reset()

        if ref not in context.refs:
            raise errors.UserKeyError.from_ctx(
                context.scope_ctx, f"No declaration of `{ref}` in {context.file_path}"
            )
        try:
            class_ = self._get_referenced_class(context.scope_ctx, ref)
            if not isinstance(class_, ap.BlockdefContext):
                raise errors.UserNotImplementedError(
                    "Can't initialize a fabll directly like this"
                )
            with self._traceback_stack.enter(class_.name()):
                with self._init_node(class_) as node:
                    node.add(F.is_app_root())
                return node
        except* SkipPriorFailedException:
            raise errors.UserException("Build failed")
        finally:
            self._finish()

    def _is_reset(self) -> bool:
        """
        Make sure caches that aren't intended to be shared between builds are empty.
        True if the caches are empty, False if they are not.
        """
        return (
            not self._node_stack
            and not self._traceback_stack
            and not self._param_assignments
        )

    def _finish(self):
        self._merge_parameter_assignments()
        assert self._is_reset()

    class ParamAssignmentIsGospel(errors.UserException):
        """
        The parameter assignment is treated as the precise specification of the
        component, rather than merely as a requirement for it's later selection
        """

        title = "Parameter assignments are component definition"  # type: ignore

    def _merge_parameter_assignments(self):
        with accumulate(
            errors._BaseBaseUserException, SkipPriorFailedException
        ) as ex_acc:
            # Handle missing definitions
            params_without_defitions, params_with_definitions = partition_as_list(
                lambda p: any(a.is_definition for a in self._param_assignments[p]),
                self._param_assignments,
            )

            for param in params_without_defitions:
                last_declaration = last(self._param_assignments.pop(param))
                with ex_acc.collect(), ato_error_converter():
                    # TODO: @v0.4 remove this deprecated import form
                    with downgrade(
                        errors.UserActionWithoutEffectError, to_level=logging.DEBUG
                    ):
                        raise errors.UserActionWithoutEffectError.from_ctx(
                            last_declaration.ctx,
                            f"Attribute `{param}` declared but never assigned.",
                            traceback=last_declaration.traceback,
                        )
            # Handle parameter assignments
            # assignments override each other
            # assignments made in the block definition of the component are "is"
            # external assignments are requirements treated as a subset

            # Allowing external assignments in the first place is a bit weird
            # Got to figure out how people are using this.
            # My guess is that in 99% of cases you can replace them by a `&=`
            params_by_node = groupby(
                params_with_definitions, key=lambda p: p.get_parent_force()[0]
            )
            for assignee_node, assigned_params in params_by_node.items():
                is_part_module = isinstance(assignee_node, L.Module) and (
                    assignee_node.has_trait(F.is_pickable_by_supplier_id)
                    or assignee_node.has_trait(F.is_pickable_by_part_number)
                )

                gospel_params: list[Parameter] = []
                for param in assigned_params:
                    assignments = self._param_assignments.pop(param)
                    definitions = [a for a in assignments if a.is_definition]
                    non_root_definitions, root_definitions = partition_as_list(
                        lambda a: a.is_root_assignment, definitions
                    )

                    assert definitions
                    for definition in definitions:
                        logger.debug(
                            "Assignment:  %s [%s] := %s",
                            param,
                            definition.ref,
                            definition.value,
                        )

                    # Don't see how this could happen, but just in case
                    root_after_external = (False, True) in pairwise(
                        a.is_root_assignment for a in definitions
                    )
                    assert not root_after_external

                    with ex_acc.collect(), ato_error_converter():
                        # Workaround for missing difference between alias and subset
                        # Only relevant for code-as-data part modules
                        # TODO: consider a warning for definitions that aren't purely
                        # narrowing
                        if is_part_module and non_root_definitions:
                            raise errors.UserNotImplementedError.from_ctx(
                                last(non_root_definitions).ctx,
                                "You can't assign to a `component` with a specific"
                                " part number outside of its definition",
                                traceback=last(non_root_definitions).traceback,
                            )

                        elif is_part_module and root_definitions:
                            param.alias_is(not_none(last(root_definitions).value))
                            param.add(does_not_require_picker_check())
                            gospel_params.append(param)

                        elif not is_part_module:
                            definition = last(definitions)
                            value = not_none(definition.value)
                            try:
                                logger.debug("Constraining %s to %s", param, value)
                                param.constrain_subset(value)
                            except UnitCompatibilityError as ex:
                                raise errors.UserTypeError.from_ctx(
                                    definition.ctx,
                                    str(ex),
                                    traceback=definition.traceback,
                                ) from ex

                if gospel_params:
                    with downgrade(self.ParamAssignmentIsGospel, to_level=logging.INFO):
                        raise self.ParamAssignmentIsGospel(
                            f"`component` `{assignee_node.get_full_name()}`"
                            " is completely specified by a part number, so these"
                            " params are treated as its exact specification:"
                            + ", ".join(f"`{p}`" for p in gospel_params)
                        )

    @property
    def _current_node(self) -> L.Node:
        return self._node_stack[-1]

    def get_traceback(self) -> Sequence[ParserRuleContext]:
        """Return the current traceback, with sequential duplicates removed"""
        # Use dict ordering guarantees and key uniqueness to remove duplicates
        return list(dict.fromkeys(self._traceback_stack).keys())

    @staticmethod
    def _sanitise_path(path: os.PathLike) -> Path:
        return Path(path).expanduser().resolve().absolute()

    def index_ast(
        self, ast: ap.File_inputContext, file_path: Path | None = None
    ) -> Context:
        if ast in self._scopes:
            return self._scopes[ast]

        context = Wendy.survey(file_path, ast)
        self._scopes[ast] = context
        return context

    def index_file(self, file_path: Path) -> Context:
        ast = parser.get_ast_from_file(file_path)
        return self.index_ast(ast, file_path)

    def _get_search_paths(self, context: Context) -> list[Path]:
        search_paths = [Path(p) for p in self.search_paths]

        if context.file_path is not None:
            search_paths.insert(0, context.file_path.parent)

        if config.has_project:
            search_paths += [config.project.paths.src, config.project.paths.modules]

        # Add the library directory to the search path too
        search_paths.append(Path(inspect.getfile(F)).parent)

        return search_paths

    def _import_item(
        self, context: Context, item: Context.ImportPlaceholder
    ) -> Type[L.Node] | ap.BlockdefContext:
        # Build up search paths to check for the import in
        # Iterate though them, checking if any contains the thing we're looking for
        search_paths = self._get_search_paths(context)
        for search_path in search_paths:
            candidate_from_path = search_path / item.from_path
            if candidate_from_path.exists():
                break
        else:
            raise errors.UserFileNotFoundError.from_ctx(
                item.original_ctx,
                f"Can't find {item.from_path} in {', '.join(map(str, search_paths))}",
            )

        from_path = self._sanitise_path(candidate_from_path)
        if from_path.suffix == ".py":
            try:
                node = import_from_path(from_path)
            except FileNotFoundError as ex:
                raise errors.UserImportNotFoundError.from_ctx(
                    item.original_ctx, str(ex)
                ) from ex

            for ref in item.ref:
                try:
                    node = getattr(node, ref)
                except AttributeError as ex:
                    raise errors.UserKeyError.from_ctx(
                        item.original_ctx, f"No attribute `{ref}` found on {node}"
                    ) from ex

            assert isinstance(node, type) and issubclass(node, L.Node)
            return node

        elif from_path.suffix == ".ato":
            context = self.index_file(from_path)
            if item.ref not in context.refs:
                raise errors.UserKeyError.from_ctx(
                    item.original_ctx, f"No declaration of `{item.ref}` in {from_path}"
                )
            node = context.refs[item.ref]

            if isinstance(node, Context.ImportPlaceholder):
                raise errors.UserTypeError.from_ctx(
                    item.original_ctx,
                    "Importing a import is not supported",
                )

            assert (
                isinstance(node, type)
                and issubclass(node, L.Node)
                or isinstance(node, ap.BlockdefContext | ap.File_inputContext)
            )
            return node

        else:
            raise errors.UserImportNotFoundError.from_ctx(
                item.original_ctx, f"Can't import file type {from_path.suffix}"
            )

    def _get_referenced_class(
        self, ctx: ParserRuleContext, ref: TypeRef
    ) -> Type[L.Node] | ap.BlockdefContext:
        """
        Returns the class / object referenced by the given ref,
        based on Bob's current context. The contextual nature
        of this means that it's only useful during the build process.
        """
        # No change in position from the current context
        # return self, eg the current parser context
        if ref == tuple():
            if isinstance(ctx, ap.BlockdefContext):
                return ctx
            else:
                raise ValueError(f"Can't get class `{ref}` from {ctx}")

        # Ascend the tree until we find a scope that has the ref within it
        ctx_ = ctx
        while ctx_ not in self._scopes:
            if ctx_.parentCtx is None:
                raise ValueError(f"No scope found for `{ref}`")
            ctx_ = ctx_.parentCtx

        context = self._scopes[ctx_]

        # FIXME: there are more cases to check here,
        # eg. if we have part of a ref resolved
        if ref not in context.refs:
            raise errors.UserKeyError.from_ctx(
                ctx, f"No class or block definition found for `{ref}`"
            )

        item = context.refs[ref]
        # Ensure the item is resolved, if not already
        if isinstance(item, Context.ImportPlaceholder):
            # TODO: search path for these imports
            item = self._import_item(context, item)
            context.refs[ref] = item

        return item

    @staticmethod
    def get_node_attr(node: L.Node, ref: ReferencePartType) -> L.Node:
        """
        Analogous to `getattr`

        Returns the value if it exists, otherwise raises an AttributeError
        Required because we're seeing attributes in both the attrs and runtime
        """

        if has_attr_or_property(node, ref.name):
            # Build-time attributes are attached as real attributes
            result = getattr(node, ref.name)
            if ref.key is not None and isinstance(result, L.Node):
                raise ValueError(f"{ref.name} is not subscriptable")
            if not isinstance(result, L.Node) and ref.key is None:
                raise ValueError(
                    f"{ref.name} is a {type(result)._ref.name__} and needs a ref.key"
                )
            if isinstance(result, dict):
                assert ref.key is not None
                if ref.key not in result:
                    raise AttributeError(name=f"{ref.name}[{ref.key}]", obj=node)
                result = result[ref.key]
            elif isinstance(result, list):
                assert ref.key is not None
                # TODO type check key
                if not isinstance(ref.key, int):
                    raise ValueError(f"Key `{ref.key}` is not an integer")
                if ref.key >= len(result):
                    raise AttributeError(name=f"{ref.name}[{ref.key}]", obj=node)
                result = result[ref.key]
            # TODO handle non-module & non-dict & non-list case
        elif ref.name in node.runtime and ref.key is None:
            # Runtime attributes are attached as runtime attributes
            result = node.runtime[ref.name]
        else:
            # Wah wah wah - we don't know what this is
            friendlyname = ref.name if ref.key is None else f"{ref.name}[{ref.key}]"
            raise AttributeError(name=friendlyname, obj=node)

        if isinstance(result, L.Module):
            return result.get_most_special()

        return result

    def _get_referenced_node(self, ref: FieldRef, ctx: ParserRuleContext) -> L.Node:
        node = self._current_node
        for i, name in enumerate(ref):
            try:
                node = self.get_node_attr(node, name)
            except AttributeError as ex:
                # If we know that a previous failure prevented the creation
                # of this node, raise a SkipPriorFailedException to prevent
                # error messages about it missing from polluting the output
                if name in self._failed_nodes.get(node, set()):
                    raise SkipPriorFailedException() from ex

                # Wah wah wah - we don't know what this is
                # Build a nice error message
                if i > 0:
                    msg = f"`{FieldRef(ref.parts[:i])}` has no attribute `{ex.name}`"
                else:
                    msg = f"No attribute `{name}`"
                raise errors.UserKeyError.from_ctx(
                    ctx, msg, traceback=self.get_traceback()
                ) from ex
            except ValueError as ex:
                raise errors.UserKeyError.from_ctx(
                    ctx, str(ex), traceback=self.get_traceback()
                ) from ex

        return node

    def _try_get_referenced_node(
        self, ref: FieldRef, ctx: ParserRuleContext
    ) -> L.Node | None:
        try:
            return self._get_referenced_node(ref, ctx)
        except errors.UserKeyError:
            return None

    def _new_node(
        self,
        item: ap.BlockdefContext | Type[L.Node],
        promised_supers: list[ap.BlockdefContext],
    ) -> tuple[L.Node, list[ap.BlockdefContext]]:
        """
        Kind of analogous to __new__ in Python, except that it's a factory

        Descends down the class hierarchy until it finds a known base class.
        As it descends, it logs all superclasses it encounters, as `promised_supers`.
        These are accumulated lowest (base-class) to highest (what was initialised).

        Once a base class is found, it creates a new class for each superclass that
        isn't already known, attaching the __atopile_src_ctx__ attribute to the new
        class.
        """
        if isinstance(item, type) and issubclass(item, L.Node):
            super_class = item
            for super_ctx in promised_supers:
                if super_ctx in self._python_classes:
                    super_class = self._python_classes[super_ctx]
                    continue

                assert issubclass(super_class, L.Node)

                # Create a new type with a more descriptive name
                type_name = super_ctx.name().getText()
                type_qualname = f"{super_class.__module__}.{type_name}"

                super_class = type(
                    type_name,  # Class name
                    (super_class,),  # Base classes
                    {
                        "__module__": super_class.__module__,
                        "__qualname__": type_qualname,
                        "__atopile_src_ctx__": super_ctx,
                    },
                )

                self._python_classes[super_ctx] = super_class

            assert issubclass(super_class, L.Node)
            return super_class(), promised_supers

        if isinstance(item, ap.BlockdefContext):
            # Find the superclass of the new node, if there's one defined
            block_type = item.blocktype()
            if super_ctx := item.blockdef_super():
                super_ref = self.visitTypeReference(super_ctx.type_reference())
                # Create a base node to build off
                base_class = self._get_referenced_class(item, super_ref)
            else:
                # Create a shell of base-node to build off
                assert isinstance(block_type, ap.BlocktypeContext)
                if block_type.INTERFACE():
                    base_class = L.ModuleInterface
                elif block_type.COMPONENT():
                    base_class = L.Module
                elif block_type.MODULE():
                    base_class = L.Module
                else:
                    raise ValueError(f"Unknown block type `{block_type.getText()}`")

            # Descend into building the superclass. We've got no information
            # on when the super-chain will be resolved, so we need to promise
            # that this current blockdef will be visited as part of the init
            result = self._new_node(
                base_class,
                promised_supers=[item] + promised_supers,
            )

            return result

        # This should never happen
        raise ValueError(f"Unknown item type `{item}`")

    @contextmanager
    def _init_node(
        self, node_type: ap.BlockdefContext | Type[L.Node]
    ) -> Generator[L.Node, None, None]:
        """
        Kind of analogous to __init__ in Python, except that it's a factory

        Pre-yield it is analogous to __new__, where it creates the hollow instance
        Post-yield it is analogous to __init__, where it fills in the details

        This is to allow for it to be attached in the graph before it's filled,
        and subsequently for errors to be raised in context of it's graph location.
        """
        new_node, promised_supers = self._new_node(
            node_type,
            promised_supers=[],
        )

        # Shim on component and module classes defined in ato
        # Do not shim fabll modules, or interfaces
        if isinstance(node_type, ap.BlockdefContext):
            if node_type.blocktype().COMPONENT() or node_type.blocktype().MODULE():
                # Some shims add the trait themselves
                if not new_node.has_trait(_has_ato_cmp_attrs):
                    new_node.add(_has_ato_cmp_attrs())

        yield new_node

        with self._node_stack.enter(new_node):
            for super_ctx in promised_supers:
                # TODO: this would be better if we had the
                # "from xyz" super in the traceback too
                with self._traceback_stack.enter(super_ctx.name()):
                    self.visitBlock(super_ctx.block())

    def _get_param(
        self, node: L.Node, ref: ReferencePartType, src_ctx: ParserRuleContext
    ) -> Parameter:
        """
        Get a param from a node.
        Not supported: If it doesn't exist, create it and promise to assign
        it later. Used in forward-declaration.
        """
        try:
            node = self.get_node_attr(node, ref)
        except AttributeError as ex:
            if ref in self._failed_nodes.get(node, set()):
                raise SkipPriorFailedException() from ex
            # Wah wah wah - we don't know what this is
            raise errors.UserNotImplementedError.from_ctx(
                src_ctx,
                f"Parameter `{ref}` not found and"
                " forward-declared params are not yet implemented",
                traceback=self.get_traceback(),
            ) from ex
        except ValueError as ex:
            raise errors.UserValueError.from_ctx(
                src_ctx, str(ex), traceback=self.get_traceback()
            ) from ex

        if not isinstance(node, Parameter):
            raise errors.UserSyntaxError.from_ctx(
                src_ctx,
                f"Node {ref} is {type(node)} not a Parameter",
                traceback=self.get_traceback(),
            )
        return node

    def _ensure_param(
        self,
        node: L.Node,
        ref: ReferencePartType,
        unit: UnitType,
        src_ctx: ParserRuleContext,
    ) -> Parameter:
        """
        Ensure a node has a param with a given name
        If it already exists, check the unit is compatible and return it
        """

        try:
            param = self.get_node_attr(node, ref)
        except AttributeError:
            # Here we attach only minimal information, so we can override it later
            if ref.key is not None:
                if not isinstance(ref.key, str):
                    raise errors.UserNotImplementedError.from_ctx(
                        src_ctx,
                        f"Can't forward assign to a non-string key `{ref}`",
                        traceback=self.get_traceback(),
                    )
                container = getattr(node, ref.name)
                param = node.add(
                    Parameter(units=unit, domain=L.Domains.Numbers.REAL()),
                    name=ref.key,
                    container=container,
                )
            else:
                param = node.add(
                    Parameter(units=unit, domain=L.Domains.Numbers.REAL()),
                    name=ref.name,
                )
        except ValueError as ex:
            raise errors.UserValueError.from_ctx(
                src_ctx, str(ex), traceback=self.get_traceback()
            ) from ex
        else:
            if not isinstance(param, Parameter):
                raise errors.UserTypeError.from_ctx(
                    src_ctx,
                    f"Cannot assign a parameter to `{ref}` on `{node}` because its"
                    f" type is `{param.__class__.__name__}`",
                    traceback=self.get_traceback(),
                )

        if not param.units.is_compatible_with(unit):
            raise errors.UserIncompatibleUnitError.from_ctx(
                src_ctx,
                f"Given units ({unit}) are incompatible"
                f" with existing units ({param.units}).",
                traceback=self.get_traceback(),
            )

        return param

    def _record_failed_node(self, node: L.Node, name: str):
        self._failed_nodes.setdefault(node, set()).add(name)

    def visitAssign_stmt(self, ctx: ap.Assign_stmtContext):
        """Assignment values and create new instance of things."""
        dec = ctx.field_reference_or_declaration()
        assigned_ref = self.visitFieldReference(
            dec.field_reference() or dec.declaration_stmt().field_reference()
        )

        assigned_name: ReferencePartType = assigned_ref[-1]
        assignable_ctx = ctx.assignable()
        assert isinstance(assignable_ctx, ap.AssignableContext)
        target = self._get_referenced_node(assigned_ref.stem, ctx)

        ########## Handle New Statements ##########
        if new_stmt_ctx := assignable_ctx.new_stmt():
            if len(assigned_ref) > 1:
                raise errors.UserSyntaxError.from_ctx(
                    ctx,
                    f"Can't declare fields in a nested object `{assigned_ref}`",
                    traceback=self.get_traceback(),
                )
            if assigned_name.key is not None:
                raise errors.UserSyntaxError.from_ctx(
                    ctx,
                    f"Can't use keys with `new` statements `{assigned_ref}`",
                    traceback=self.get_traceback(),
                )

            assert isinstance(new_stmt_ctx, ap.New_stmtContext)
            ref = self.visitTypeReference(new_stmt_ctx.type_reference())

            try:
                with self._traceback_stack.enter(new_stmt_ctx):
                    with self._init_node(
                        self._get_referenced_class(ctx, ref)
                    ) as new_node:
                        try:
                            self._current_node.add(new_node, name=assigned_name.name)
                        except FieldExistsError as e:
                            raise errors.UserAlreadyExistsError.from_ctx(
                                ctx,
                                f"Field `{assigned_name}` already exists",
                                traceback=self.get_traceback(),
                            ) from e
                        new_node.add(from_dsl(ctx))
            except Exception:
                # Not a narrower exception because it's often an ExceptionGroup
                self._record_failed_node(self._current_node, assigned_name.name)
                raise

            return NOTHING

        ########## Handle Regular Assignments ##########
        value = self.visit(assignable_ctx)
        # Arithmetic
        if assignable_ctx.literal_physical() or assignable_ctx.arithmetic_expression():
            declaration = ctx.field_reference_or_declaration().declaration_stmt()
            if declaration:
                # check valid declaration
                # create param with corresponding units
                self.visitDeclaration_stmt(declaration)

            unit = HasUnit.get_units(value)
            param = self._ensure_param(target, assigned_name, unit, ctx)
            self._param_assignments[param].append(
                _ParameterDefinition(
                    ref=assigned_ref,
                    value=value,
                    ctx=ctx,
                    traceback=self.get_traceback(),
                )
            )

        # String or boolean
        elif assignable_ctx.string() or assignable_ctx.boolean_():
            if assigned_name.key is not None:
                raise errors.UserSyntaxError.from_ctx(
                    ctx,
                    f"Can't use keys with non-arithmetic attribute assignments "
                    f"`{assigned_ref}`",
                    traceback=self.get_traceback(),
                )

            # Check if it's a property or attribute that can be set
            if has_instance_settable_attr(target, assigned_name.name):
                try:
                    setattr(target, assigned_name.name, value)
                except errors.UserException as e:
                    e.attach_origin_from_ctx(assignable_ctx)
                    raise
            elif (
                # If ModuleShims has a settable property, use it
                hasattr(GlobalAttributes, assigned_name.name)
                and isinstance(getattr(GlobalAttributes, assigned_name.name), property)
                and getattr(GlobalAttributes, assigned_name.name).fset
            ):
                prop = cast_assert(
                    property, getattr(GlobalAttributes, assigned_name.name)
                )
                assert prop.fset is not None
                # TODO: @v0.4 remove this deprecated import form
                with (
                    downgrade(DeprecatedException, errors.UserNotImplementedError),
                    _attach_ctx_to_ex(ctx, self.get_traceback()),
                ):
                    prop.fset(target, value)
            else:
                # Strictly, these are two classes of errors that could use independent
                # suppression, but we'll just suppress them both collectively for now
                # TODO: @v0.4 remove this deprecated import form
                with downgrade(errors.UserException):
                    raise errors.UserException.from_ctx(
                        ctx,
                        f"Ignoring assignment of `{value}` to `{assigned_name}` on"
                        f" `{target}`",
                        traceback=self.get_traceback(),
                    )

        else:
            raise ValueError(f"Unhandled assignable type `{assignable_ctx.getText()}`")

        return NOTHING

    def _get_mif_and_warn_when_exists(
        self, name: ReferencePartType, ctx: ParserRuleContext
    ) -> L.ModuleInterface | None:
        try:
            mif = self.get_node_attr(self._current_node, name)
        except AttributeError:
            return None
        except ValueError as ex:
            raise errors.UserValueError.from_ctx(
                ctx, str(ex), traceback=self.get_traceback()
            ) from ex

        if isinstance(mif, L.ModuleInterface):
            # TODO: @v0.4 remove this deprecated import form
            with downgrade(errors.UserAlreadyExistsError):
                raise errors.UserAlreadyExistsError(
                    f"`{name}` already exists; skipping."
                )
        else:
            raise errors.UserTypeError.from_ctx(
                ctx,
                f"`{name}` already exists.",
                traceback=self.get_traceback(),
            )

        return mif

    def visitPindef_stmt(
        self, ctx: ap.Pindef_stmtContext
    ) -> KeyOptMap[L.ModuleInterface]:
        return self.visitPin_stmt(ctx.pin_stmt(), declaration=False)

    def visitPin_declaration(
        self, ctx: ap.Pin_declarationContext
    ) -> KeyOptMap[L.ModuleInterface]:
        return self.visitPin_stmt(ctx.pin_stmt(), declaration=True)

    def visitPin_stmt(
        self, ctx: ap.Pin_stmtContext, declaration: bool
    ) -> KeyOptMap[L.ModuleInterface]:
        if ctx.name():
            name = self.visitName(ctx.name())
        elif ctx.totally_an_integer():
            name = f"{ctx.totally_an_integer().getText()}"
        elif ctx.string():
            name = self.visitString(ctx.string())
        else:
            raise ValueError(f"Unhandled pin name type `{ctx}`")

        ref = FieldRef(parts=[], pin=name).last
        if declaration:
            if mif := self._get_mif_and_warn_when_exists(ref, ctx):
                return KeyOptMap.from_item(
                    KeyOptItem.from_kv(TypeRef.from_one(name), mif)
                )
        else:
            try:
                mif = self.get_node_attr(self._current_node, ref)
            except AttributeError:
                pass
            else:
                return KeyOptMap.from_item(
                    KeyOptItem.from_kv(TypeRef.from_one(name), mif)
                )

        if shims_t := self._current_node.try_get_trait(_has_ato_cmp_attrs):
            mif = shims_t.add_pin(name, ref.name)
            return KeyOptMap.from_item(KeyOptItem.from_kv(TypeRef.from_one(name), mif))

        raise errors.UserTypeError.from_ctx(
            ctx,
            f"Can't declare pins on components of type {self._current_node}",
            traceback=self.get_traceback(),
        )

    def visitSignaldef_stmt(
        self, ctx: ap.Signaldef_stmtContext
    ) -> KeyOptMap[L.ModuleInterface]:
        name = self.visitName(ctx.name())
        # TODO: @v0.4: remove this protection
        if mif := self._get_mif_and_warn_when_exists(ReferencePartType(name), ctx):
            return KeyOptMap.from_item(KeyOptItem.from_kv(TypeRef.from_one(name), mif))

        mif = self._current_node.add(F.Electrical(), name=name)
        return KeyOptMap.from_item(KeyOptItem.from_kv(TypeRef.from_one(name), mif))

    def _connect(
        self, a: L.ModuleInterface, b: L.ModuleInterface, ctx: ParserRuleContext | None
    ):
        """
        FIXME: In ato, we allowed duck-typing of connectables
        We need to reconcile this with the strong typing
        in faebryk's connect method
        For now, we'll attempt to connect by name, and log a deprecation
        warning if that succeeds, else, re-raise the exception emitted
        by the connect method
        """
        # If we're attempting to connect an Electrical to a SignalElectrical
        # (or ElectricLogic) then allow the connection, but issue a warning
        if pair := is_type_pair(a, b, F.Electrical, F.ElectricSignal):
            pair[0].connect(pair[1].line)

            # TODO: @v0.4 remove this deprecated import form
            with downgrade(errors.UserTypeError):
                a, b = pair
                a_type = a.__class__.__name__
                b_type = b.__class__.__name__
                raise errors.UserTypeError.from_ctx(
                    ctx,
                    f"Connected `{a}` (type {a_type}) to "
                    f"`{b}.line`, because `{b}` is an `{b_type}`. "
                    "This means that the reference isn't also connected through.",
                    traceback=self.get_traceback(),
                )

        else:
            try:
                # Try a proper connection
                a.connect(b)

            except NodeException as top_ex:
                top_ex = errors.UserNodeException.from_node_exception(
                    top_ex, ctx, self.get_traceback()
                )

                # If that fails, try connecting via duck-typing
                for name, (c_a, c_b) in a.zip_children_by_name_with(
                    b, L.ModuleInterface
                ).items():
                    if c_a is None:
                        if has_attr_or_property(a, name):
                            c_a = getattr(a, name)
                        else:
                            raise top_ex

                    if c_b is None:
                        if has_attr_or_property(b, name):
                            c_b = getattr(b, name)
                        else:
                            raise top_ex

                    try:
                        self._connect(c_a, c_b, None)
                    except NodeException:
                        raise top_ex

                else:
                    # If we connect everything via name (and tried in the first place)
                    # then we're good to go! We just need to tell everyone to probably
                    # not do that in the future - and we're off!
                    if (
                        ctx is not None
                    ):  # Check that this is the top-level _connect call
                        # TODO: @v0.4 increase the level of this to WARNING
                        # when there's an alternative
                        with downgrade(DeprecatedException, to_level=logging.DEBUG):
                            raise DeprecatedException.from_ctx(
                                ctx,
                                f"Connected `{a}` to `{b}` by duck-typing."
                                "They should be of the same type.",
                                traceback=self.get_traceback(),
                            )

    def visitConnect_stmt(self, ctx: ap.Connect_stmtContext):
        """Connect interfaces together"""
        connectables = [self.visitConnectable(c) for c in ctx.connectable()]
        for err_cltr, (a, b) in iter_through_errors(
            itertools.pairwise(connectables),
            errors._BaseBaseUserException,
            SkipPriorFailedException,
        ):
            with err_cltr():
                self._connect(a, b, ctx)

        return NOTHING

    def visitConnectable(self, ctx: ap.ConnectableContext) -> L.ModuleInterface:
        """Return the address of the connectable object."""
        if def_stmt := ctx.pindef_stmt() or ctx.signaldef_stmt():
            (_, mif), *_ = self.visit(def_stmt)
            return mif
        elif reference_ctx := ctx.field_reference():
            ref = self.visitFieldReference(reference_ctx)
            node = self._get_referenced_node(ref, ctx)
            if not isinstance(node, L.ModuleInterface):
                raise errors.UserTypeError.from_ctx(
                    ctx,
                    f"Can't connect `{node}` because it's not a `ModuleInterface`",
                    traceback=self.get_traceback(),
                )
            return node
        else:
            raise ValueError(f"Unhandled connectable type `{ctx}`")

    def visitRetype_stmt(self, ctx: ap.Retype_stmtContext):
        from_ref = self.visitFieldReference(ctx.field_reference())
        to_ref = self.visitTypeReference(ctx.type_reference())
        from_node = self._get_referenced_node(from_ref, ctx)

        # Only Modules can be specialized (since they're the only
        # ones with specialization gifs).
        # TODO: consider duck-typing this
        if not isinstance(from_node, L.Module):
            raise errors.UserTypeError.from_ctx(
                ctx,
                f"Can't specialize `{from_node}` because it's not a `Module`",
                traceback=self.get_traceback(),
            )

        # TODO: consider extending this w/ the ability to specialize to an instance
        class_ = self._get_referenced_class(ctx, to_ref)
        with self._traceback_stack.enter(ctx):
            with self._init_node(class_) as specialized_node:
                pass

        if not isinstance(specialized_node, L.Module):
            raise errors.UserTypeError.from_ctx(
                ctx,
                f"Can't specialize with `{specialized_node}`"
                " because it's not a `Module`",
                traceback=self.get_traceback(),
            )

        # FIXME: this is an abuse of disconnect_parent. The graph isn't intended to be
        # mutable like this, and it existed only for use in traits, however the
        # alternatives I could come up with were worse:
        # - an isinstance check to additionally run `get_most_special` on Modules +
        #   more processing down the line when we want the full name of the node
        # This is only be applied when specializing to a whole class, not an instance
        try:
            parent_deets = from_node.get_parent()
            # We use from_node.get_name() rather than from_ref[-1] because we can
            # ensure this reuses the exact name after any normalization
            from_node_name = from_node.get_name()
            assert parent_deets is not None, (
                "uhh not sure how you get here without trying to replace the root node,"
                " which you shouldn't ever have access to"
            )
            parent, _ = parent_deets
            from_node.parent.disconnect_parent()
            assert isinstance(parent, L.Module)

            # We have to make sure the from_node was part of the runtime attrs
            if not any(r is from_node for r in parent.runtime.values()):
                raise errors.UserNotImplementedError.from_ctx(
                    ctx,
                    "We cannot properly specialize nodes within the base definition of"
                    " a module. This limitation mostly applies to fabll modules today.",
                    traceback=self.get_traceback(),
                )

            # Now, slot that badboi back in right where it's less-special brother's spot
            del parent.runtime[from_node_name]
            parent.add(specialized_node, name=from_node_name)

            try:
                from_node.specialize(specialized_node)
            except* L.Module.InvalidSpecializationError as ex:
                raise errors.UserException.from_ctx(
                    ctx,
                    f"Can't specialize `{from_ref}` with `{to_ref}`:\n"
                    + "\n".join(f" - {e.message}" for e in ex.exceptions),
                    traceback=self.get_traceback(),
                ) from ex
        except Exception:
            # TODO: skip further errors about this node w/ self._record_failed_node()
            raise

        return NOTHING

    def visitBlockdef(self, ctx: ap.BlockdefContext):
        """Do nothing. Handled in Surveyor."""
        return NOTHING

    def visitImport_stmt(self, ctx: ap.Import_stmtContext):
        """Do nothing. Handled in Surveyor."""
        return NOTHING

    def visitDep_import_stmt(self, ctx: ap.Dep_import_stmtContext):
        """Do nothing. Handled in Surveyor."""
        return NOTHING

    def visitAssert_stmt(self, ctx: ap.Assert_stmtContext):
        comparisons = [c for _, c in self.visitComparison(ctx.comparison())]
        for cmp in comparisons:
            if isinstance(cmp, BoolSet):
                if not cmp:
                    raise errors.UserAssertionError.from_ctx(
                        ctx,
                        "Assertion failed",
                        traceback=self.get_traceback(),
                    )
            elif isinstance(cmp, ConstrainableExpression):
                cmp.constrain()
            else:
                raise ValueError(f"Unhandled comparison type {type(cmp)}")
        return NOTHING

    # Returns fab_param.ConstrainableExpression or BoolSet
    def visitComparison(
        self, ctx: ap.ComparisonContext
    ) -> KeyOptMap[ConstrainableExpression | BoolSet]:
        exprs = [
            self.visitArithmetic_expression(c)
            for c in [ctx.arithmetic_expression()]
            + [cop.getChild(0).arithmetic_expression() for cop in ctx.compare_op_pair()]
        ]
        op_strs = [
            cop.getChild(0).getChild(0).getText() for cop in ctx.compare_op_pair()
        ]

        predicates = []
        for (lh, rh), op_str in zip(itertools.pairwise(exprs), op_strs):
            match op_str:
                # @v0.4 upgrade to error
                case "<":
                    with downgrade(
                        errors.UserNotImplementedError, to_level=logging.WARNING
                    ):
                        raise errors.UserNotImplementedError(
                            "`<` is not supported. Use `<=` instead."
                        )
                    op = LessOrEqual
                case ">":
                    with downgrade(
                        errors.UserNotImplementedError, to_level=logging.WARNING
                    ):
                        raise errors.UserNotImplementedError(
                            "`>` is not supported. Use `>=` instead."
                        )
                    op = GreaterOrEqual
                case "<=":
                    op = LessOrEqual
                case ">=":
                    op = GreaterOrEqual
                case "within":
                    op = IsSubset
                case "is":
                    op = Is
                case _:
                    # We shouldn't be able to get here with parseable input
                    raise ValueError(f"Unhandled operator `{op_str}`")

            # TODO: should we be reducing here to a series of ANDs?
            predicates.append(op(lh, rh))

        return KeyOptMap([KeyOptItem.from_kv(None, p) for p in predicates])

    def visitArithmetic_expression(
        self, ctx: ap.Arithmetic_expressionContext
    ) -> Numeric:
        if ctx.OR_OP() or ctx.AND_OP():
            raise errors.UserTypeError.from_ctx(
                ctx,
                "Logical operations are not supported",
                traceback=self.get_traceback(),
            )
            lh = self.visitArithmetic_expression(ctx.arithmetic_expression())
            rh = self.visitSum(ctx.sum_())

            if ctx.OR_OP():
                return operator.or_(lh, rh)
            else:
                return operator.and_(lh, rh)

        return self.visitSum(ctx.sum_())

    def visitSum(self, ctx: ap.SumContext) -> Numeric:
        if ctx.ADD() or ctx.MINUS():
            lh = self.visitSum(ctx.sum_())
            rh = self.visitTerm(ctx.term())

            if ctx.ADD():
                return operator.add(lh, rh)
            else:
                return operator.sub(lh, rh)

        return self.visitTerm(ctx.term())

    def visitTerm(self, ctx: ap.TermContext) -> Numeric:
        if ctx.STAR() or ctx.DIV():
            lh = self.visitTerm(ctx.term())
            rh = self.visitPower(ctx.power())

            if ctx.STAR():
                return operator.mul(lh, rh)
            else:
                return operator.truediv(lh, rh)

        return self.visitPower(ctx.power())

    def visitPower(self, ctx: ap.PowerContext) -> Numeric:
        if ctx.POWER():
            base, exp = map(self.visitFunctional, ctx.functional())
            return operator.pow(base, exp)
        else:
            return self.visitFunctional(ctx.functional(0))

    def visitFunctional(self, ctx: ap.FunctionalContext) -> Numeric:
        if ctx.name():
            name = self.visitName(ctx.name())
            operands = [self.visitBound(b) for b in ctx.bound()]
            if name == "min":
                return Min(*operands)
            elif name == "max":
                return Max(*operands)
            else:
                raise errors.UserNotImplementedError.from_ctx(
                    ctx, f"Unknown function `{name}`"
                )
        else:
            return self.visitBound(ctx.bound(0))

    def visitBound(self, ctx: ap.BoundContext) -> Numeric:
        return self.visitAtom(ctx.atom())

    def visitAtom(self, ctx: ap.AtomContext) -> Numeric:
        if ctx.field_reference():
            ref = self.visitFieldReference(ctx.field_reference())
            target = self._get_referenced_node(ref.stem, ctx)
            return self._get_param(target, ref.last, ctx)

        elif ctx.literal_physical():
            return self.visitLiteral_physical(ctx.literal_physical())

        elif group_ctx := ctx.arithmetic_group():
            assert isinstance(group_ctx, ap.Arithmetic_groupContext)
            return self.visitArithmetic_expression(group_ctx.arithmetic_expression())

        raise ValueError(f"Unhandled atom type `{ctx}`")

    def _get_unit_from_ctx(self, ctx: ParserRuleContext) -> UnitType:
        """Return a pint unit from a context."""
        unit_str = ctx.getText()
        try:
            return P.Unit(unit_str)
        except UndefinedUnitError as ex:
            raise errors.UserUnknownUnitError.from_ctx(
                ctx,
                f"Unknown unit `{unit_str}`",
                traceback=self.get_traceback(),
            ) from ex

    def visitLiteral_physical(
        self, ctx: ap.Literal_physicalContext
    ) -> Quantity_Interval:
        """Yield a physical value from a physical context."""
        if ctx.quantity():
            qty = self.visitQuantity(ctx.quantity())
            value = Single(qty)
        elif ctx.bilateral_quantity():
            value = self.visitBilateral_quantity(ctx.bilateral_quantity())
        elif ctx.bound_quantity():
            value = self.visitBound_quantity(ctx.bound_quantity())
        else:
            # this should be protected because it shouldn't be parseable
            raise ValueError
        return value

    def visitQuantity(self, ctx: ap.QuantityContext) -> Quantity:
        """Yield a physical value from an implicit quantity context."""
        raw: str = ctx.NUMBER().getText()
        if raw.startswith("0x"):
            value = int(raw, 16)
        else:
            value = float(raw)

        # Ignore the positive unary operator
        if ctx.MINUS():
            value = -value

        if unit_ctx := ctx.name():
            unit = self._get_unit_from_ctx(unit_ctx)
        else:
            unit = dimensionless

        return Quantity(value, unit)  # type: ignore

    def visitBilateral_quantity(
        self, ctx: ap.Bilateral_quantityContext
    ) -> Quantity_Interval:
        """Yield a physical value from a bilateral quantity context."""
        nominal_qty = self.visitQuantity(ctx.quantity())

        tol_ctx: ap.Bilateral_toleranceContext = ctx.bilateral_tolerance()
        tol_num = float(tol_ctx.NUMBER().getText())

        # Handle proportional tolerances
        if tol_ctx.PERCENT():
            tol_divider = 100
        elif tol_ctx.name() and tol_ctx.name().getText() == "ppm":
            tol_divider = 1e6
        else:
            tol_divider = None

        if tol_divider:
            if nominal_qty == 0:
                raise errors.UserException.from_ctx(
                    tol_ctx,
                    "Can't calculate tolerance percentage of a nominal value of zero",
                    traceback=self.get_traceback(),
                )

            # Calculate tolerance value from percentage/ppm
            tol_value = tol_num / tol_divider
            return Range.from_center_rel(nominal_qty, tol_value)

        # Ensure the tolerance has a unit
        if tol_name := tol_ctx.name():
            # In this case there's a named unit on the tolerance itself
            tol_qty = tol_num * self._get_unit_from_ctx(tol_name)
        elif nominal_qty.unitless:
            tol_qty = tol_num * dimensionless
        else:
            tol_qty = tol_num * nominal_qty.units

        # Ensure units on the nominal quantity
        if nominal_qty.unitless:
            nominal_qty = nominal_qty * HasUnit.get_units(tol_qty)

        # If the nominal has a unit, then we rely on the ranged value's unit compatibility # noqa: E501  # pre-existing
        if not nominal_qty.is_compatible_with(tol_qty):
            raise errors.UserTypeError.from_ctx(
                tol_name,
                f"Tolerance unit ({HasUnit.get_units(tol_qty)}) is not dimensionally"
                f" compatible with nominal unit ({nominal_qty.units})",
                traceback=self.get_traceback(),
            )

        return Range.from_center(nominal_qty, tol_qty)

    def visitBound_quantity(self, ctx: ap.Bound_quantityContext) -> Quantity_Interval:
        """Yield a physical value from a bound quantity context."""

        start, end = map(self.visitQuantity, ctx.quantity())

        # If only one of them has a unit, take the unit from the one which does
        if start.unitless and not end.unitless:
            start = start * end.units
        elif not start.unitless and end.unitless:
            end = end * start.units

        elif not start.is_compatible_with(end):
            # If they've both got units, let the RangedValue handle
            # the dimensional compatibility
            raise errors.UserTypeError.from_ctx(
                ctx,
                f"Tolerance unit ({end.units}) is not dimensionally"
                f" compatible with nominal unit ({start.units})",
                traceback=self.get_traceback(),
            )

        return Range(start, end)

    def visitCum_assign_stmt(self, ctx: ap.Cum_assign_stmtContext | Any):
        """
        Cumulative assignments can only be made on top of
        nothing (implicitly declared) or declared, but undefined values.

        Unlike assignments, they may not implicitly declare an attribute.
        """
        ref_dec = ctx.field_reference_or_declaration()
        assignee_ref = self.visitFieldReference(ref_dec.field_reference())
        target = self._get_referenced_node(assignee_ref, ctx)
        self.visitDeclaration_stmt(ref_dec.declaration_stmt())

        assignee = self._get_param(target, assignee_ref.last, ctx)
        value = self.visitCum_assignable(ctx.cum_assignable())

        # HACK: we have no way to check by what operator
        # the param is dynamically resolved
        # For now we assume any dynamic trait is sufficient
        if ctx.cum_operator().ADD_ASSIGN():
            assignee.alias_is(value)
        elif ctx.cum_operator().SUB_ASSIGN():
            assignee.alias_is(-value)
        else:
            # Syntax should protect from this
            raise ValueError(f"Unhandled set assignment operator {ctx}")

        # TODO: @v0.4 increase the level of this to WARNING
        # when there's an alternative
        with downgrade(DeprecatedException, to_level=logging.DEBUG):
            raise DeprecatedException(f"{ctx.cum_operator().getText()} is deprecated.")
        return NOTHING

    def visitSet_assign_stmt(self, ctx: ap.Set_assign_stmtContext):
        """
        Set cumulative assignments can only be made on top of
        nothing (implicitly declared) or declared, but undefined values.

        Unlike assignments, they may not implicitly declare an attribute.
        """
        ref_dec = ctx.field_reference_or_declaration()
        assignee_ref = self.visitFieldReference(ref_dec.field_reference())
        target = self._get_referenced_node(assignee_ref, ctx)
        self.visitDeclaration_stmt(ref_dec.declaration_stmt())

        assignee = self._get_param(target, assignee_ref.last, ctx)
        value = self.visitCum_assignable(ctx.cum_assignable())

        if ctx.OR_ASSIGN():
            assignee.constrain_superset(value)
        elif ctx.AND_ASSIGN():
            assignee.constrain_subset(value)
        else:
            # Syntax should protect from this
            raise ValueError(f"Unhandled set assignment operator {ctx}")

        # TODO: @v0.4 remove this deprecated import form
        with downgrade(DeprecatedException):
            lhs = ref_dec.field_reference().getText()
            rhs = ctx.cum_assignable().getText()
            if ctx.OR_ASSIGN():
                subset = lhs
                superset = rhs
            else:
                subset = rhs
                superset = lhs
            raise DeprecatedException(
                f"Set assignment of `{assignee}` is deprecated."
                f' Use "assert `{subset}` within `{superset}` "instead.'
            )
        return NOTHING

    def _try_get_unit_from_type_info(
        self, ctx: ap.Type_infoContext | None
    ) -> UnitType | None:
        if ctx is None:
            return None
        unit_ctx: ap.UnitContext = ctx.unit()
        if unit_ctx is None:
            return None
        # TODO: @v0.4.0: remove this shim
        unit = unit_ctx.getText()
        if unit in _declaration_domain_to_unit:
            unit = _declaration_domain_to_unit[unit]
            # TODO: consider deprecating this
        else:
            unit = self._get_unit_from_ctx(unit_ctx)

        return unit

    def _handleParameterDeclaration(
        self, ref: TypeRef, unit: UnitType, ctx: ParserRuleContext
    ):
        assert unit is not None, "Type info should be enforced by the parser"
        name = FieldRef.from_type_ref(ref).last
        param = self._ensure_param(self._current_node, name, unit, ctx)
        if param in self._param_assignments:
            declaration_after_definition = any(
                assignment.is_definition
                for assignment in self._param_assignments[param]
            )
            # TODO: @v0.4 remove this deprecated import form
            with downgrade(errors.UserKeyError):
                if declaration_after_definition:
                    raise errors.UserKeyError.from_ctx(
                        ctx,
                        f"Ignoring declaration of `{name}` "
                        "because it's already defined",
                        traceback=self.get_traceback(),
                    )
                else:
                    raise errors.UserKeyError.from_ctx(
                        ctx,
                        f"Ignoring redeclaration of `{name}`",
                        traceback=self.get_traceback(),
                    )
        else:
            self._param_assignments[param].append(
                _ParameterDefinition(
                    ref=FieldRef.from_type_ref(ref),
                    ctx=ctx,
                    traceback=self.get_traceback(),
                )
            )

    def visitDeclaration_stmt(self, ctx: ap.Declaration_stmtContext | None):
        """Handle declaration statements."""
        if ctx is None:
            return NOTHING
        ref = self.visitFieldReference(ctx.field_reference())
        if len(ref) > 1:
            raise errors.UserSyntaxError.from_ctx(
                ctx,
                f"Can't declare fields in a nested object `{ref}`",
                traceback=self.get_traceback(),
            )
        type_ref = ref.to_type_ref()
        if type_ref is None:
            raise errors.UserSyntaxError.from_ctx(
                ctx,
                f"Can't declare keyed attributes `{ref}`",
                traceback=self.get_traceback(),
            )

        # check declaration type
        unit = self._try_get_unit_from_type_info(ctx.type_info())
        if unit is not None:
            self._handleParameterDeclaration(type_ref, unit, ctx)
            return NOTHING

        assert False, "Only parameter declarations supported"

    def visitPass_stmt(self, ctx: ap.Pass_stmtContext):
        return NOTHING


bob = Bob()
