import dataclasses
import itertools
from dataclasses import dataclass, replace
from typing import Callable, Dict, Iterable, List, Literal, Optional, Set, Tuple, Union

import numpy
import onnx
import onnx.shape_inference

from . import _build, attr
from ._adapt import adapt_best_effort
from .arrow import Arrow
from .arrowfields import NoArrows
from .internal_op import Argument, _Initializer
from .node import Node
from .schemas import max_opset_policy
from .type_system import Tensor, Type


def arguments_dict(**kwargs: Optional[Union[Type, numpy.ndarray]]) -> Dict[str, Arrow]:
    """
    Parameters
    ----------
    kwargs
        Types or arrays for the newly created arguments.
        Keyword argument names are meaningful and used to name the arguments of the final graph.
        A numpy array is interpreted as an initializer (default argument value),
        and its type is used to create a respective Tensor.
    Returns
    -------
    Dict[str, Arrow]
        Argument Arrows of given Types, named the same as kwargs.
    """
    result = {}
    for name, info in kwargs.items():
        if info is None or isinstance(info, Type):
            result[name] = Argument(
                Argument.Attributes(name=name, type=info, default=None), NoArrows()
            ).outputs.arg
        elif isinstance(info, numpy.ndarray):
            result[name] = Argument(
                Argument.Attributes(
                    name=name, type=Tensor.like_array(info), default=info
                ),
                NoArrows(),
            ).outputs.arg
        else:
            raise TypeError(f"Cannot construct argument from {type(info)}.")
    return result


def arguments(**kwargs: Optional[Union[Type, numpy.ndarray]]) -> Tuple[Arrow, ...]:
    """This function is a shorthand for a respective call to ``arguments_dict``, unpacking the Arrows from the dict."""
    return tuple(arguments_dict(**kwargs).values())


def enum_arguments(
    *infos: Union[Type, numpy.array], prefix: str = "in"
) -> Tuple[Arrow, ...]:
    """
    Convenience function for creating an enumeration of arguments, prefixed with ``prefix``.
    Calls ``arguments`` internally.

    This is a function useful for creating subgraphs, where the exact names don't really matter, only their order.
    Note that repeated use of this in the same graph may repeat names if the prefix is also the same.

    Parameters
    ----------
    infos
        Types/initializers for the created arguments.
    prefix
        String to prefix the names of created arguments with.
    Returns
    -------
    Tuple[Arrow, ...]
        Argument Arrows as specified, in the same order as information ``infos``.
    """
    return arguments(**{f"{prefix}{i}": info for i, info in enumerate(infos)})


def initializer(arr: numpy.ndarray) -> Arrow:
    """
    Create a single initializer (frozen argument) with a given array value.

    This is an alternate method to creating a constant from using a dedicated Constant constructor.
    As a convention, initializers may be used for more global-scope constants.

    Parameters
    ----------
    arr
        Value of the initializer.
    Returns
    -------
        Arrow which is always equal to the respective value provided by `arr`.
    """
    return _Initializer(
        _Initializer.Attributes(type=Tensor.like_array(arr), default=arr),
        NoArrows(),
    ).outputs.arg


