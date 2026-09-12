#!/usr/bin/env python3
"""Install a bounded local Ubuntu 24.04/Python 3.12 GIS runtime, without root.

The normal pipeline reuses an already compatible environment. This optional
fallback extracts official Ubuntu packages and PyPI wheels into the accounted
project tree. It does not invoke package maintainer scripts or change the host.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import subprocess
import sys
import tarfile
import urllib.request
import urllib.parse
from datetime import timedelta
from email.utils import parsedate_to_datetime

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))


def package_plan():
    if sys.version_info[:2] != (3, 12) or 'VERSION_ID="24.04"' not in Path('/etc/os-release').read_text():
        raise RuntimeError('This optional bootstrap is only for Ubuntu 24.04 / Python 3.12; reuse a compatible GDAL environment elsewhere')
    command = ['apt-get', '--print-uris', '--yes', '--download-only', '--no-install-recommends',
               'install', 'python3-gdal', 'gdal-bin']
    text = subprocess.check_output(command, text=True)
    packages = []
    for line in text.splitlines():
        match = re.match(r"'([^']+)' (\S+) (\d+) MD5Sum:([a-f0-9]+)", line)
        if match:
            url, name, size, md5 = match.groups()
            if not url.startswith('http://archive.ubuntu.com/ubuntu/'):
                raise RuntimeError('Unreviewed native package host')
            packages.append(dict(url=url.replace('http:', 'https:', 1), name=name, bytes=int(size), publisher_md5=md5))
    if sum(p['bytes'] for p in packages) > 80_000_000:
        raise RuntimeError('Native dependency archive plan changed beyond 80 MB cap')
    # Snapshot URLs preserve the exact cached package versions after a live
    # archive removes superseded files. No apt update, package replacement, or
    # weakened checksum is performed. The snapshot date follows the cached
    # official Ubuntu release metadata used for this dependency resolution.
    dates = []
    for name in ['archive.ubuntu.com_ubuntu_dists_noble-updates_InRelease',
                 'security.ubuntu.com_ubuntu_dists_noble-security_InRelease']:
        path = Path('/var/lib/apt/lists') / name
        if path.exists():
            match = re.search(r'^Date: (.+)$', path.read_text(), re.M)
            if match:
                dates.append(parsedate_to_datetime(match[1]))
    if not dates:
        raise RuntimeError('Official cached InRelease dates are needed for the immutable fallback')
    snapshot = (max(dates) + timedelta(days=1)).strftime('%Y%m%dT000000Z')
    selectors = []
    for item in packages:
        package, version, architecture = urllib.parse.unquote(item['name']).rsplit('_', 2)
        item.update(package=package, version=version, snapshot_id=snapshot,
                    snapshot_url=item['url'].replace('https://archive.ubuntu.com/ubuntu/',
                                                    f'https://snapshot.ubuntu.com/ubuntu/{snapshot}/', 1))
        selectors.append(f'{package}={version}')
    metadata = subprocess.check_output(['apt-cache', 'show', *selectors], text=True)
    if len(metadata.encode()) > 2_000_000:
        raise RuntimeError('Cached dependency metadata exceeds bounded inspection limit')
    inspected = {}
    for paragraph in metadata.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines()
                      if ': ' in line and not line.startswith(' '))
        if {'Package', 'Version', 'SHA256', 'Size', 'MD5sum', 'Filename'} <= fields.keys():
            inspected[(fields['Package'], fields['Version'])] = fields
    for item in packages:
        fields = inspected.get((item['package'], item['version']))
        if (not fields or int(fields['Size']) != item['bytes'] or fields['MD5sum'] != item['publisher_md5']
                or urllib.parse.unquote(item['url']).split('/ubuntu/', 1)[1] != fields['Filename']):
            raise RuntimeError('APT cached SHA256 record disagrees with the pinned dependency plan')
        item['publisher_sha256'] = fields['SHA256']
        item['metadata_provenance'] = 'Existing APT cached Ubuntu package metadata; no system index update'
    return packages


def acquire_native(item, dependencies, budget):
    """Use one documented immutable mirror only when the live object is missing."""
    from seoul_visibility.acquisition_safety import AcquisitionError, guarded_download

    original = dependencies / 'archives' / item['name']
    snapshot = dependencies / 'archives' / ('snapshot-' + item['snapshot_id']) / item['name']
    def verify_deb(candidate):
        with candidate.open('rb') as stream:
            if hashlib.file_digest(stream, 'md5').hexdigest() != item['publisher_md5']:
                raise RuntimeError('Publisher native package MD5 mismatch; preserved')
    def download(url, path, host):
        return guarded_download(url, path, budget, max_bytes=item['bytes'], expected_size=item['bytes'],
            expected_sha256=item['publisher_sha256'], allowed_hosts={host}, magic=b'!<arch>\n',
            validator=verify_deb, max_retries=2)
    if snapshot.exists() and not original.exists():
        transfer = download(item['snapshot_url'], snapshot, 'snapshot.ubuntu.com')
        return snapshot, dict(transfer=transfer, original_url=item['url'], snapshot_url=item['snapshot_url'],
                             fallback_reason='Reuse previously verified immutable mirror object',
                             pinned_version_unchanged=True)
    try:
        transfer = download(item['url'], original, 'archive.ubuntu.com')
        return original, dict(transfer=transfer, pinned_version_unchanged=True)
    except AcquisitionError as error:
        if error.status != 'missing_source':
            raise
        transfer = download(item['snapshot_url'], snapshot, 'snapshot.ubuntu.com')
        return snapshot, dict(transfer=transfer, original_url=item['url'], snapshot_url=item['snapshot_url'],
            fallback_reason=str(error), pinned_version_unchanged=True,
            publisher_sha256_unchanged=item['publisher_sha256'], publisher_md5_unchanged=item['publisher_md5'],
            documentation='https://snapshot.ubuntu.com/')


def wheel_plan(name, version):
    # Bounded metadata probe, no package execution and no dependency resolver cache.
    url = f'https://pypi.org/pypi/{name}/{version}/json'
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise RuntimeError('PyPI metadata exceeds bound')
    values = json.loads(data)['urls']
    matches = [v for v in values if 'cp312-cp312' in v['filename'] and
               'manylinux' in v['filename'] and 'x86_64' in v['filename']]
    if len(matches) != 1:
        raise RuntimeError(f'Expected one compatible public wheel for {name} {version}')
    value = matches[0]
    return dict(name=value['filename'], url=value['url'], bytes=value['size'],
                publisher_sha256=value['digests']['sha256'], metadata_bytes=len(data))


def extract_deb(archive, destination, budget):
    # dpkg-deb only decodes the data archive; no installation or maintainer scripts.
    maximum = 256_000_000
    with budget.reserve(int(maximum * 1.25), temporary_bytes=maximum, label='native dependency extraction') as reservation:
        process = subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(archive)], stdout=subprocess.PIPE)
        count = 0
        credit = 0
        def before_write(size, target):
            nonlocal credit
            size += 8192
            if credit < size:
                credit = max(size, 4 * 1024**2)
                reservation.check_write(credit, target)
            credit -= size
        try:
            with tarfile.open(fileobj=process.stdout, mode='r|') as source:
                for member in source:
                    relative = PurePosixPath(member.name)
                    if relative.is_absolute() or '..' in relative.parts:
                        raise RuntimeError('Unsafe native archive path')
                    target = destination / str(relative)
                    if member.issym() and target.is_symlink():
                        budget.safe_path(target.parent)
                        if os.readlink(target)!=member.linkname or not target.resolve().is_relative_to(destination.resolve()):
                            raise RuntimeError('Existing native symlink differs from verified archive; preserved')
                        continue
                    target = budget.safe_path(target)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if member.issym():
                        link = Path(member.linkname)
                        if link.is_absolute() or not (target.parent / link).resolve().is_relative_to(destination.resolve()):
                            raise RuntimeError('Unsafe native package symlink')
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if not target.exists() and not target.is_symlink():
                            target.symlink_to(member.linkname)
                        continue
                    if not member.isfile() or member.size > maximum or count + member.size > maximum:
                        raise RuntimeError('Native archive type/expansion cap rejected')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source_file = source.extractfile(member)
                    if target.exists():
                        # Packages share doc/directories; preserve any incompatible preexisting file.
                        with target.open('rb') as existing:
                            while block := source_file.read(1024 * 1024):
                                if existing.read(len(block)) != block:
                                    raise RuntimeError('Existing native dependency differs; preserved')
                            if existing.read(1):
                                raise RuntimeError('Existing native dependency has trailing content')
                        count += member.size
                        continue
                    partial = target.with_name(target.name + '.part')
                    actual = 0
                    if partial.exists():
                        # Whole archive has just passed its pinned publisher MD5.
                        # Resume only a byte-identical member prefix; no deletion.
                        with partial.open('rb') as previous:
                            while block := previous.read(1024 * 1024):
                                if source_file.read(len(block)) != block:
                                    raise RuntimeError('Interrupted native member differs from verified archive; preserved')
                                actual += len(block); count += len(block)
                    with partial.open('ab' if partial.exists() else 'xb') as output:
                        while block := source_file.read(1024 * 1024):
                            if count + len(block) > maximum or actual + len(block) > member.size:
                                raise RuntimeError('Native actual expansion exceeds bounded plan')
                            before_write(len(block), partial)
                            output.write(block)
                            count += len(block)
                            actual += len(block)
                    if actual != member.size:
                        raise RuntimeError('Truncated native archive member')
                    partial.chmod(member.mode & 0o755)
                    partial.replace(target)
            # Streaming tar stops at its end marker before dpkg-deb necessarily
            # finishes writing record padding. Drain a bounded zero-only tail
            # before waiting, so a full stdout pipe cannot deadlock the child.
            tail = process.stdout.read(1024 * 1024 + 1)
            if len(tail) > 1024 * 1024 or tail.strip(b'\x00'):
                raise RuntimeError('Unexpected native archive trailing data')
            if process.wait(timeout=30):
                raise RuntimeError('Native archive decoder failed')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return count


def main():
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--component',choices=['all','native','python'],default='all')
    args = parser.parse_args()
    from seoul_visibility.acquisition_safety import Budget, atomic_json, guarded_download, safe_extract_zip
    data = ROOT / 'data/citywide'
    budget = Budget(ROOT, stage_root=data / 'staging',additional_accounted_bytes=5*1024**2)
    budget.check(additional=1_073_741_824, temporary=300_000_000)
    native = package_plan()
    wheels = ([wheel_plan('pyproj', '3.7.2'), wheel_plan('osmium', '4.1.1'), wheel_plan('scipy', '1.15.3')]
              if args.component in {'all', 'python'} else [])
    record = dict(native=native, wheels=wheels, incremental_peak_limit=1_073_741_824,
                  native_per_archive_expansion_limit=256_000_000,
                  method='Official Ubuntu native GDAL + Python bindings, paired NumPy 1.26.4; local extraction only',
                  attribution='APT cached distribution metadata MD5 and SHA256; PyPI published wheel SHA256',
                  immutable_mirror_policy='Only missing live package files may use official snapshot.ubuntu.com; exact package version/size/digests remain pinned')
    print(json.dumps(record, indent=2), flush=True)
    if not args.execute:
        return
    deps = data / 'dependencies'
    for directory in (deps, data / 'staging/tmp', data / 'staging/cache'):
        directory.mkdir(parents=True, exist_ok=True)
    atomic_json(deps / 'plan.json', record, budget=budget)
    for item in native if args.component in {'all','native'} else []:
        print(json.dumps({'stage': 'native_dependency', 'package': item['name'], 'event': 'verify_or_acquire'}), flush=True)
        path, acquisition = acquire_native(item, deps, budget)
        item['acquisition'] = acquisition
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'md5').hexdigest()
        if actual != item['publisher_md5']:
            raise RuntimeError('Publisher native package MD5 mismatch; preserved')
        extract_deb(path, deps / 'native', budget)
        atomic_json(deps / 'native-source-records.json', native, budget=budget)
        print(json.dumps({'stage': 'native_dependency', 'package': item['name'], 'event': 'verified_extracted'}), flush=True)
    for item in wheels if args.component in {'all','python'} else []:
        path = deps / 'archives' / item['name']
        guarded_download(item['url'], path, budget, max_bytes=item['bytes'], expected_size=item['bytes'],
                         expected_sha256=item['publisher_sha256'], allowed_hosts={'files.pythonhosted.org'}, magic=b'PK')
        safe_extract_zip(path, deps / 'site', budget, max_expanded_bytes=150_000_000)
    record['final_storage'] = budget.snapshot()
    atomic_json(deps / f'installed.{args.component}.json', record, budget=budget)


if __name__ == '__main__':
    main()
