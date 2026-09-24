"""Validate separate Chitti and services Compose projects without starting containers."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CHITTI_SERVICES = {'chitti', 'qdrant', 'chitti-model-preload', 'chitti-semantic-index', 'chitti-nginx'}
STANDALONE_REQUIRED = ['CHITTI_BIND_IP', 'CHITTI_REGISTER_BASE_URL', 'CHITTI_GATEWAY_KEY',
                       'SVC_CHITTI_KEY', 'INTERNAL_SIGNING_SECRET', 'CHITTI_LLM_API_KEY',
                       'CHITTI_TLS_CERT_FILE', 'CHITTI_TLS_KEY_FILE']
REMOTE_REQUIRED = ['CHITTI_GATEWAY_KEY', 'GATEWAY_CHITTI_CA_FILE', 'GATEWAY_CHITTI_URL']


def split_config(standalone, missing=None, empty=False, *, identity=None, remote=True):
    if not shutil.which('docker'):
        pytest.skip('Docker Compose CLI required for configuration validation')
    files = ['docker-compose.chitti.yml'] if standalone else [
        'docker-compose.yml', 'docker-compose.prod-posture.yml', 'docker-compose.services.yml',
    ]
    if not standalone and not remote:
        files = files[:-1]
    cmd = ['docker', 'compose', '--env-file', '/dev/null']
    for file in files:
        cmd += ['-f', str(ROOT / 'deploy' / 'compose' / file)]
    if not standalone:
        cmd += ['--profile', 'sso']
    cmd += ['config', '--format', 'json']
    required = STANDALONE_REQUIRED if standalone else REMOTE_REQUIRED
    env = {'PATH': os.environ['PATH']}
    env.update({key: f'fixture-{key.lower()}-32-characters-value' for key in required})
    env.update({
        'CHITTI_BIND_IP': '10.0.1.10',
        'CHITTI_REGISTER_BASE_URL': 'https://services.example.test:8443/machine',
        'GATEWAY_CHITTI_URL': 'https://chitti.example.test:8443',
    })
    if identity:
        env.update(identity)
    if missing:
        env.pop(missing)
        if empty:
            env[missing] = ''
    return subprocess.run(cmd, env=env, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603


def test_chitti_project_runtime_artifacts_and_preparation_graph():
    result = split_config(True)
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config['name'] == 'chitti'
    services = config['services']
    assert set(services) == CHITTI_SERVICES
    assert set(config['volumes']) == {'chitti_model_cache', 'chitti_qdrant_data'}
    chat = services['chitti']
    env = chat['environment']
    assert env['CHITTI_ENVIRONMENT'] == 'production'
    assert env['CHITTI_REQUIRE_DELEGATION'] == env['CHITTI_PIPELINE_ENABLED'] == 'true'
    assert env['CHITTI_PIPELINE_STOP_AFTER'] == 'answer_generation'
    assert env['CHITTI_LOG_PIPELINE'] == env['CHITTI_CAPTURE_RETRIEVAL_TRACE'] == 'false'
    assert all(not env[key] for key in ('CHITTI_DEBUG_USER_EMAIL', 'CHITTI_DEBUG_USER_ID',
                                       'CHITTI_DEBUG_USER_ROLES'))
    assert env['CHITTI_MODEL_OFFLINE'] == env['HF_HUB_OFFLINE'] == 'true'
    assert env['CHITTI_REGISTER_BASE_URL'] == 'https://services.example.test:8443/machine'
    assert env['CHITTI_REGISTER_CA_FILE'] == '/etc/chitti/tls.crt'
    ca = next(v for v in chat['volumes'] if v['target'] == '/etc/chitti/tls.crt')
    assert ca['read_only'] and not ca['bind'].get('create_host_path', False)
    edge = services['chitti-nginx']
    assert edge['depends_on']['chitti']['condition'] == 'service_started'
    assert {'/etc/nginx/certs/tls.crt', '/etc/nginx/certs/tls.key'} <= {
        v['target'] for v in edge['volumes'] if v['read_only']
    }
    assert '/readyz' in chat['healthcheck']['test'][-1]
    assert chat['read_only'] and chat['cap_drop'] == ['ALL']
    assert chat['security_opt'] == ['no-new-privileges:true']
    assert int(chat['mem_limit']) == 4 * 1024**3 and float(chat['cpus']) == 2
    assert set(chat['depends_on']) == {'qdrant', 'chitti-semantic-index'}
    assert chat['depends_on']['qdrant']['condition'] == 'service_healthy'
    assert chat['depends_on']['chitti-semantic-index']['condition'] == 'service_completed_successfully'
    index = services['chitti-semantic-index']
    assert set(index['depends_on']) == {'qdrant', 'chitti-model-preload'}
    assert index['depends_on']['chitti-model-preload']['condition'] == 'service_completed_successfully'
    assert index['depends_on']['qdrant']['condition'] == 'service_healthy'
    assert index['environment']['CHITTI_MODEL_OFFLINE'] == 'true'
    preload = services['chitti-model-preload']
    assert preload['environment']['CHITTI_MODEL_OFFLINE'] == 'false'
    assert not preload.get('depends_on')
    for name in ('chitti', 'chitti-model-preload', 'chitti-semantic-index'):
        service = services[name]
        assert service['image'] == chat['image']
        assert service['build'] == chat['build']
        models = service['environment']
        for key in ('CHITTI_DENSE_MODEL', 'CHITTI_SPARSE_MODEL', 'CHITTI_RERANK_MODEL'):
            assert models[key] == env[key]
            assert models[key + '_REVISION'] == env[key + '_REVISION']
            assert len(models[key + '_REVISION']) == 40
        cache = next(v for v in service['volumes'] if v['target'] == '/models')
        assert cache['source'] == 'chitti_model_cache'
        assert bool(cache.get('read_only')) == (name != 'chitti-model-preload')
    assert preload['command'] == ['python', '-m', 'app.model_preload']
    assert index['command'] == ['python', '-m', 'app.semantic_index']
    qdrant = services['qdrant']
    assert qdrant['image'].startswith('qdrant/qdrant:v1.18.2-unprivileged@sha256:')
    assert qdrant['read_only'] and qdrant['cap_drop'] == ['ALL']
    assert int(qdrant['mem_limit']) == 2 * 1024**3 and float(qdrant['cpus']) == 1
    for name, service in services.items():
        assert service['logging'] == {'driver': 'journald', 'options': {'tag': 'prism.{{.Name}}'}}
        if name == 'chitti-nginx':
            assert [(p['host_ip'], p['published'], p['target']) for p in service['ports']] == [
                ('10.0.1.10', '8443', 443),
            ]
        else:
            assert not service.get('ports')


@pytest.mark.parametrize('settings', [
    {},  # Existing defaults, with no ACCESS_API_KEY or REGISTER_APP_PASSWORD.
    {
        'PRISM_DB_PASSWORD': 'existing-database-password',
        'SVC_CHITTI_KEY': 'existing-chitti-key',
        'SVC_GATEWAY_KEY': 'existing-gateway-key',
        'INTERNAL_SIGNING_SECRET': 'existing-signing-secret-32-characters',
        'GATEWAY_OIDC_ISSUERS': 'https://accounts.google.com|staging-client',
        'WORKFLOWS_OIDC_ISSUERS': 'https://accounts.google.com|workflow-client',
    },
])
def test_remote_sets_chitti_tls_and_removes_register_ports(settings):
    result = split_config(False, identity=settings)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)['services']
    result = split_config(False, remote=False, identity=settings)
    assert result.returncode == 0, result.stderr
    baseline = json.loads(result.stdout)['services']
    assert services.keys() == baseline.keys()
    assert not CHITTI_SERVICES.intersection(services)
    assert 'register-migrate' not in services
    for name, service in services.items():
        expected = baseline[name]
        if name == 'gateway':
            expected['environment']['GATEWAY_CHITTI_URL'] = 'https://chitti.example.test:8443'
            expected['environment']['GATEWAY_CHITTI_API_KEY'] = (
                'fixture-chitti_gateway_key-32-characters-value'
            )
            expected['environment']['GATEWAY_UPSTREAM_CA_FILE'] = '/etc/prism/chitti-ca.crt'
            assert service['volumes'][-1]['target'] == '/etc/prism/chitti-ca.crt'
            assert service['volumes'][-1]['read_only'] is True
            expected['volumes'] = service['volumes']
        elif name == 'register':
            assert not service.get('ports')
            expected.pop('ports', None)
        assert service == expected, name
        assert set(service.get('depends_on', {})) <= services.keys()
    assert services['register']['environment']['REGISTER_DB_USER'] == 'prism'
    assert services['access']['environment']['ACCESS_API_KEYS'] == 'dev-local-key'
    assert services['gateway']['environment']['GATEWAY_REQUIRE_AUTH'] == 'true'
    if settings:
        register = services['register']['environment']
        assert 'svc_chitti:existing-chitti-key' in register['REGISTER_SERVICE_API_KEYS']
        assert register['REGISTER_INTERNAL_SIGNING_SECRET'] == settings['INTERNAL_SIGNING_SECRET']
        assert services['gateway']['environment']['GATEWAY_INTERNAL_SIGNING_SECRET'] == (
            settings['INTERNAL_SIGNING_SECRET']
        )
    # Only the existing dependencies are started by the targeted rollout.
    selected = {'register', 'gateway'}
    pending = list(selected)
    while pending:
        for dependency in services[pending.pop()].get('depends_on', {}):
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    assert selected == {'register', 'gateway', 'postgres', 'minio', 'access'}


@pytest.mark.parametrize('standalone,required', [(True, STANDALONE_REQUIRED), (False, REMOTE_REQUIRED)])
@pytest.mark.parametrize('empty', [False, True])
def test_split_deployment_refuses_missing_or_empty_configuration(standalone, required, empty):
    for missing in required:
        result = split_config(standalone, missing=missing, empty=empty)
        assert result.returncode != 0, missing
        assert missing in result.stderr


@pytest.mark.parametrize('identity', [
    {},  # Existing Dex posture.
    {
        'GATEWAY_OIDC_ISSUERS': 'https://accounts.google.com|staging-client',
        'WORKFLOWS_OIDC_ISSUERS': 'https://accounts.google.com|workflow-client',
        'GATEWAY_OIDC_ALLOWED_DOMAINS': 'evamfinance.com',
        'WORKFLOWS_OIDC_ALLOWED_DOMAINS': 'evamfinance.com',
        'GOOGLE_SSO_CLIENT_ID': 'staging-client',
        'UI_DEX_URL': 'https://staging.example.test',
    },
])
def test_remote_preserves_existing_sign_in_and_workflow_identity(identity):
    result = split_config(False, identity=identity)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)['services']
    result = split_config(False, identity=identity, remote=False)
    assert result.returncode == 0, result.stderr
    baseline = json.loads(result.stdout)['services']
    for name in ('gateway', 'workflows', 'orchestrator'):
        def auth_settings(service):
            return {key: value for key, value in service['environment'].items()
                    if '_OIDC_' in key or key.endswith('_REQUIRE_AUTH')}
        assert auth_settings(services[name]) == auth_settings(baseline[name])
    assert services['gateway']['environment']['GATEWAY_REQUIRE_AUTH'] == 'true'
    assert services['ui']['build']['args'] == baseline['ui']['build']['args']
    if identity:
        assert services['gateway']['environment']['GATEWAY_OIDC_ISSUERS'] == identity['GATEWAY_OIDC_ISSUERS']
        assert services['orchestrator']['environment']['WORKFLOWS_OIDC_ISSUERS'] == (
            identity['WORKFLOWS_OIDC_ISSUERS']
        )


@pytest.mark.parametrize('chitti_ip,services_ip', [('10.0.1.10', '10.0.1.20'), ('10.0.1.10', '10.0.1.10')])
def test_projects_connect_through_published_ports_on_one_or_two_hosts(chitti_ip, services_ip):
    chitti_port = 9443 if chitti_ip == services_ip else 8443
    shared = {'CHITTI_GATEWAY_KEY': 'shared-gateway-key', 'SVC_CHITTI_KEY': 'shared-register-key',
              'INTERNAL_SIGNING_SECRET': 'shared-signing-secret-32-characters'}
    chitti = split_config(True, identity={
        **shared, 'CHITTI_BIND_IP': chitti_ip, 'CHITTI_HTTPS_PORT': str(chitti_port),
        'CHITTI_REGISTER_BASE_URL': f'https://{services_ip}:8443/machine',
    })
    platform = split_config(False, identity={
        **shared, 'GATEWAY_CHITTI_URL': f'https://{chitti_ip}:{chitti_port}',
    })
    assert chitti.returncode == platform.returncode == 0, chitti.stderr + platform.stderr
    chat_config, platform_config = json.loads(chitti.stdout), json.loads(platform.stdout)
    assert chat_config['name'] != platform_config['name']
    assert {n['name'] for n in chat_config['networks'].values()}.isdisjoint(
        n['name'] for n in platform_config['networks'].values()
    )
    services = platform_config['services']
    chat = chat_config['services']['chitti']['environment']
    gateway, register = services['gateway']['environment'], services['register']['environment']
    assert gateway['GATEWAY_CHITTI_API_KEY'] == chat['CHITTI_API_KEYS']
    assert f"svc_chitti:{chat['CHITTI_REGISTER_API_KEY']}" in register['REGISTER_SERVICE_API_KEYS']
    assert chat['CHITTI_INTERNAL_SIGNING_SECRET'] == register['REGISTER_INTERNAL_SIGNING_SECRET']
    assert gateway['GATEWAY_INTERNAL_SIGNING_SECRET'] == chat['CHITTI_INTERNAL_SIGNING_SECRET']
    assert gateway['GATEWAY_CHITTI_URL'] == f'https://{chitti_ip}:{chitti_port}'
    assert chat['CHITTI_REGISTER_BASE_URL'] == f'https://{services_ip}:8443/machine'
    bindings = []
    for config in (chat_config, platform_config):
        for service in config['services'].values():
            bindings.extend((p['host_ip'], p['published'], p['protocol']) for p in service.get('ports', [])
                            if p.get('host_ip'))
    assert len(bindings) == len(set(bindings))