@dataclass(frozen=True, eq=False)
class Graph:
    """
    Represents an abstraction for a wrapped up ONNX computation graph,
    that can be built into ONNX GraphProto & ModelProto.

    Should be constructed only with the ``results`` functions.

    Use the methods ``rename``, ``doc``, ``with_arguments`` to set additional data for the graph.
    These methods return a new instance of Graph with a respective private attribute set.

    Note that to not only fix results (which a Graph is constructed with), but also arguments, ``with_arguments``
    should be used.

    Note: building a Graph is cached, so changing it in-place without the setters will invalidate the build.
    """

    _results: Dict[str, Arrow]
    _name: Optional[str] = None
    _doc_string: Optional[str] = None
    _arguments: Optional[Tuple[Arrow, ...]] = None
    _extra_opset_req: Optional[Set[Tuple[str, int]]] = None
    _constructor: Optional[Callable[..., Iterable[Arrow]]] = None
    _build_result: "_build.Cached[_build.BuildResult]" = dataclasses.field(
        default_factory=_build.Cached
    )

    def __repr__(self):
        name_repr = self._name if self._name is not None else "?"
        args_repr = (
            f"{', '.join(str(a) for a in self._arguments)}"
            if self._arguments is not None
            else "..."
        )
        res_repr = f"{', '.join(f'{k}: {a}' for k, a in self._results.items())}"
        comments: List[str] = []
        if self._doc_string is not None:
            comments.append(f'"{self._doc_string[:10]}..."')
        if self._extra_opset_req is not None:
            comments.append(f"+{len(self._extra_opset_req)} opset req")
        return f"<Graph '{name_repr}' ({args_repr}) -> ({res_repr}){': ' if comments else ''}{', '.join(comments)}>"

    def __post_init__(self):
        if any(not isinstance(arrow, Arrow) for arrow in self._results.values()):
            raise TypeError(
                f"Graph results must be Arrows, not {set(type(obj) for obj in self._results.values()) - {Arrow}}."
            )
        if self._arguments is not None and any(
            not isinstance(arrow, Arrow) for arrow in self._arguments
        ):
            raise TypeError(
                f"Graph results must be Arrows, not {set(type(obj) for obj in self._arguments) - {Arrow}}."
            )

    def with_name(self, name: str) -> "Graph":
        """Return a Graph with its name set to ``name``."""
        return replace(self, _name=name)

    def with_doc(self, doc_string: str) -> "Graph":
        """Return a Graph with its doc string set to ``doc``."""
        return replace(self, _doc_string=doc_string)

    def with_arguments(self, *args: Arrow) -> "Graph":
        """
        Return a Graph with given Arrows marked as exactly its arguments.
        A useful idiom is ``results(...).with_arguments(...)`` when you want to specify both results and arguments.
        """
        return replace(self, _arguments=args)

    def with_opset(self, *args: Tuple[str, int]) -> "Graph":
        """
        Add the given minimum opset requirements to the graph.
        Useful when the graph is using legacy nodes, but Steelix should attempt to convert them to a required version.
        """
        extra_opset_req = set(args)
        if self._extra_opset_req is not None:
            extra_opset_req |= self._extra_opset_req
        return replace(self, _extra_opset_req=extra_opset_req)

    def _with_constructor(self, fun: Callable[..., Iterable[Arrow]]) -> "Graph":
        """Assign a constructor that constructed this Graph given ``self.requested_arguments``."""
        return replace(self, _constructor=fun)

    def _reconstruct(self, *args: Arrow) -> "Graph":
        assert self._constructor is not None
        return (
            results(**dict(zip(self._results, self._constructor(*args))))
            .with_arguments(*args)
            ._with_constructor(self._constructor)
        )

    def _inject_build_result(self, what: "_build.BuildResult") -> "Graph":
        """
        Internal function used to build a Graph with a custom build result.
        Used when building subgraphs to have further control over the build state.
        """
        return replace(self, _build_result=_build.Cached(what))

    @property
    def requested_arguments(self) -> Optional[Iterable[Arrow]]:
        """Arguments requested by this Graph (for building) - ``None`` if unspecified."""
        return self._arguments

    @property
    def requested_results(self) -> Dict[str, Arrow]:
        """Results (named) requested by this Graph (for building)."""
        return self._results

    def get_arguments(self) -> Dict[str, Arrow]:
        """
        Get the effective named arguments (after build) of this Graph.

        May be expensive, as it has to build Use ``requested_arguments`` for a cheaper variant that may be sufficient.
        """
        return {
            self._get_build_result().scope.arrow[arrow]: arrow
            for arrow in self._get_build_result().arguments
        }

    def get_results(self) -> Dict[str, Arrow]:
        """
        Get the effective named results (after build) of this Graph.

        May be expensive, as it has to build. Use ``requested_results`` for a cheaper variant that may be sufficient.
        """
        return {
            self._get_build_result().scope.arrow[arrow]: arrow
            for arrow in self._get_build_result().results
        }

    def get_opsets(self) -> Dict[str, int]:
        """
        Get the effective opsets used by this Graph. The used policy for mixed versions is maximum-requested.

        May be expensive, as it has to build.
        """
        return max_opset_policy(self._get_opset_req())

    def _get_build_result(self) -> "_build.BuildResult":
        """Internal function for getting (with cache) the build result structure for this Graph."""
        if self._build_result._value is None:
            self._build_result.value = _build.Builder(self).build_main()
        return self._build_result.value

    def _get_opset_req(self) -> Set[Tuple[str, int]]:
        """Internal function for accessing the opset requirements, including extras requested by the Graph itself."""
        return self._get_build_result().opset_req | (
            self._extra_opset_req if self._extra_opset_req is not None else set()
        )

    def _get_initializers_by_name(self) -> Dict[str, numpy.ndarray]:
        """Internal function for accessing the initializers by name in the build."""
        return {
            self._get_build_result().scope.arrow[arrow]: init
            for arrow, init in self._get_build_result().initializers.items()
        }

    def get_adapted_nodes(self) -> Dict[Node, Tuple[onnx.NodeProto, ...]]:
        """
        Do a best-effort at generating NodeProtos of consistent versions, matching ``self.opsets``.
        In essence, the policy is to upgrade to the highest used version.
        This does not attempt to fix too complicated nodes, but should work for embedded models and simple single nodes.

        Note that onnx.version_converter only implements conversion for the default domain.
        """
        nodes = self._get_build_result().nodes
        consistent_nodes = nodes.copy()
        for node, protos in nodes.items():
            best_effort = adapt_best_effort(
                node,
                list(protos),
                self.get_opsets(),
                self._get_build_result().scope.arrow.name_of,
                self._get_build_result().scope.node.name_of,
            )
            consistent_nodes[node] = (
                tuple(best_effort) if best_effort is not None else protos
            )

        return consistent_nodes

    def to_onnx(self, *, concrete: bool = False) -> onnx.GraphProto:
        """
        This function performs the Steelix build process, gathering arguments, results, nodes and other information.

        - Saves type information for arguments & results.
        - Sets the name of the graph, with defaults if it is not set or if it is a subgraphs
        - Saves initializers.
        - Sets the docstring if one is set.
        Returns
        -------
        onnx.GraphProto
            Translation of this Graph into an ONNX GraphProto object.
        """
        if not self.get_results():
            raise ValueError("Attempt to build graph without results.")

        argument_info = [
            arrow.unwrap_type().to_onnx_value_info(
                name, concrete=concrete, _traceback_name=f"argument {name} ({arrow})"
            )
            for name, arrow in self.get_arguments().items()
        ]
        result_info = [
            arrow.unwrap_type().to_onnx_value_info(
                name, concrete=concrete, _traceback_name=f"result {name} ({arrow})"
            )
            for name, arrow in self.get_results().items()
        ]

        if self._name:
            name = self._name
        else:
            name = "steelix_graph"

        initializer_tensors = [
            attr.from_array(arr, name)
            for name, arr in self._get_initializers_by_name().items()
        ]

        node_protos = itertools.chain.from_iterable(self.get_adapted_nodes().values())
        return onnx.helper.make_graph(
            list(node_protos),
            name,
            argument_info,
            result_info,
            initializer_tensors,
            self._doc_string,
        )

    def to_onnx_model(
        self,
        producer_name: str = "steelix",
        model_doc_string: str = "",
        infer_shapes: bool = False,
        check_model: Union[Literal[0], Literal[1], Literal[2]] = 1,
        *,
        concrete: bool = True,
    ) -> onnx.ModelProto:
        """
        Internally, this function first obtains a GraphProto from ``.to_onnx()``. Additionally:

        - Function definitions are collected and built into FunctionProtos.
        - Opset requirements are collected (consistency policy is to use the highest version).
            ONNX only allows one version of each domain per model, so some attempt at conversion of nodes is made.
        - Checks are performed, at the level described by the respective arguments.

        Parameters
        ----------
        producer_name
            Value of the ONNX ModelProto producer name field.
        model_doc_string
            Doc string for the ONNX ModelProto.
        infer_shapes
            If the value is True, the model is passed through `onnx.shape_inference.infer_shapes`.
        check_model
            If the value is at least 1 (default), `onnx.checker.check_model` is executed on the model.
            If it is 2, `full_check` of the `check_model` call is set to `True` (e.g. tests against shape inference).
        concrete
            Whether to raise for non-concrete value infos (like missing shape information).
        Returns
        -------
            Translation of this Graph into an ONNX ModelProto object.
        """
        function_protos: Dict[Tuple[str, str], onnx.FunctionProto] = {}
        for fun in self._get_build_result().functions:
            proto = fun.to_onnx_function()
            if proto is None:
                continue
            key = (proto.domain, proto.name)
            if key in function_protos and proto != function_protos[key]:
                raise RuntimeError(
                    f"Built dependency function {proto.domain}:{proto.name} has two different definitions. "
                    f"Was its implementation non-deterministic or is there a naming collision?"
                )
            function_protos[key] = proto

        if not self.get_opsets():
            raise RuntimeError(
                "ONNX often does not properly handle graphs which are empty, "
                "and this one seems to contain no opset imports (only internal nodes?). "
                "Consider adding an Identity operator if you are just copying arguments."
            )

        model = onnx.helper.make_model(
            self.to_onnx(concrete=concrete),
            producer_name=producer_name,
            doc_string=model_doc_string,
            functions=list(function_protos.values()),
            opset_imports=[
                onnx.helper.make_operatorsetid(domain, version)
                for domain, version in self.get_opsets().items()
            ],
        )

        if infer_shapes:
            model = onnx.shape_inference.infer_shapes(model)
        if check_model:
            onnx.checker.check_model(model, full_check=check_model >= 2)
        return model


