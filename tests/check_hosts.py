# SPDX-License-Identifier: MPL-2.0
"""Validate the independent, immutable native host acceptance inventory."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ru_time.compatibility import RUNYTE_RANGE
from vendor.application import ReleaseRange


def unique_fields(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError('Duplicate host inventory field: ' + name)
        value[name] = item
    return value


def load(path=None):
    path = Path(path or ROOT / 'tests/runyte-hosts.json')
    if path.stat().st_size > 64 * 1024:
        raise ValueError('Host inventory exceeds 64 KiB')
    inventory = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_fields)
    if not isinstance(inventory, dict) or set(inventory) != {'version', 'hosts'} or type(inventory['version']) is not int or inventory['version'] != 1:
        raise ValueError('Unsupported host inventory')
    hosts = inventory['hosts']
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 16:
        raise ValueError('Native acceptance needs immutable host pins')
    support = ReleaseRange(RUNYTE_RANGE)
    roles = []
    seen = set()
    for host in hosts:
        if not isinstance(host, dict) or set(host) != {'revision', 'host_version', 'mode', 'roles'}:
            raise ValueError('Incomplete host pin')
        revision = host['revision']
        if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision) or len(set(revision)) == 1 or revision in seen:
            raise ValueError('Invalid or duplicate source revision')
        seen.add(revision)
        if not support.contains(host['host_version']):
            raise ValueError('Acceptance host lies outside the authored support range')
        if host['mode'] not in ('exact', 'bootstrap-candidate'):
            raise ValueError('Invalid host construction mode')
        if host['mode'] == 'bootstrap-candidate' and (host['host_version'] != '0.3.0' or len(hosts) != 1):
            raise ValueError('Only the first stable floor may use bootstrap construction')
        if not isinstance(host['roles'], list) or not host['roles'] or any(role not in ('oldest', 'newest') for role in host['roles']):
            raise ValueError('Invalid host acceptance roles')
        roles += host['roles']
    if sorted(roles) != ['newest', 'oldest']:
        raise ValueError('Exactly one oldest and one newest host must be tested')
    oldest = next(host for host in hosts if 'oldest' in host['roles'])
    newest = next(host for host in hosts if 'newest' in host['roles'])
    # The oldest tested host must be the declared floor, not simply the oldest
    # convenient pin. The SDK ordinal excludes prereleases from ordinary ranges.
    if ReleaseRange('=' + oldest['host_version'].split('+', 1)[0]).first != support.first:
        raise ValueError('Oldest native host must equal the authored support floor')
    if ReleaseRange('=' + newest['host_version'].split('+', 1)[0]).first < ReleaseRange('=' + oldest['host_version'].split('+', 1)[0]).first:
        raise ValueError('Newest native host predates the supported floor')
    return hosts


def matrix(hosts):
    rows = []
    for host in hosts:
        # Exercise both Python endpoints on the oldest host; later hosts need
        # the newest interpreter while the default matrix retains the cross-product.
        for version in (['3.10', '3.14'] if 'oldest' in host['roles'] else ['3.14']):
            rows.append({**host, 'python': version})
    return {'include': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    value = json.dumps(matrix(load()), separators=(',', ':'))
    if args.github_output:
        with args.github_output.open('a', encoding='utf-8') as output:
            output.write('matrix=' + value + '\n')
    print(value)


if __name__ == '__main__':
    main()
