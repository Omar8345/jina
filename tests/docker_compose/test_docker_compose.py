# kind version has to be bumped to v0.11.1 since pytest-kind is just using v0.10.0 which does not work on ubuntu in ci
import os
import subprocess
import time
from typing import Dict, List

import docker
import pytest

from jina import Document, Flow


class DockerComposeFlow:

    healthy_status = 'healthy'
    unhealthy_status = 'unhealthy'

    def __init__(self, dump_path, timeout_second=30):
        self.dump_path = dump_path
        self.timeout_second = timeout_second

    def __enter__(self):
        subprocess.run(
            f'docker-compose -f {self.dump_path} up --build -d --remove-orphans'.split(
                ' '
            )
        )

        container_ids = (
            subprocess.run(
                f'docker-compose -f {self.dump_path} ps -q'.split(' '),
                capture_output=True,
            )
            .stdout.decode("utf-8")
            .split('\n')
        )
        container_ids.remove('')  # remove empty  return line

        if not container_ids:
            raise RuntimeError('docker-compose ps did not detect any launch container')

        client = docker.from_env()

        init_time = time.time()
        healthy = False

        while time.time() - init_time < self.timeout_second:
            if self._are_all_container_healthy(container_ids, client):
                healthy = True
                break
            time.sleep(0.1)

        if not healthy:
            raise RuntimeError('Docker containers are not healthy')

    @staticmethod
    def _are_all_container_healthy(
        container_ids: List[str], client: docker.client.DockerClient
    ) -> bool:

        for id_ in container_ids:
            status = client.containers.get(id_).attrs['State']['Health']['Status']

            if status != DockerComposeFlow.healthy_status:
                return False
        return True

    def __exit__(self, exc_type, exc_val, exc_tb):
        subprocess.run(
            f'docker-compose -f {self.dump_path} down --remove-orphans'.split(' ')
        )


async def run_test(flow, endpoint, num_docs=10, request_size=10):
    # start port forwarding
    from jina.clients import Client

    client_kwargs = dict(
        host='localhost',
        port=flow.port,
        return_responses=True,
        asyncio=True,
    )
    client_kwargs.update(flow._common_kwargs)

    client = Client(**client_kwargs)
    client.show_progress = True
    responses = []
    async for resp in client.post(
        endpoint,
        inputs=[Document() for _ in range(num_docs)],
        request_size=request_size,
    ):
        responses.append(resp)

    return responses


@pytest.fixture()
def flow_with_sharding(docker_images, polling):
    flow = Flow(name='test-flow-with-sharding', port=9090, protocol='http').add(
        name='test_executor_sharding',
        shards=2,
        replicas=2,
        uses=f'docker://{docker_images[0]}',
        uses_after=f'docker://{docker_images[1]}',
        polling=polling,
    )
    return flow


@pytest.fixture
def flow_configmap(docker_images):
    flow = Flow(name='k8s-flow-configmap', port=9091, protocol='http').add(
        name='test_executor_configmap',
        uses=f'docker://{docker_images[0]}',
        env={'k1': 'v1', 'k2': 'v2'},
    )
    return flow


@pytest.fixture
def flow_with_needs(docker_images):
    flow = (
        Flow(
            name='test-flow-with-needs',
            port=9092,
            protocol='http',
        )
        .add(
            name='segmenter',
            uses=f'docker://{docker_images[0]}',
        )
        .add(
            name='textencoder',
            uses=f'docker://{docker_images[0]}',
            needs='segmenter',
        )
        .add(
            name='imageencoder',
            uses=f'docker://{docker_images[0]}',
            needs='segmenter',
        )
        .add(
            name='merger',
            uses=f'docker://{docker_images[1]}',
            needs=['imageencoder', 'textencoder'],
            disable_reduce=True,
        )
    )
    return flow


