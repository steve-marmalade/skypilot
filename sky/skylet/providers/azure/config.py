import json
import logging
import random
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from azure.common.credentials import get_cli_profile
from azure.identity import AzureCliCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.resource.resources.models import DeploymentMode

UNIQUE_ID_LEN = 4

logger = logging.getLogger(__name__)


def get_azure_sdk_function(client: Any, function_name: str) -> Callable:
    """Retrieve a callable function from Azure SDK client object.

    Newer versions of the various client SDKs renamed function names to
    have a begin_ prefix. This function supports both the old and new
    versions of the SDK by first trying the old name and falling back to
    the prefixed new name.
    """
    func = getattr(
        client, function_name, getattr(client, f"begin_{function_name}", None)
    )
    if func is None:
        raise AttributeError(
            "'{obj}' object has no {func} or begin_{func} attribute".format(
                obj={client.__name__}, func=function_name
            )
        )
    return func


def bootstrap_azure(config):
    config = _configure_key_pair(config)
    config = _configure_resource_group(config)
    return config


def _configure_resource_group(config):
    # TODO: look at availability sets
    # https://docs.microsoft.com/en-us/azure/virtual-machines/windows/tutorial-availability-sets
    subscription_id = config["provider"].get("subscription_id")
    if subscription_id is None:
        subscription_id = get_cli_profile().get_subscription_id()
    # Increase the timeout to fix the Azure get-access-token (used by ray azure
    # node_provider) timeout issue.
    # Tracked in https://github.com/Azure/azure-cli/issues/20404#issuecomment-1249575110
    resource_client = ResourceManagementClient(
        AzureCliCredential(process_timeout=30), subscription_id
    )
    config["provider"]["subscription_id"] = subscription_id
    logger.info("Using subscription id: %s", subscription_id)

    assert (
        "resource_group" in config["provider"]
    ), "Provider config must include resource_group field"
    resource_group = config["provider"]["resource_group"]

    assert (
        "location" in config["provider"]
    ), "Provider config must include location field"
    params = {"location": config["provider"]["location"]}

    if "tags" in config["provider"]:
        params["tags"] = config["provider"]["tags"]

    logger.info("Creating/Updating resource group: %s", resource_group)
    rg_create_or_update = get_azure_sdk_function(
        client=resource_client.resource_groups, function_name="create_or_update"
    )
    rg_create_or_update(resource_group_name=resource_group, parameters=params)

    # load the template file
    current_path = Path(__file__).parent
    template_path = current_path.joinpath("azure-config-template.json")
    with open(template_path, "r") as template_fp:
        template = json.load(template_fp)

    # Setup firewall rules for ports
    nsg_resource = None
    for resource in template["resources"]:
        if resource["type"] == "Microsoft.Network/networkSecurityGroups":
            nsg_resource = resource
            break
    assert nsg_resource is not None, "Could not find NSG resource in template"
    ports = config["provider"].get("ports", None)
    if ports is not None:
        ports = [str(port) for port in ports if port != 22]
        nsg_resource["properties"]["securityRules"].append(
            {
                "name": "user-ports",
                "properties": {
                    "priority": 1001,
                    "protocol": "TCP",
                    "access": "Allow",
                    "direction": "Inbound",
                    "sourceAddressPrefix": "*",
                    "sourcePortRange": "*",
                    "destinationAddressPrefix": "*",
                    "destinationPortRanges": ports,
                },
            }
        )

    logger.info("Using cluster name: %s", config["cluster_name"])

    # set unique id for resources in this cluster
    unique_id = config["provider"].get("unique_id")
    if unique_id is None:
        hasher = sha256()
        hasher.update(config["provider"]["resource_group"].encode("utf-8"))
        unique_id = hasher.hexdigest()[:UNIQUE_ID_LEN]
    else:
        unique_id = str(unique_id)
    config["provider"]["unique_id"] = unique_id
    logger.info("Using unique id: %s", unique_id)
    cluster_id = "{}-{}".format(config["cluster_name"], unique_id)

    subnet_mask = config["provider"].get("subnet_mask")
    if subnet_mask is None:
        # choose a random subnet, skipping most common value of 0
        random.seed(unique_id)
        subnet_mask = "10.{}.0.0/16".format(random.randint(1, 254))
    logger.info("Using subnet mask: %s", subnet_mask)

    parameters = {
        "properties": {
            "mode": DeploymentMode.incremental,
            "template": template,
            "parameters": {
                "subnet": {"value": subnet_mask},
                "clusterId": {"value": cluster_id},
            },
        }
    }

    create_or_update = get_azure_sdk_function(
        client=resource_client.deployments, function_name="create_or_update"
    )
    outputs = (
        create_or_update(
            resource_group_name=resource_group,
            deployment_name="ray-config",
            parameters=parameters,
        )
        .result()
        .properties.outputs
    )

    # append output resource ids to be used with vm creation
    config["provider"]["msi"] = outputs["msi"]["value"]
    config["provider"]["nsg"] = outputs["nsg"]["value"]
    config["provider"]["subnet"] = outputs["subnet"]["value"]

    return config


def _configure_key_pair(config):
    # SkyPilot: The original checks and configurations are no longer
    # needed, since we have already set them up in the upper level
    # SkyPilot codes. See sky/templates/azure-ray.yml.j2
    return config
