# tests/test_cluster.py

import pytest
import sys
import os
from pyedgeconnect import Orchestrator
from dotenv import load_dotenv
load_dotenv()

# Get env variables. If they do not exist, exit program.
try:
    orch_url = os.getenv('ORCH_URL')
    orch_api_key = os.getenv('ORCH_API_KEY')

    if not all([orch_url, orch_api_key]):
        raise ValueError(
            "One or more required environment variables are not set.")
except ValueError as e:
    print(f"Error: {e}")
    sys.exit(1)


orch = Orchestrator(orch_url, api_key=orch_api_key, verify_ssl=False)
print(orch.orch_version)


def test_get_cluster_state_basic():
    # Define a test case
    # Replace this with actual test logic depending on what get_cluster_state does
    # and what inputs it expects
    try:
        result = orch.get_cluster_state("Fannett", False)
        assert isinstance(result, dict)  # Example: Assert it returns a dictionary
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")
#

