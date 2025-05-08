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


# def get_all_cluster_profiles(self) -> list:
#     """Get all cluster profiles.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - GET
#           - /cluster/profiles
#
#     :return: Returns list of cluster profiles \n
#         * Each profile is a dictionary containing: \n
#             * keyword **id** (`str`): Unique identifier for the profile
#             * keyword **name** (`str`): Name of the profile
#             * keyword **interfaceLabel** (`str`): Label of the interface
#             * keyword **flowRedirection** (`str`): Flow redirection setting
#             * keyword **userSessionSync** (`str`): User session synchronization setting
#             * keyword **waitTime** (`int`): Wait time value in milliseconds
#             * keyword **isEdgeHaProfile** (`bool`): Whether the profile is an Edge HA profile
#     :rtype: list
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profiles are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profiles"
#
#     return self._get(path)
#
#
# def add_cluster_profiles(self, profiles: list) -> dict:
#     """Add one or more cluster profiles.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - POST
#           - /cluster/profiles
#
#     :param profiles: List of profile dictionaries to add
#         Each profile dictionary should contain:
#             * keyword **name** (`str`): Name of the profile
#             * keyword **interfaceLabel** (`str`): Label of the interface
#             * keyword **flowRedirection** (`str`): Flow redirection setting
#             * keyword **userSessionSync** (`str`): User session synchronization setting
#             * keyword **waitTime** (`int`): Wait time value in milliseconds
#     :type profiles: list
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profiles are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profiles"
#
#     return self._post(path, data=profiles)
#
#
# def update_cluster_profile(self, profile: dict) -> dict:
#     """Update an existing cluster profile.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - PUT
#           - /cluster/profiles
#
#     :param profile: Dictionary containing profile information to update
#         Must contain:
#             * keyword **id** (`str`): Unique identifier of the profile to update
#             * keyword **name** (`str`): Updated name of the profile
#             * keyword **interfaceLabel** (`str`): Updated label of the interface
#             * keyword **flowRedirection** (`str`): Updated flow redirection setting
#             * keyword **userSessionSync** (`str`): Updated user session synchronization setting
#             * keyword **waitTime** (`int`): Updated wait time value in milliseconds
#     :type profile: dict
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profiles are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profiles"
#
#     return self._put(path, data=profile)
#
#
# def delete_cluster_profile(self, profile_id: str) -> dict:
#     """Delete a cluster profile.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - DELETE
#           - /cluster/profiles
#
#     :param profile_id: Unique system generated Cluster Profile Id to delete
#     :type profile_id: str
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profiles are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profiles"
#
#     params = {
#         "profileId": profile_id
#     }
#
#     return self._delete(path, params=params)
#
#
# def get_all_cluster_profile_mappings(self) -> dict:
#     """Get all cluster profile mappings.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - GET
#           - /cluster/profileMapping
#
#     :return: Returns a dictionary where keys are cluster profile IDs and values are lists
#              of entities (like folders, processes, etc.) mapped to that profile
#         * key **<clusterProfileId>** (`list`): List of entity IDs mapped to the profile
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profile mappings are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profileMapping"
#
#     return self._get(path)
#
#
# def update_cluster_profile_mapping(self, mappings: dict) -> dict:
#     """Update cluster profile mappings.
#
#     Updates only the site mapping in the provided data. Existing mappings not included in the
#     request will remain unchanged.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - PUT
#           - /cluster/profileMapping
#
#     :param mappings: Dictionary where keys are cluster profile IDs and values are lists
#                     of entity IDs to be mapped to that profile
#         * key **<clusterProfileId>** (`list`): List of entity IDs to map to the profile
#     :type mappings: dict
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profile mappings are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profileMapping"
#
#     return self._put(path, data=mappings)
#
#
# def initialize_edge_ha_cluster(self, appliance_ids: list) -> dict:
#     """Initialize appliances for EdgeHA cluster.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - POST
#           - /cluster/initForEdgeHA
#
#     :param appliance_ids: List of appliance IDs to initialize for Edge HA cluster
#     :type appliance_ids: list
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Edge HA cluster initialization is only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/initForEdgeHA"
#
#     return self._post(path, data=appliance_ids)
#
#
# def initialize_edge_ha_cluster(self, appliance_ids: list) -> dict:
#     """Initialize appliances for EdgeHA cluster.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - POST
#           - /cluster/initForEdgeHA
#
#     :param appliance_ids: List of appliance IDs to initialize for Edge HA cluster
#     :type appliance_ids: list
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Edge HA cluster initialization is only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/initForEdgeHA"
#
#     return self._post(path, data=appliance_ids)
#
#
# def add_cluster_profiles(self, profiles: list) -> dict:
#     """Add cluster profiles.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - POST
#           - /cluster/profiles
#
#     :param profiles: List of cluster profile configurations to add
#         * keyword **name** (`str`): Name of the cluster profile
#         * keyword **interfaceLabel** (`str`): Label for the interface
#         * keyword **flowRedirection** (`str`): Flow redirection configuration
#         * keyword **userSessionSync** (`str`): User session synchronization configuration
#         * keyword **waitTime** (`int`): Wait time in milliseconds
#     :type profiles: list
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profiles are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profiles"
#
#     return self._post(path, data=profiles)
#
#
# def update_cluster_profile_mapping(self, profile_mapping: dict) -> dict:
#     """Add or update cluster profile mapping.
#     Updates only the site mapping in the request data, existing mappings will remain the same.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - PUT
#           - /cluster/profileMapping
#
#     :param profile_mapping: Dictionary mapping cluster profile IDs to lists of site IDs
#         Example: {"profile_id1": ["site_id1", "site_id2"]}
#     :type profile_mapping: dict
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profile mapping is only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profileMapping"
#
#     return self._put(path, data=profile_mapping)
#
#
# def update_cluster_profile(self, profile: dict) -> dict:
#     """Update a cluster profile.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - PUT
#           - /cluster/profiles
#
#     :param profile: Cluster profile configuration to update
#         * keyword **id** (`str`): ID of the cluster profile to update
#         * keyword **name** (`str`): Name of the cluster profile
#         * keyword **interfaceLabel** (`str`): Label for the interface
#         * keyword **flowRedirection** (`str`): Flow redirection configuration
#         * keyword **userSessionSync** (`str`): User session synchronization configuration
#         * keyword **waitTime** (`int`): Wait time in milliseconds
#     :type profile: dict
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profiles are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profiles"
#
#     return self._put(path, data=profile)
#
#
# def get_cluster_profiles(self) -> list:
#     """Get all cluster profiles.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - GET
#           - /cluster/profiles
#
#     :return: List of cluster profiles
#         * keyword **id** (`str`): ID of the cluster profile
#         * keyword **name** (`str`): Name of the cluster profile
#         * keyword **interfaceLabel** (`str`): Label for the interface
#         * keyword **flowRedirection** (`str`): Flow redirection configuration
#         * keyword **userSessionSync** (`str`): User session synchronization configuration
#         * keyword **waitTime** (`int`): Wait time in milliseconds
#         * keyword **isEdgeHaProfile** (`bool`): Whether this is an Edge HA profile
#     :rtype: list
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profiles are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profiles"
#
#     return self._get(path)
#
#
# def get_cluster_state(self, cluster_name: str, cached: bool = True) -> dict:
#     """Get the current state of a cluster.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - GET
#           - /cluster
#
#     :param cluster_name: Site/Cluster name
#     :type cluster_name: str
#     :param cached: Get data from cache (True) or from appliance (False), defaults to True
#     :type cached: bool, optional
#     :return: Dictionary containing cluster state information
#         * Outer key is site name, value is an object containing:
#             * keyword **siteName** (`str`): Name of the site
#             * keyword **inSync** (`bool`): Whether the site is in sync
#             * keyword **nonce** (`float`): Nonce value
#             * keyword **siteApplianceInterfaceIps** (`dict`): Dictionary of appliance interfaces
#                 * Each key is an appliance ID (`nePk`), value is an object with:
#                     * keyword **applianceName** (`str`): Name of the appliance
#                     * keyword **interfaceLabel** (`str`): Label of the interface
#                     * keyword **interfaceName** (`str`): Name of the interface
#                     * keyword **ip** (`str`): IP address
#                     * keyword **state** (`dict`): State information including:
#                         * keyword **peerIPStatusList** (`list`): List of peer IP status objects
#                         * keyword **cluster** (`bool`): Whether clustering is enabled
#                         * keyword **flow_redir** (`bool`): Whether flow redirection is enabled
#                         * keyword **wait_time** (`float`): Wait time
#                         * keyword **interface** (`str`): Interface name
#                         * keyword **user_sync** (`bool`): Whether user sync is enabled
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster state information is only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster"
#
#     params = {
#         "clusterName": cluster_name,
#         "cached": str(cached).lower()
#     }
#
#     return self._get(path, params=params)
#
#
# def delete_cluster_profile(self, profile_id: str) -> dict:
#     """Delete a cluster profile.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - DELETE
#           - /cluster/profiles
#
#     :param profile_id: Unique system generated Cluster Profile ID
#     :type profile_id: str
#     :return: Returns dictionary indicating success status
#         * keyword **success** (`bool`): Whether the operation was successful
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profiles are only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profiles"
#
#     params = {
#         "profileId": profile_id
#     }
#
#     return self._delete(path, params=params)
#
#
# def get_cluster_alarm_count(self) -> dict:
#     """Get alarm counts for all clusters.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - GET
#           - /cluster/alarmCount
#
#     :return: Dictionary of alarm counts by site name
#         * Outer key is site name, value is an object containing:
#             * keyword **alarmCount** (`int`): Number of alarms for the site
#             * keyword **maxSeverity** (`int`): Maximum severity level of alarms
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster alarm count is only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/alarmCount"
#
#     return self._get(path)
#
#
# def get_cluster_profile_mapping(self) -> dict:
#     """Get mapping of all cluster profiles to site names.
#
#     .. list-table::
#         :header-rows: 1
#
#         * - Swagger Section
#           - Method
#           - Endpoint
#         * - cluster
#           - GET
#           - /cluster/profileMapping
#
#     :return: Dictionary mapping cluster profile IDs to lists of site names
#         * Each key is a cluster profile ID
#         * Each value is a list of site names using that profile
#     :rtype: dict
#     """
#
#     if self.orch_version < 9.5:
#         raise ValueError(
#             "Cluster profile mapping is only supported on Orchestrator 9.5 and above"
#         )
#     else:
#         path = "/cluster/profileMapping"
#
#     return self._get(path)