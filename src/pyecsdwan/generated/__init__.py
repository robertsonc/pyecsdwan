"""Machine-generated pydantic models and typed client bindings (issue #26).

Everything under ``models/`` and ``bindings/`` is emitted by
``tools/gen_models.py`` from the vendored OpenAPI baselines in ``specs/``.
Do not hand-edit those modules: regenerate them instead. The one hand-written
module here is :mod:`pyecsdwan.generated._base`, which holds the shared
pydantic configuration and the runtime helpers the bindings call, so a policy
change (say, how a request body is serialized) is a one-line edit here rather
than a re-run over every generated file.

Generation is on demand, not bulk: the baselines carry 1833 operations and
this package only holds the ones a resource module or a Tier-1 plugin
actually needs. ``python tools/gen_models.py --help`` shows how to add one.
"""
