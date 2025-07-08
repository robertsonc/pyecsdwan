from DeploymentInfo import DeploymentInfo
from ApplianceInfo import ApplianceInfo

def main():
    #TODO: create class that aggregates all these classes so only one call needs to be made
    DeploymentInfo("6.NE", upload_to_orch=False)
    ApplianceInfo("6.NE", upload_to_orch=False)

if __name__ == '__main__':
    main()