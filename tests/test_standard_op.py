import numpy
import pytest

from steelix._graph import arguments
from steelix._type_inference import InferenceError
from steelix._type_system import Tensor


def test_basic_inference(op):
    a, b = arguments(a=Tensor(numpy.float32, ("N",)), b=Tensor(numpy.float32, (2, "N")))
    assert op.add(a, b).type == Tensor(numpy.float32, (2, "N"))


def test_variadic_input_inference(op):
    typ = Tensor(numpy.float32, ("N",))
    a, b, c = arguments(a=typ, b=typ, c=typ)
    assert op.maximum([a, b, c]).type == typ


def test_variadic_output_inference(op):
    (x,) = arguments(x=Tensor(numpy.float32, (3, "N")))
    x1, x2, x3 = op.split(x, op.const([1, 1, 1]), outputs_count=3)
    assert x1.type == x2.type == x3.type == Tensor(numpy.float32, (1, "N"))


def test_optional_input_inference(op):
    (x,) = arguments(x=Tensor(numpy.float32, ("N",)))
    assert op.clip(x, max=op.const(1.0)).type == Tensor(numpy.float32, ("N",))
    assert op.clip(x, min=op.const(0.0)).type == Tensor(numpy.float32, ("N",))
    assert op.clip(x, min=op.const(0.0), max=op.const(1.0)).type == Tensor(
        numpy.float32, ("N",)
    )


def test_function_body_inference(op):
    a, b = arguments(a=Tensor(numpy.float32, ("N",)), b=Tensor(numpy.float32, ("N",)))
    assert op.greater_or_equal(a, b).type == Tensor(numpy.bool_, ("N",))


def test_inference_fails(op):
    a, b = arguments(a=Tensor(numpy.float32, (2,)), b=Tensor(numpy.float32, (3,)))
    with pytest.raises(InferenceError):
        op.add(a, b)


def test_inference_validation_fails(op):
    a, b = arguments(a=Tensor(numpy.float32, (2,)), b=Tensor(numpy.float64, (2,)))
    with pytest.raises(InferenceError):
        op.add(a, b)