def results(**kwargs: Arrow) -> Graph:
    """
    Use this function to construct a ``Graph`` object.

    Parameters
    ----------
    kwargs
        Arrows to be marked as results in the created Graph.
    Returns
    -------
    Graph
        Graph with the results given in `kwargs`, in the same order. Keys are used as names for the results.
    """
    return Graph(kwargs)


def enum_results(*arrows: Arrow, prefix="out") -> Graph:
    """
    Use this function to construct a ``Graph`` object, whenever the exact names are not important.
    Useful when creating subgraphs.

    Parameters
    ----------
    arrows
        Arrows to be marked as results.
    prefix
        String to prefix the names of created results with.
    Returns
    -------
        Graph with the results given in `arrows`, in the same order.
        Names are the `prefix` with an enumeration index at the end.
    """
    return results(**{f"{prefix}{i}": arrow for i, arrow in enumerate(arrows)})


def subgraph(types: Iterable[Type], fun: Callable[..., Iterable[Arrow]]) -> Graph:
    """
    Convenience function for creating a subgraph, for use in an operator like If or Loop.
    However, for those operators one may prefer to use alternative constructors like ``xif`` or ``xloop``
    (which use this function internally).

    Parameters
    ----------
    types
        A list of argument types for the subgraph.
    fun
        A function taking as many Arrow arguments as the length of `types`, and returning the results of the subgraph.
    Returns
    -------
    Graph
        Graph with results based on the return value of `fun`.
    """
    ins = enum_arguments(*types)
    for arrow in ins:
        arrow._rename(None)
    outs = fun(*ins)
    return enum_results(*outs).with_arguments(*ins)._with_constructor(fun)
