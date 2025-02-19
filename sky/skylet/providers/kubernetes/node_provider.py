import copy
import logging
import time
from typing import Dict
from urllib.parse import urlparse
from uuid import uuid4

from ray.autoscaler._private.command_runner import SSHCommandRunner
from ray.autoscaler.node_provider import NodeProvider
from ray.autoscaler.tags import NODE_KIND_HEAD
from ray.autoscaler.tags import TAG_RAY_CLUSTER_NAME
from ray.autoscaler.tags import TAG_RAY_NODE_KIND

from sky.adaptors import kubernetes
from sky.skylet.providers.kubernetes import config
from sky.utils import kubernetes_utils

logger = logging.getLogger(__name__)

MAX_TAG_RETRIES = 3
DELAY_BEFORE_TAG_RETRY = 0.5

RAY_COMPONENT_LABEL = 'cluster.ray.io/component'


# Monkey patch SSHCommandRunner to allow specifying SSH port
def set_port(self, port):
    self.ssh_options.arg_dict['Port'] = port


SSHCommandRunner.set_port = set_port


def head_service_selector(cluster_name: str) -> Dict[str, str]:
    """Selector for Operator-configured head service."""
    return {RAY_COMPONENT_LABEL: f'{cluster_name}-ray-head'}


def to_label_selector(tags):
    label_selector = ''
    for k, v in tags.items():
        if label_selector != '':
            label_selector += ','
        label_selector += '{}={}'.format(k, v)
    return label_selector


