"""Generated pydantic models, one module per spec operation.

Module names are the operation slug (``<scope>_<method>_<path>``); see
``tools/gen_models.py``. Nothing is re-exported here on purpose -- an
``__init__`` that imported every model would have to be regenerated every
time one more operation is added, and would drag the whole package into
memory for a single import.
"""
