"""Bundled fake Orchestrator for demos and e2e tests.

A small FastAPI application that mimics the subset of the HPE Aruba
EdgeConnect SD-WAN Orchestrator REST API (9.3+) that pyecsdwan uses:
appliance inventory, interface labels, template groups and associations,
overlays and overlay associations, the action-status poll loop, and
header/session authentication. All routes live under ``/gms/rest`` so an
``OrchClient`` pointed at the mock's base URL behaves exactly as it does
against a live Orchestrator.

Run standalone with ``python -m pyecsdwan.mock`` or embed in tests via
:func:`pyecsdwan.mock.server.run_in_thread`.
"""