class KubernetesNodeProvider(NodeProvider):

    def __init__(self, provider_config, cluster_name):
        NodeProvider.__init__(self, provider_config, cluster_name)
        self.cluster_name = cluster_name

        # Kubernetes namespace to user
        self.namespace = kubernetes_utils.get_current_kube_config_context_namespace(
        )

        # Timeout for resource provisioning. If it takes longer than this
        # timeout, the resource provisioning will be considered failed.
        # This is useful for failover. May need to be adjusted for different
        # kubernetes setups.
        self.timeout = provider_config['timeout']

    def non_terminated_nodes(self, tag_filters):
        # Match pods that are in the 'Pending' or 'Running' phase.
        # Unfortunately there is no OR operator in field selectors, so we
        # have to match on NOT any of the other phases.
        field_selector = ','.join([
            'status.phase!=Failed',
            'status.phase!=Unknown',
            'status.phase!=Succeeded',
            'status.phase!=Terminating',
        ])

        tag_filters[TAG_RAY_CLUSTER_NAME] = self.cluster_name
        label_selector = to_label_selector(tag_filters)
        pod_list = kubernetes.core_api().list_namespaced_pod(
            self.namespace,
            field_selector=field_selector,
            label_selector=label_selector)

        # Don't return pods marked for deletion,
        # i.e. pods with non-null metadata.DeletionTimestamp.
        return [
            pod.metadata.name
            for pod in pod_list.items
            if pod.metadata.deletion_timestamp is None
        ]

    def is_running(self, node_id):
        pod = kubernetes.core_api().read_namespaced_pod(node_id, self.namespace)
        return pod.status.phase == 'Running'

    def is_terminated(self, node_id):
        pod = kubernetes.core_api().read_namespaced_pod(node_id, self.namespace)
        return pod.status.phase not in ['Running', 'Pending']

    def node_tags(self, node_id):
        pod = kubernetes.core_api().read_namespaced_pod(node_id, self.namespace)
        return pod.metadata.labels

    def external_ip(self, node_id):
        # Return the IP address of the first node with an external IP
        nodes = kubernetes.core_api().list_node().items
        for node in nodes:
            if node.status.addresses:
                for address in node.status.addresses:
                    if address.type == 'ExternalIP':
                        return address.address
        # If no external IP is found, use the API server IP
        api_host = kubernetes.core_api().api_client.configuration.host
        parsed_url = urlparse(api_host)
        return parsed_url.hostname

    def external_port(self, node_id):
        # Extract the NodePort of the head node's SSH service
        # Node id is str e.g., example-cluster-ray-head-v89lb

        # TODO(romilb): Implement caching here for performance.
        # TODO(romilb): Multi-node would need more handling here.
        cluster_name = node_id.split('-ray-head')[0]
        return kubernetes_utils.get_head_ssh_port(cluster_name, self.namespace)

    def internal_ip(self, node_id):
        pod = kubernetes.core_api().read_namespaced_pod(node_id, self.namespace)
        return pod.status.pod_ip

    def get_node_id(self, ip_address, use_internal_ip=True) -> str:

        def find_node_id():
            if use_internal_ip:
                return self._internal_ip_cache.get(ip_address)
            else:
                return self._external_ip_cache.get(ip_address)

        if not find_node_id():
            all_nodes = self.non_terminated_nodes({})
            ip_func = self.internal_ip if use_internal_ip else self.external_ip
            ip_cache = (self._internal_ip_cache
                        if use_internal_ip else self._external_ip_cache)
            for node_id in all_nodes:
                ip_cache[ip_func(node_id)] = node_id

        if not find_node_id():
            if use_internal_ip:
                known_msg = f'Worker internal IPs: {list(self._internal_ip_cache)}'
            else:
                known_msg = f'Worker external IP: {list(self._external_ip_cache)}'
            raise ValueError(f'ip {ip_address} not found. ' + known_msg)

        return find_node_id()

    def set_node_tags(self, node_ids, tags):
        for _ in range(MAX_TAG_RETRIES - 1):
            try:
                self._set_node_tags(node_ids, tags)
                return
            except kubernetes.api_exception() as e:
                if e.status == 409:
                    logger.info(config.log_prefix +
                                'Caught a 409 error while setting'
                                ' node tags. Retrying...')
                    time.sleep(DELAY_BEFORE_TAG_RETRY)
                    continue
                else:
                    raise
        # One more try
        self._set_node_tags(node_ids, tags)

    def _set_node_tags(self, node_id, tags):
        pod = kubernetes.core_api().read_namespaced_pod(node_id, self.namespace)
        pod.metadata.labels.update(tags)
        kubernetes.core_api().patch_namespaced_pod(node_id, self.namespace, pod)

    def create_node(self, node_config, tags, count):
        conf = copy.deepcopy(node_config)
        pod_spec = conf.get('pod', conf)
        service_spec = conf.get('service')
        node_uuid = str(uuid4())
        tags[TAG_RAY_CLUSTER_NAME] = self.cluster_name
        tags['ray-node-uuid'] = node_uuid
        pod_spec['metadata']['namespace'] = self.namespace
        if 'labels' in pod_spec['metadata']:
            pod_spec['metadata']['labels'].update(tags)
        else:
            pod_spec['metadata']['labels'] = tags

        # Allow Operator-configured service to access the head node.
        if tags[TAG_RAY_NODE_KIND] == NODE_KIND_HEAD:
            head_selector = head_service_selector(self.cluster_name)
            pod_spec['metadata']['labels'].update(head_selector)

        logger.info(config.log_prefix +
                    'calling create_namespaced_pod (count={}).'.format(count))
        new_nodes = []
        for _ in range(count):
            pod = kubernetes.core_api().create_namespaced_pod(
                self.namespace, pod_spec)
            new_nodes.append(pod)

        new_svcs = []
        if service_spec is not None:
            logger.info(config.log_prefix + 'calling create_namespaced_service '
                        '(count={}).'.format(count))

            for new_node in new_nodes:

                metadata = service_spec.get('metadata', {})
                metadata['name'] = new_node.metadata.name
                service_spec['metadata'] = metadata
                service_spec['spec']['selector'] = {'ray-node-uuid': node_uuid}
                svc = kubernetes.core_api().create_namespaced_service(
                    self.namespace, service_spec)
                new_svcs.append(svc)

        # Wait for all pods to be ready, and if it exceeds the timeout, raise an
        # exception. If pod's container is ContainerCreating, then we can assume
        # that resources have been allocated and we can exit.

        start = time.time()
        while True:
            if time.time() - start > self.timeout:
                raise config.KubernetesError(
                    'Timed out while waiting for nodes to start. '
                    'Cluster may be out of resources or '
                    'may be too slow to autoscale.')
            all_ready = True

            for node in new_nodes:
                pod = kubernetes.core_api().read_namespaced_pod(
                    node.metadata.name, self.namespace)
                if pod.status.phase == 'Pending':
                    # Iterate over each pod to check their status
                    if pod.status.container_statuses is not None:
                        for container_status in pod.status.container_statuses:
                            # Continue if container status is ContainerCreating
                            # This indicates this pod has been scheduled.
                            if container_status.state.waiting is not None and container_status.state.waiting.reason == 'ContainerCreating':
                                continue
                            else:
                                # If the container wasn't in creating state,
                                # then we know pod wasn't scheduled or had some
                                # other error, such as image pull error.
                                # See list of possible reasons for waiting here:
                                # https://stackoverflow.com/a/57886025
                                all_ready = False
                    else:
                        # If container_statuses is None, then the pod hasn't
                        # been scheduled yet.
                        all_ready = False
            if all_ready:
                break
            time.sleep(1)

    def terminate_node(self, node_id):
        logger.info(config.log_prefix + 'calling delete_namespaced_pod')
        try:
            kubernetes.core_api().delete_namespaced_pod(
                node_id,
                self.namespace,
                _request_timeout=config.DELETION_TIMEOUT)
        except kubernetes.api_exception() as e:
            if e.status == 404:
                logger.warning(config.log_prefix +
                               f'Tried to delete pod {node_id},'
                               ' but the pod was not found (404).')
            else:
                raise
        try:
            kubernetes.core_api().delete_namespaced_service(
                node_id,
                self.namespace,
                _request_timeout=config.DELETION_TIMEOUT)
            kubernetes.core_api().delete_namespaced_service(
                f'{node_id}-ssh',
                self.namespace,
                _request_timeout=config.DELETION_TIMEOUT)
        except kubernetes.api_exception():
            pass

    def terminate_nodes(self, node_ids):
        # TODO(romilb): terminate_nodes should be include optimizations for
        #  deletion of multiple nodes. Currently, it deletes one node at a time.
        #  We should look in to using deletecollection here for batch deletion.
        for node_id in node_ids:
            self.terminate_node(node_id)

    def get_command_runner(self,
                           log_prefix,
                           node_id,
                           auth_config,
                           cluster_name,
                           process_runner,
                           use_internal_ip,
                           docker_config=None):
        """Returns the CommandRunner class used to perform SSH commands.

        Args:
        log_prefix(str): stores "NodeUpdater: {}: ".format(<node_id>). Used
            to print progress in the CommandRunner.
        node_id(str): the node ID.
        auth_config(dict): the authentication configs from the autoscaler
            yaml file.
        cluster_name(str): the name of the cluster.
        process_runner(module): the module to use to run the commands
            in the CommandRunner. E.g., subprocess.
        use_internal_ip(bool): whether the node_id belongs to an internal ip
            or external ip.
        docker_config(dict): If set, the docker information of the docker
            container that commands should be run on.
        """
        common_args = {
            'log_prefix': log_prefix,
            'node_id': node_id,
            'provider': self,
            'auth_config': auth_config,
            'cluster_name': cluster_name,
            'process_runner': process_runner,
            'use_internal_ip': use_internal_ip,
        }
        command_runner = SSHCommandRunner(**common_args)
        if use_internal_ip:
            port = 22
        else:
            port = self.external_port(node_id)
        command_runner.set_port(port)
        return command_runner

    @staticmethod
    def bootstrap_config(cluster_config):
        return config.bootstrap_kubernetes(cluster_config)

    @staticmethod
    def fillout_available_node_types_resources(cluster_config):
        """Fills out missing "resources" field for available_node_types."""
        return config.fillout_resources_kubernetes(cluster_config)