@pytest.mark.asyncio
@pytest.mark.timeout(3600)
@pytest.mark.parametrize(
    'docker_images',
    [['test-executor', 'executor-merger', 'jinaai/jina']],
    indirect=True,
)
async def test_flow_with_needs(logger, flow_with_needs, tmpdir, docker_images):
    dump_path = os.path.join(str(tmpdir), 'docker-compose-flow-with-need.yml')
    flow_with_needs.to_docker_compose_yaml(dump_path, 'default')
    with DockerComposeFlow(dump_path):
        resp = await run_test(
            flow=flow_with_needs,
            endpoint='/debug',
        )
        expected_traversed_executors = {
            'segmenter',
            'imageencoder',
            'textencoder',
        }

        docs = resp[0].docs
        assert len(docs) == 10
        for doc in docs:
            assert set(doc.tags['traversed-executors']) == expected_traversed_executors


@pytest.mark.timeout(3600)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    'docker_images',
    [['test-executor', 'executor-merger', 'jinaai/jina']],
    indirect=True,
)
@pytest.mark.parametrize('polling', ['ANY', 'ALL'])
async def test_flow_with_sharding(flow_with_sharding, polling, tmpdir):
    dump_path = os.path.join(str(tmpdir), 'docker-compose-flow-sharding.yml')
    flow_with_sharding.to_docker_compose_yaml(dump_path)

    with DockerComposeFlow(dump_path):
        resp = await run_test(
            flow=flow_with_sharding, endpoint='/debug', num_docs=10, request_size=1
        )

    assert len(resp) == 10
    docs = resp[0].docs
    for r in resp[1:]:
        docs.extend(r.docs)
    assert len(docs) == 10

    runtimes_to_visit = {
        'test_executor_sharding-0/rep-0',
        'test_executor_sharding-1/rep-0',
        'test_executor_sharding-0/rep-1',
        'test_executor_sharding-1/rep-1',
    }

    for doc in docs:
        if polling == 'ALL':
            assert len(set(doc.tags['traversed-executors'])) == 2
            assert set(doc.tags['shard_id']) == {0, 1}
            assert doc.tags['parallel'] == [2, 2]
            assert doc.tags['shards'] == [2, 2]
            for executor in doc.tags['traversed-executors']:
                if executor in runtimes_to_visit:
                    runtimes_to_visit.remove(executor)
        else:
            assert len(set(doc.tags['traversed-executors'])) == 1
            assert len(set(doc.tags['shard_id'])) == 1
            assert 0 in set(doc.tags['shard_id']) or 1 in set(doc.tags['shard_id'])
            assert doc.tags['parallel'] == [2]
            assert doc.tags['shards'] == [2]
            for executor in doc.tags['traversed-executors']:
                if executor in runtimes_to_visit:
                    runtimes_to_visit.remove(executor)

    assert len(runtimes_to_visit) == 0


@pytest.mark.timeout(3600)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    'docker_images', [['test-executor', 'jinaai/jina']], indirect=True
)
async def test_flow_with_configmap(flow_configmap, docker_images, tmpdir):
    dump_path = os.path.join(str(tmpdir), 'docker-compose-flow-configmap.yml')
    flow_configmap.to_docker_compose_yaml(dump_path)

    with DockerComposeFlow(dump_path):
        resp = await run_test(
            flow=flow_configmap,
            endpoint='/env',
        )

    docs = resp[0].docs
    assert len(docs) == 10
    for doc in docs:
        assert doc.tags['k1'] == 'v1'
        assert doc.tags['k2'] == 'v2'
        assert doc.tags['env'] == {'k1': 'v1', 'k2': 'v2'}


@pytest.mark.asyncio
@pytest.mark.timeout(3600)
@pytest.mark.parametrize(
    'docker_images',
    [['test-executor', 'jinaai/jina']],
    indirect=True,
)
async def test_flow_with_workspace(logger, docker_images, tmpdir):
    flow = Flow(name='k8s_flow-with_workspace', port=9090, protocol='http').add(
        name='test_executor',
        uses=f'docker://{docker_images[0]}',
        workspace='/shared',
    )

    dump_path = os.path.join(str(tmpdir), 'docker-compose-flow-workspace.yml')
    flow.to_docker_compose_yaml(dump_path)

    with DockerComposeFlow(dump_path):
        resp = await run_test(
            flow=flow,
            endpoint='/workspace',
        )

    docs = resp[0].docs
    assert len(docs) == 10
    for doc in docs:
        assert doc.tags['workspace'] == '/shared/TestExecutor/0'
