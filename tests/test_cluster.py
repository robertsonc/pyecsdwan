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

@pytest.fixture(scope="module", autouse=True)
def check_orch_version():
    version = orch.orch_version
    if orch.orch_version < 9.5:
        pytest.skip("Skipping tests for Orchestrator versions < 9.5.0")
    else:
        print(f"Orchestrator version satisfies requirements for this module: {version}")
        yield


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

@pytest.fixture
def setup_test_add_cluster_profile():
    # determine if test cluster profiles exist; if so, delete them before trying to add
    global profile_id
    profiles = orch.get_all_cluster_profiles()
    for profile in profiles:
        if profile['name'] == 'ClusterProfileTest100' or profile['name'] == 'ClusterProfileTest101':
            profile_id = profile['id']
            # delete_cluster_profile is tested further down - if that fails, this will fail
            delete = orch.delete_cluster_profile(profile_id)
            assert delete == True
    yield

@pytest.mark.usefixtures("setup_test_add_cluster_profile")
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
        assert result['success'] == True
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
        'name': 'ClusterProfileTest100',
        'interfaceLabel': 'lan0',
        'flowRedirection': 'Secure',
        'waitTime': 55,
        'userSessionSync': 'Secure'
    }
    try:
        orch.update_cluster_profile(update_existing_profile)
        all_cluster_profiles = orch.get_all_cluster_profiles()
        for profile in all_cluster_profiles:
            if profile['id'] == profile_id:
                assert profile['waitTime'] == 55
                break
        # assert result['success'] == True
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")

# do not run standalone - previous tests are needed to get profile_id
def test_delete_cluster_profile():
    try:
        orch.delete_cluster_profile(profile_id)
        all_cluster_profiles = orch.get_all_cluster_profiles()
        for profile in all_cluster_profiles:
            if profile['id'] == profile_id:
                pytest.fail("Test failed: Cluster profile was not deleted")
                break
            else:
                assert True
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")

def test_get_all_cluster_profile_mappings():
    try:
        result = orch.get_all_cluster_profile_mappings()
        assert isinstance(result, dict)
    except Exception as e:
        pytest.fail(f"Test failed with exception: {e}")





