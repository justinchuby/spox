"""Small module implementing simple global-scope converter logic. Useful for implementing e.g. sklearn converters."""

from typing import Any, Dict

# Keys are objects or single-parameter callables for object and type conversion, respectively
converters: Dict[Any, Any] = {}


def convert(what):
    """
    Implements a simple conversion protocol. Given an object, attempts:
    1. Object conversion. If the object is in the ``converters`` dictionary, the associated value is returned.
    2. Type conversion. If the type of the object is in the ``converters`` dictionary, the associated value
        is called with ``what`` as the only parameter (the value decorates ``what``).
    Otherwise, ``KeyError`` is raised.
    """
    if what in converters:  # Object conversion
        return converters[what]
    elif type(what) in converters:  # Type conversion
        return converters[type(what)](what)
    raise KeyError(
        f"No object or type converter for {what}. Did you forget to add one to the converters dictionary?"
    )
