# MIT License
# (C) Copyright 2021 Hewlett Packard Enterprise Development LP.
#
# group : Cluster information


def get_cluster_state(self, cluster_name: str, cached: bool) -> dict:
    """
    Get the state of a cluster.

    Args:
        request: The HTTP request object
        **kwargs: Additional keyword arguments

    Query Parameters:
        clusterName: Site / Cluster Name
        cached: (Required) Get data from cache (true) or from appliance (false)

    Returns:
        JSON response with cluster state information
    """

    if self.orch_version < 9.5:
        raise ValueError(
            "Cluster state is only supported on Orchestrator 9.5 and above"
        )
    else:
        path = f"/cluster?clusterName={cluster_name}&cached={cached}"

    return self._get(path)
