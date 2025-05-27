# tests/test_cluster.py

import pytest
import sys
import os
from pyedgeconnect import Orchestrator
from dotenv import load_dotenv
load_dotenv()

profile_id = None

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


def test_get_cluster_state():
    try:
        result = orch.get_cluster_state("Fannett", False)
        assert isinstance(result, dict)  # Example: Assert it returns a dictionary
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")

def test_get_cluster_alarm_count():
    try:
        result = orch.get_cluster_alarm_count()
        assert isinstance(result, dict)
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")

def test_get_all_cluster_profiles():
    try:
        result = orch.get_all_cluster_profiles()
        assert isinstance(result, list)
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")

def test_add_cluster_profile():
    new_test_cluster_profiles = \
    [
        {
            'name': 'ClusterProfileTest100',
            'interfaceLabel': 'lan0',
            'flowRedirection': 'Secure',
            'waitTime': 50,
            'userSessionSync': 'Secure'
        },
        {
            'name': 'ClusterProfileTest101',
            'interfaceLabel': 'lan0',
            'flowRedirection': 'Secure',
            'waitTime': 50,
            'userSessionSync': 'Secure'
        }
    ]
    try:
        result = orch.add_cluster_profiles(new_test_cluster_profiles)
        #TODO: need to test for right return code here
        print(result)
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")

@pytest.fixture
def setup_test_update_cluster_profile():
    global profile_id
    profiles = orch.get_all_cluster_profiles()
    for profile in profiles:
        if profile['name'] == 'ClusterProfileTest100':
            profile_id = profile['id']
            break
    yield

@pytest.mark.usefixtures("setup_test_update_cluster_profile")
def test_update_cluster_profile():
    update_existing_profile =\
    {
        'id': profile_id,
        'name': 'ClusterProfileTestUpdated100',
        'interfaceLabel': 'lan0',
        'flowRedirection': 'Secure',
        'waitTime': 55,
        'userSessionSync': 'Secure'
    }
    # orch.update_cluster_profile(update_existing_profile)
    try:
        result = orch.update_cluster_profile(update_existing_profile)
        print(result)
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")


