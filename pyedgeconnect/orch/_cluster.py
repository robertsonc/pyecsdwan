# MIT License
# (C) Copyright 2021 Hewlett Packard Enterprise Development LP.
#
# group : Cluster information


def get_cluster_state(self, cluster_name: str, cached: bool) -> dict:
    """Get the state of a cluster.

    .. list-table::
        :header-rows: 1

        * - Swagger Section
          - Method
          - Endpoint
        * - cluster
          - GET
          - /cluster

    :param cluster_name: Site / Cluster Name
    :type cluster_name: str
    :param cached: Boolean indicating whether to get data from cache (true)
        or from appliance (false)
    :type cached: bool
    :return: Returns dictionary of cluster state information \n
        * keyword **<siteName>** (`dict`): Dictionary with site information \n
            * keyword **siteName** (`str`): Name of the site/cluster
            * keyword **inSync** (`bool`): Whether the cluster is in sync
            * keyword **nonce** (`float`): Nonce value for the cluster
            * keyword **siteApplianceInterfaceIps** (`dict`): Dictionary of appliance
                interface IPs keyed by network primary key (nePk) \n
                * keyword **<nePk>** (`dict`): Dictionary with appliance information \n
                    * keyword **applianceName** (`str`): Name of the appliance
                    * keyword **interfaceLabel** (`str`): Label of the interface
                    * keyword **interfaceName** (`str`): Name of the interface
                    * keyword **state** (`dict`): State information for the interface \n
                        * keyword **peerIPStatusList** (`list[dict]`): List of peer IP statuses \n
                            * keyword **peerIP** (`str`): IP address of the peer
                            * keyword **peerIPState** (`str`): State of the peer IP
                        * keyword **cluster** (`bool`): Whether clustering is enabled
                        * keyword **flow_redir** (`bool`): Whether flow redirection is enabled
                        * keyword **wait_time** (`float`): Wait time value
                        * keyword **interface** (`str`): Interface name
                        * keyword **user_sync** (`bool`): Whether user sync is enabled
                    * keyword **ip** (`str`): IP address of the interface
    :rtype: dict
    """

    if self.orch_version < 9.5:
        raise ValueError(
            "Cluster state is only supported on Orchestrator 9.5 and above"
        )
    else:
        path = f"/cluster?clusterName={cluster_name}&cached={cached}"

    return self._get(path)


def get_cluster_alarm_count(self) -> dict:
    """Get the alarm count for the cluster.

    .. list-table::
        :header-rows: 1

        * - Swagger Section
          - Method
          - Endpoint
        * - cluster
          - GET
          - /cluster/alarmCount

    :return: Returns dictionary containing alarm count information for the cluster
    :rtype: dict
    """

    if self.orch_version < 9.5:
        raise ValueError(
            "Cluster alarm count is only supported on Orchestrator 9.5 and above"
        )
    else:
        path = "/cluster/alarmCount"

    return self._get(path)
