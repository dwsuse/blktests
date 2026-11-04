# SPDX-License-Identifier: GPL-3.0+
# Copyright (c) 2026 SUSE LLC and contributors
"""Tests for the ONTAP target, against a fake ONTAP REST API."""

import base64
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nvme_target_control import target
from nvme_target_control.target import Subsystem
from nvme_target_control.targets import ontap

SVM_UUID = 'd714f2f5-ab77-11ed-af12-00a0988fb732'
PREFIX = 'nqn.1992-08.com.netapp:sn.d714f2f5ab7711edaf1200a0988fb732'
NQN = PREFIX + ':subsystem.blktests'
HOSTNQN = 'nqn.2014-08.org.nvmexpress:uuid:57c34e91'
KEY = 'DHHC-1:01:aGVsbG8gd29ybGQgaGVsbG8gd29ybGQgaGVsbG8hISEhTr8=:'
KEY2 = 'DHHC-1:00:YW5vdGhlciBrZXkgYW5vdGhlciBrZXkgYW5vdGhlciEhIUV8=:'


class Response(io.BytesIO):
    def __init__(self, status: int, body: dict[str, Any] | None) -> None:
        super().__init__(
            json.dumps(body).encode() if body is not None else b''
        )
        self.status = status


def records(r: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    """A collection GET reply."""
    return 200, {'records': r, 'num_records': len(r)}


class FakeOntap:
    """The REST resources the ONTAP target uses, in memory."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.auth: list[str] = []
        self.contexts: list[Any] = []
        self.lifs = [
            {'name': 'lif-b', 'ip': {'address': '192.0.2.2'}},
            {'name': 'lif-a', 'ip': {'address': '192.0.2.1'}},
        ]
        self.volumes: dict[str, dict[str, Any]] = {}
        self.namespaces: dict[str, str] = {}
        self.subsystems: dict[str, dict[str, Any]] = {}
        self.maps: dict[str, str] = {}  # namespace name -> subsystem name
        self.job_state = 'success'

    def urlopen(
        self,
        req: urllib.request.Request,
        context: Any = None,
        timeout: int = 0,
    ) -> Response:
        url = urllib.parse.urlsplit(req.full_url)
        query = dict(urllib.parse.parse_qsl(url.query))
        body = (
            json.loads(req.data)  # type: ignore[arg-type]
            if req.data
            else None
        )
        method = req.get_method()
        path = urllib.parse.unquote(url.path.removeprefix('/api'))
        self.requests.append((method, path, body))
        self.auth.append(req.get_header('Authorization') or '')
        self.contexts.append(context)
        status, result = self.handle(method, path, query, body)
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url,
                status,
                'error',
                {},  # type: ignore[arg-type]
                io.BytesIO(json.dumps(result).encode()),
            )
        return Response(status, result)

    def handle(
        self,
        method: str,
        path: str,
        query: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any] | None]:
        if (method, path) == ('GET', '/svm/svms'):
            return records([{'uuid': SVM_UUID, 'name': query['name']}])
        if (method, path) == ('GET', '/network/ip/interfaces'):
            return records(self.lifs)
        if path == '/storage/volumes':
            if method == 'POST':
                assert body is not None
                self.volumes[body['name']] = body
                return 202, {'job': {'uuid': 'job-1'}}
            vols = [{'name': query['name']}] * (query['name'] in self.volumes)
            return records(vols)
        if path.startswith('/cluster/jobs/'):
            return 200, {'state': self.job_state, 'message': 'no space'}
        if path == '/storage/namespaces':
            if method == 'POST':
                assert body is not None
                self.namespaces[body['name']] = str(uuid.uuid4())
                return 201, None
            name = query['name']
            if name not in self.namespaces:
                return records([])
            return records([{'uuid': self.namespaces[name]}])
        if path == '/protocols/nvme/subsystem-maps':
            if method == 'POST':
                assert body is not None
                self.maps[body['namespace']['name']] = body['subsystem'][
                    'name'
                ]
                return 201, None
            subsys = self.maps.get(query['namespace.name'])
            if subsys is None:
                return records([])
            uid = self.subsystems[subsys]['uuid']
            return records([{'subsystem': {'name': subsys, 'uuid': uid}}])
        if path == '/protocols/nvme/subsystems':
            if method == 'POST':
                assert body is not None
                self.subsystems[body['name']] = {
                    'uuid': str(uuid.uuid4()),
                    'hosts': {h['nqn']: h for h in body['hosts']},
                }
                return 201, None
            name = query['name']
            if name not in self.subsystems:
                return records([])
            return records([{'name': name, **self.subsystems[name]}])
        parts = path.split('/')
        found = [
            n for n, s in self.subsystems.items() if s['uuid'] == parts[4]
        ]
        if not found:
            return 404, {'error': {'message': 'no subsystem'}}
        subsys = found[0]
        hosts = self.subsystems[subsys]['hosts']
        if method == 'DELETE' and len(parts) == 5:
            del self.subsystems[subsys]
            self.maps = {k: v for k, v in self.maps.items() if v != subsys}
            return 200, None
        if method == 'DELETE':
            if parts[6] not in hosts:
                return 404, {'error': {'message': 'no host'}}
            del hosts[parts[6]]
            return 200, None
        assert body is not None
        hosts[body['nqn']] = body
        return 201, None


class OntapTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / 'password').write_text('secret\n')
        self.fake = FakeOntap()
        patch = mock.patch.object(urllib.request, 'urlopen', self.fake.urlopen)
        patch.start()
        self.addCleanup(patch.stop)

    def target(self, **config: Any) -> ontap.OntapTarget:
        cfg = {
            'type': 'ontap',
            'host': '192.0.2.100',
            'user': 'admin',
            'password_file': str(self.tmp / 'password'),
            'svm': 'svm1',
            'aggregate': 'aggr1',
            'cache_dir': str(self.tmp / 'cache'),
            **config,
        }
        tgt = target.create(cfg)
        assert isinstance(tgt, ontap.OntapTarget)
        return tgt

    def subsys(self, **keys: str) -> Subsystem:
        tgt = self.target()
        return Subsystem(NQN, tgt.value('subsys_uuid'), HOSTNQN, **keys)


class TestOntapConfig(OntapTestCase):
    def test_values(self) -> None:
        tgt = self.target()
        self.assertEqual(tgt.value('traddr'), '192.0.2.1')
        self.assertEqual(tgt.value('trsvid'), '4420')
        self.assertEqual(tgt.value('subsysnqn'), NQN)
        self.assertEqual(
            tgt.value('subsys_uuid'), self.fake.namespaces['/vol/blktests/ns1']
        )
        self.assertEqual(
            tgt.features(),
            [
                'list-ports',
                'show-port',
                'set-host',
                'dhchap-sha256',
                'dhchap-sha512',
                'dhchap-null',
                'dhchap-ffdhe2048',
                'dhchap-ffdhe3072',
                'dhchap-ffdhe4096',
                'dhchap-ffdhe6144',
                'dhchap-ffdhe8192',
            ],
        )

    def test_creates_volume_without_snapshots(self) -> None:
        self.target(volume='bt', ns_size='1G').value('subsys_uuid')
        vol = self.fake.volumes['bt']
        self.assertEqual(vol['aggregates'], [{'name': 'aggr1'}])
        self.assertEqual(vol['snapshot_policy'], {'name': 'none'})
        self.assertEqual(vol['space'], {'snapshot': {'reserve_percent': 0}})
        self.assertIn('/vol/bt/ns1', self.fake.namespaces)

    def test_cached(self) -> None:
        self.target().value('subsys_uuid')
        nr = len(self.fake.requests)
        tgt = self.target()
        for name in ['traddr', 'subsysnqn', 'subsys_uuid']:
            tgt.value(name)
        self.assertEqual(len(self.fake.requests), nr)

    def test_request(self) -> None:
        self.target(verify_tls=False).value('traddr')
        auth = base64.b64encode(b'admin:secret').decode()
        self.assertEqual(set(self.fake.auth), {'Basic ' + auth})
        self.assertTrue(all(c is not None for c in self.fake.contexts))
        self.fake.contexts.clear()
        self.target(cache_dir=str(self.tmp / 'c2')).value('traddr')
        self.assertTrue(all(c is None for c in self.fake.contexts))

    def test_job_failure(self) -> None:
        self.fake.job_state = 'failure'
        with self.assertRaisesRegex(ontap.OntapError, 'no space'):
            self.target().value('subsys_uuid')


class TestOntapSubsystems(OntapTestCase):
    def test_setup_cleanup(self) -> None:
        tgt = self.target()
        tgt.setup(self.subsys(hostkey=KEY, ctrlkey=KEY2), 4)
        subsys = self.fake.subsystems['blktests']
        self.assertEqual(
            subsys['hosts'][HOSTNQN]['dh_hmac_chap'],
            {'host_secret_key': KEY, 'controller_secret_key': KEY2},
        )
        self.assertEqual(self.fake.maps, {'/vol/blktests/ns1': 'blktests'})
        self.assertEqual(tgt.list_ports(NQN), ['1', '2'])
        self.assertEqual(
            tgt.show_port('2'), target.Port('tcp', '192.0.2.2', '4420')
        )
        self.assertEqual(tgt.hosts_file.stat().st_mode & 0o777, 0o600)

        tgt.cleanup(NQN)
        self.assertEqual((self.fake.subsystems, self.fake.maps), ({}, {}))
        self.assertEqual(tgt.list_ports(NQN), [])
        self.assertEqual(json.loads(tgt.hosts_file.read_text()), {})

    def test_stale_subsystem(self) -> None:
        tgt = self.target()
        tgt.setup(self.subsys())
        tgt.setup(self.subsys())
        self.assertEqual(list(self.fake.subsystems), ['blktests'])
        self.fake.maps['/vol/blktests/ns1'] = 'other'
        self.fake.subsystems['other'] = {'uuid': 'u', 'hosts': {}}
        with self.assertRaisesRegex(ontap.OntapError, 'mapped to other'):
            tgt.setup(self.subsys())

    def test_unsupported(self) -> None:
        tgt = self.target()
        with self.assertRaisesRegex(ontap.OntapError, 'start with'):
            tgt.setup(Subsystem('nqn.foo', 'u', HOSTNQN))
        with self.assertRaises(NotImplementedError):
            tgt.setup(self.subsys(), tls='required')
        with self.assertRaisesRegex(ontap.OntapError, 'no port 3'):
            tgt.show_port('3')
        with self.assertRaises(NotImplementedError):
            tgt.set_ana('1', 1, 'optimized')


class TestOntapSetHost(OntapTestCase):
    def chap(self) -> dict[str, str]:
        hosts = self.fake.subsystems['blktests']['hosts']
        chap: dict[str, str] = hosts[HOSTNQN]['dh_hmac_chap']
        return chap

    def test_set_host(self) -> None:
        tgt = self.target()
        tgt.setup(self.subsys(hostkey=KEY))
        tgt.set_host(HOSTNQN, {'hash': 'hmac(sha512)', 'dhgroup': 'ffdhe8192'})
        self.assertEqual(
            self.chap(),
            {
                'host_secret_key': KEY,
                'hash_function': 'sha_512',
                'group_size': '8192_bit',
            },
        )
        # A new target, as for the next command, knows the secrets
        self.target().set_host(HOSTNQN, {'hostkey': KEY2, 'ctrlkey': KEY})
        self.assertEqual(self.chap()['host_secret_key'], KEY2)
        self.assertEqual(self.chap()['controller_secret_key'], KEY)
        deletes = [r for r in self.fake.requests if r[0] == 'DELETE']
        self.assertEqual(len(deletes), 2)

    def test_unsupported_hash(self) -> None:
        tgt = self.target()
        tgt.setup(self.subsys(hostkey=KEY))
        with self.assertRaisesRegex(ontap.OntapError, 'hmac.sha384'):
            tgt.set_host(HOSTNQN, {'hash': 'hmac(sha384)'})

    def test_sha384_secret(self) -> None:
        tgt = self.target()
        with self.assertRaisesRegex(ontap.OntapError, 'SHA-384 secrets'):
            tgt.setup(self.subsys(hostkey='DHHC-1:02:abc:'))
        tgt.setup(self.subsys(hostkey=KEY))
        with self.assertRaisesRegex(ontap.OntapError, 'SHA-384 secrets'):
            tgt.set_host(HOSTNQN, {'ctrlkey': 'DHHC-1:02:abc:'})
        # The host is still allowed with the old key
        self.assertEqual(self.chap(), {'host_secret_key': KEY})

    def test_host_already_removed(self) -> None:
        tgt = self.target()
        tgt.setup(self.subsys(hostkey=KEY))
        del self.fake.subsystems['blktests']['hosts'][HOSTNQN]
        tgt.set_host(HOSTNQN, {'hostkey': KEY2})
        self.assertEqual(self.chap(), {'host_secret_key': KEY2})

    def test_http_error(self) -> None:
        tgt = self.target()
        tgt.setup(self.subsys())
        self.fake.subsystems['blktests']['uuid'] = 'gone'
        self.fake.subsystems['x'] = {'uuid': 'other', 'hosts': {}}
        with self.assertRaises(ontap.OntapError) as e:
            tgt._delete_subsystem('missing')
        self.assertEqual(e.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
