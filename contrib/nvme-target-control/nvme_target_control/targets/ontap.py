# SPDX-License-Identifier: GPL-3.0+
# Copyright (c) 2026 SUSE LLC and contributors
"""NetApp ONTAP storage virtual machine (SVM), through the ONTAP REST API.

blktests connects over NVMe/TCP to the LIFs of the SVM. [target] options:

  host           cluster management address
  user           ONTAP user
  password_file  file with the password of user in the first line
  svm            the SVM
  aggregate      aggregate for the volume, needed if it doesn't exist
  volume         volume of the namespace, default 'blktests'
  volume_size    size of the volume if it is created, default 4G
  ns_size        size of the namespace if it is created, default 2G
  verify_tls     verify the certificate of host, default true
  cache_dir      default ~/.cache/nvme-target-control

The first time blktests asks for the subsystem UUID, the target creates the
volume, without snapshots, and the namespace /vol/VOLUME/ns1 unless they
exist, and caches the namespace UUID, the SVM UUID and the LIFs. Setup
creates an NVMe subsystem, allows the host and maps the namespace to it.
Cleanup deletes the subsystem. The volume and namespace stay.

ONTAP derives the subsystem NQN from the subsystem name and the SVM.
'config --show-subsysnqn' shows the NQN of the subsystem 'blktests'. Other
NQNs only work with the NQN prefix of the SVM.

The ports are the NVMe/TCP LIFs, numbered 1..N by LIF name. Every
subsystem is reachable on all of them. Ports can't be added or removed and
ONTAP manages the ANA states. ONTAP can't modify the DH-HMAC-CHAP settings
of a host: set-host removes the host and adds it again, which drops its
connections. The target doesn't set up TLS.
"""

import base64
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..target import Port, Subsystem, Target

NQN_PREFIX = 'nqn.1992-08.com.netapp:sn.{}:subsystem.'
DEF_SUBSYSTEM = 'blktests'

# The names blktests uses for the DH-HMAC-CHAP hash and DH group, and the
# ones ONTAP uses. ONTAP doesn't support SHA-384.
HASHES = {
    'hmac(sha256)': 'sha_256',
    'hmac(sha512)': 'sha_512',
}
DHGROUPS = {
    'null': 'none',
    'ffdhe2048': '2048_bit',
    'ffdhe3072': '3072_bit',
    'ffdhe4096': '4096_bit',
    'ffdhe6144': '6144_bit',
    'ffdhe8192': '8192_bit',
}


class OntapError(OSError):
    """An ONTAP REST request failed, status is its HTTP status."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class OntapTarget(Target):
    """NetApp ONTAP storage virtual machine with NVMe/TCP LIFs."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.svm = str(config['svm'])
        self.volume = str(config.get('volume', 'blktests'))
        self.ns_path = f'/vol/{self.volume}/ns1'
        cache = Path(
            config.get('cache_dir', '~/.cache/nvme-target-control')
        ).expanduser()
        self.cache_file = cache / f'ontap-{config["host"]}-{self.svm}.json'
        # The DH-HMAC-CHAP settings of the hosts, by subsystem name: ONTAP
        # doesn't return the secrets, set_host needs them.
        self.hosts_file = self.cache_file.with_suffix('.hosts.json')

    # REST

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f'https://{self.config["host"]}/api{path}'
        if query:
            url += '?' + urllib.parse.urlencode(query)
        password = (
            Path(self.config['password_file']).read_text().split('\n')[0]
        )
        auth = f'{self.config["user"]}:{password}'.encode()
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                'Authorization': 'Basic ' + base64.b64encode(auth).decode(),
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            },
        )
        ctx = (
            None
            if self.config.get('verify_tls', True)
            else ssl._create_unverified_context()
        )
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
                data = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            with e:
                text = e.read().decode(errors='replace')
            raise OntapError(
                f'{method} {path}: {e.code} {text}', e.code
            ) from None
        result: dict[str, Any] = json.loads(data) if data else {}
        if status == 202 and 'job' in result:
            self._wait_job(result['job']['uuid'])
        return result

    def _wait_job(self, uuid: str) -> None:
        for _ in range(300):
            job = self._request('GET', f'/cluster/jobs/{uuid}')
            if job['state'] == 'success':
                return
            if job['state'] == 'failure':
                raise OntapError(f'job {uuid}: {job.get("message")}')
            time.sleep(1)
        raise OntapError(f'job {uuid} did not finish')

    def _records(
        self, path: str, query: dict[str, str]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = self._request(
            'GET', path, query={'svm.name': self.svm, **query}
        ).get('records', [])
        return records

    # Values blktests asks for many times per test: cached

    def _cache(self) -> dict[str, Any]:
        try:
            cache: dict[str, Any] = json.loads(self.cache_file.read_text())
            return cache
        except (OSError, ValueError):
            pass
        svm = self._request('GET', '/svm/svms', query={'name': self.svm})
        lifs = self._records(
            '/network/ip/interfaces',
            {'services': 'data_nvme_tcp', 'fields': 'name,ip.address'},
        )
        cache = {
            'svm_uuid': svm['records'][0]['uuid'],
            'lifs': [
                lif['ip']['address']
                for lif in sorted(lifs, key=lambda lif: str(lif['name']))
            ],
            'ns_uuid': self._namespace_uuid(),
        }
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps(cache))
        return cache

    def _namespace_uuid(self) -> str:
        """Return the UUID of the namespace, create it first if needed."""
        ns = self._records(
            '/storage/namespaces', {'name': self.ns_path, 'fields': 'uuid'}
        )
        if ns:
            return str(ns[0]['uuid'])
        if not self._records('/storage/volumes', {'name': self.volume}):
            self._request(
                'POST',
                '/storage/volumes',
                {
                    'name': self.volume,
                    'svm': {'name': self.svm},
                    'aggregates': [{'name': self.config['aggregate']}],
                    'size': self.config.get('volume_size', '4G'),
                    # The tests rewrite the namespace: snapshots of it
                    # would fill the volume and fail the writes
                    'snapshot_policy': {'name': 'none'},
                    'space': {'snapshot': {'reserve_percent': 0}},
                },
            )
        self._request(
            'POST',
            '/storage/namespaces',
            {
                'svm': {'name': self.svm},
                'name': self.ns_path,
                'os_type': 'linux',
                'space': {'size': self.config.get('ns_size', '2G')},
            },
        )
        return self._namespace_uuid()

    def traddr(self) -> str:
        return str(self._cache()['lifs'][0])

    def subsysnqn(self) -> str:
        return self._nqn_prefix() + DEF_SUBSYSTEM

    def subsys_uuid(self) -> str:
        return str(self._cache()['ns_uuid'])

    def _nqn_prefix(self) -> str:
        return NQN_PREFIX.format(self._cache()['svm_uuid'].replace('-', ''))

    def _name(self, subsysnqn: str) -> str:
        """Return the subsystem name ONTAP derives subsysnqn from."""
        prefix = self._nqn_prefix()
        if not subsysnqn.startswith(prefix):
            raise OntapError(
                f'{subsysnqn}: ONTAP subsystem NQNs start with {prefix}'
            )
        return subsysnqn[len(prefix) :]

    # Subsystems

    def _subsystem(self, name: str) -> dict[str, Any] | None:
        subsys = self._records('/protocols/nvme/subsystems', {'name': name})
        return subsys[0] if subsys else None

    def _delete_subsystem(self, uuid: str) -> None:
        self._request(
            'DELETE',
            f'/protocols/nvme/subsystems/{uuid}',
            query={
                'allow_delete_while_mapped': 'true',
                'allow_delete_with_hosts': 'true',
            },
        )

    def setup(
        self, subsys: Subsystem, nr_ports: int = 1, tls: str = 'none'
    ) -> None:
        if tls != 'none':
            raise NotImplementedError('OntapTarget does not set up TLS')
        name = self._name(subsys.nqn)
        # A subsystem left by an aborted run would keep the namespace
        stale = self._records(
            '/protocols/nvme/subsystem-maps',
            {
                'namespace.name': self.ns_path,
                'fields': 'subsystem.name,subsystem.uuid',
            },
        )
        for m in stale:
            if not m['subsystem']['name'].startswith(DEF_SUBSYSTEM):
                raise OntapError(
                    f'{self.ns_path} is mapped to {m["subsystem"]["name"]}'
                )
            self._delete_subsystem(m['subsystem']['uuid'])

        chap = {}
        if subsys.hostkey:
            chap['host_secret_key'] = subsys.hostkey
        if subsys.ctrlkey:
            chap['controller_secret_key'] = subsys.ctrlkey
        self._request(
            'POST',
            '/protocols/nvme/subsystems',
            {
                'svm': {'name': self.svm},
                'name': name,
                'os_type': 'linux',
                'hosts': [self._host(subsys.hostnqn, chap)],
            },
        )
        hosts = self._hosts()
        hosts[name] = {subsys.hostnqn: chap}
        self._save_hosts(hosts)
        self._request(
            'POST',
            '/protocols/nvme/subsystem-maps',
            {
                'svm': {'name': self.svm},
                'subsystem': {'name': name},
                'namespace': {'name': self.ns_path},
            },
        )

    def cleanup(self, subsysnqn: str) -> None:
        name = self._name(subsysnqn)
        subsys = self._subsystem(name)
        if subsys:
            self._delete_subsystem(subsys['uuid'])
        hosts = self._hosts()
        if hosts.pop(name, None) is not None:
            self._save_hosts(hosts)

    # Hosts

    @staticmethod
    def _host(hostnqn: str, chap: dict[str, str]) -> dict[str, Any]:
        for field in ('host_secret_key', 'controller_secret_key'):
            # DHHC-1:02: is a secret transformed with SHA-384
            if chap.get(field, '').startswith('DHHC-1:02:'):
                raise OntapError('ONTAP does not support SHA-384 secrets')
        host: dict[str, Any] = {'nqn': hostnqn}
        if chap:
            host['dh_hmac_chap'] = chap
        return host

    def _hosts(self) -> dict[str, dict[str, dict[str, str]]]:
        try:
            hosts: dict[str, dict[str, dict[str, str]]] = json.loads(
                self.hosts_file.read_text()
            )
            return hosts
        except (OSError, ValueError):
            return {}

    def _save_hosts(self, hosts: dict[str, dict[str, dict[str, str]]]) -> None:
        self.hosts_file.parent.mkdir(parents=True, exist_ok=True)
        # The file holds secrets
        self.hosts_file.touch(mode=0o600)
        self.hosts_file.write_text(json.dumps(hosts))

    def set_host(self, hostnqn: str, attrs: dict[str, str]) -> None:
        """Change the DH-HMAC-CHAP settings of the host.

        ONTAP can't modify them: remove the host from the subsystems setup
        created and add it again. This drops the connections of the host
        to these subsystems; the host reconnects.
        """
        fields = {
            'hostkey': ('host_secret_key', None),
            'ctrlkey': ('controller_secret_key', None),
            'hash': ('hash_function', HASHES),
            'dhgroup': ('group_size', DHGROUPS),
        }
        hosts = self._hosts()
        for name, subsys_hosts in hosts.items():
            if hostnqn not in subsys_hosts:
                continue
            # Change a copy: the host keeps its settings if one is invalid
            chap = dict(subsys_hosts[hostnqn])
            for attr, value in attrs.items():
                field, names = fields[attr]
                if not value:
                    chap.pop(field, None)
                elif names is None:
                    chap[field] = value
                elif value in names:
                    chap[field] = names[value]
                else:
                    raise OntapError(f'ONTAP does not support {attr} {value}')
            host = self._host(hostnqn, chap)
            subsys_hosts[hostnqn] = chap
            subsys = self._subsystem(name)
            if not subsys:
                continue
            path = f'/protocols/nvme/subsystems/{subsys["uuid"]}/hosts'
            quoted = urllib.parse.quote(hostnqn, safe='')
            try:
                self._request('DELETE', f'{path}/{quoted}')
            except OntapError as e:
                # Gone if an earlier set-host failed to add it again
                if e.status != 404:
                    raise
            self._request('POST', path, host)
        self._save_hosts(hosts)

    # Ports: the LIFs

    def list_ports(self, subsysnqn: str) -> list[str]:
        if not self._subsystem(self._name(subsysnqn)):
            return []
        return [str(i) for i in range(1, len(self._cache()['lifs']) + 1)]

    def show_port(self, port: str) -> Port:
        lifs = self._cache()['lifs']
        if not port.isdigit() or not 1 <= int(port) <= len(lifs):
            raise OntapError(f'no port {port}')
        return Port('tcp', lifs[int(port) - 1], self.value('trsvid'))
