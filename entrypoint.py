import io
import json
import logging
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from functools import lru_cache, cached_property
from logging.config import dictConfig
from typing import Optional, Tuple, Generator
from urllib.parse import urlparse

import github_action_utils as gha_utils
import requests
from gtnh.defs import Side, ModSource
from gtnh.models.available_assets import AvailableAssets
from gtnh.models.gtnh_release import GTNHRelease
from gtnh.models.gtnh_version import GTNHVersion
from gtnh.models.mod_info import GTNHModInfo

dictConfig({
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'basic': {
            'format': '[%(asctime)s] [%(name)s/%(levelname)s] %(message)s',
        },
        'gha': {
            'format': '%(message)s',
        }
    },
    'handlers': {
        'gha': {
            'class': 'log_utils.GHAHandler',
            'formatter': 'gha',
            'level': 'INFO',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'basic',
            'level': 'DEBUG',
        }
    },
    'root': {
        'level': 'DEBUG' if 'RUNNER_DEBUG' in os.environ else 'INFO',
    }
})


class CrashReport:
    def __init__(self, url, content):
        self.url = url
        if callable(content):
            self._content_func = content
        else:
            self._content = content

    @property
    def content(self) -> str:
        if hasattr(self, '_content_func'):
            self._content = self._content_func(self.url)
        return self._content

    @cached_property
    def main_stack_trace(self) -> list[str]:
        ret = []
        for line in self.content.splitlines()[6:]:
            if not line.strip():
                return ret
            ret.append(line.strip())
        return ret

    @cached_property
    def mod_list(self) -> list['InstalledMod']:
        in_list = False
        ret = []
        for line in self.content.splitlines():
            if 'States' in line:
                in_list = True
            elif in_list:
                line = line.strip()
                try:
                    parsed = InstalledMod.parse(line)
                except ValueError:
                    break
                if not parsed:
                    continue
                ret.append(parsed)
        return ret

    @cached_property
    def truncated(self) -> bool:
        return 'Is Modded' not in self.content

    @cached_property
    def java_version(self) -> str | None:
        v = re.findall('Java Version: (\S+),', self.content)
        return v[0].strip() if v else None

    @cached_property
    def side(self) -> Side:
        if 'map_client.txt' in self.content:
            if self.is_recent_java():
                return Side.CLIENT_JAVA9
            return Side.CLIENT
        if 'map_server.txt' in self.content:
            if self.is_recent_java():
                return Side.SERVER_JAVA9
            return Side.SERVER
        # unable to determine side. shouldn't be possible
        return Side.BOTH

    def is_java8(self):
        return self.java_version is not None and self.java_version.startswith('1.8.0')

    def is_recent_java(self):
        # after java 8 they changed to 11.x.x, instead of forever sticking with 1 as major version
        return self.java_version is not None and not self.java_version.startswith('1.')

    def __hash__(self):
        return hash(self.url)

    def __eq__(self, other):
        return hasattr(other, 'url') and self.url == other.url

    @classmethod
    def from_url(cls, url: str):
        if re.fullmatch(r'https://github.com/[^/]+/GT-New-Horizons-Modpack/files/.+', url):
            return cls(url, requests.get(url).text)


def _coremod_filename_convention_modid(match):
    return f'{match.group("modid")}-{match.group("version")}.jar'


coremods = {
    'CodeChickenCore': _coremod_filename_convention_modid,
    'PlayerAPI': _coremod_filename_convention_modid,
}


@dataclass
class InstalledMod:
    modid: str
    version: str
    modname: str
    filename: str
    errored: bool
    disabled: bool

    @classmethod
    def parse(cls, line: str) -> Optional['InstalledMod']:
        match = re.fullmatch(
            r'(?P<status>[ULCHIJADE]+)\s+(?P<modid>[^{ \t][^{]*)\{(?P<version>[^}]+)} \[(?P<modname>.+)] \((?P<filename>.+)\)',
            line)
        if not match:
            raise ValueError(f'invalid line: {line}')
        res = match.groupdict()
        if res['modid'] in ('FML', 'Forge'):
            # lol
            return None
        if res['filename'] == 'minecraft.jar':
            # coremod. guess a true filename
            func = coremods.get(res['modid'])
            if not func:
                # uh, no idea what's this
                return None
            res['filename'] = func(match)
        status = res.pop('status')
        res['errored'] = 'E' in status
        res['disabled'] = 'D' in status
        return cls(**res)


def _iter_nightly_runs(session: requests.Session) -> Generator[dict, None, None]:
    params = {}
    while True:
        res = session.get(
            'https://api.github.com/repos/GTNewHorizons/DreamAssemblerXXL/actions/workflows/58547244/runs',
            params=params
        )
        res.raise_for_status()
        res_json = res.json()
        res.close()
        for run in res_json['workflow_runs']:
            yield run
        if res_json['total_count'] == len(res_json['workflow_runs']):
            return
        params['created'] = f'<{res_json["workflow_runs"][-1]["created_at"]}'


@lru_cache()
def get_assets() -> AvailableAssets:
    if os.path.exists('tmp/assets.json'):
        return AvailableAssets.parse_file('tmp/assets.json')

    res = requests.get('https://raw.githubusercontent.com/GTNewHorizons/DreamAssemblerXXL/master/gtnh-assets.json')
    res.raise_for_status()
    res_json = res.json()
    if not os.environ.get('CI'):
        with open(os.path.join('tmp/assets.json'), 'w') as f:
            json.dump(res_json, f)
    return AvailableAssets.parse_obj(res_json)


@lru_cache()
def get_official_mods(version: str, side: Side) -> list[tuple[GTNHModInfo, GTNHVersion]]:
    manifest = get_manifest(version)
    assets = get_assets()
    mods = []
    for vals, source in ((manifest.external_mods, ModSource.other), (manifest.github_mods, ModSource.github)):
        for modid, miv in vals.items():
            if miv.side and side in miv.side.valid_mod_sides():
                mods.append(assets.get_mod_and_version(modid, miv, (miv.side or Side.CLIENT).valid_mod_sides(), source))
    return mods


def get_manifest(version: str) -> GTNHRelease | None:
    if 'nightly' in version:
        res = re.findall(r'nightly.*(\d+)', version)
        if not res:
            raise Exception('Unrecognizable nightly version')
        sequence = int(res[0])
        with requests.Session() as session:
            if 'GITHUB_TOKEN' in os.environ:
                session.headers['Authorization'] = f'Bearer {os.environ["GITHUB_TOKEN"]}'
            session.headers.update({
                'Accept': 'application/vnd.github.v3+json',
                'x-github-api-version': '2022-11-28'
            })
            for run in _iter_nightly_runs(session):
                if run['run_number'] < sequence:
                    raise Exception(f'Could not find user supplied nightly version {sequence}')
                if run['run_number'] == sequence:
                    break
            else:
                raise Exception(f'Could not find user supplied nightly version {sequence}')

            res = session.get(
                f'https://api.github.com/repos/GTNewHorizons/DreamAssemblerXXL/actions/runs/{run["id"]}/artifacts')
            for artifact in res.json()['artifacts']:
                if 'manifest' in artifact['name']:
                    break
            else:
                raise Exception(f'Could not find manifest artifact in nightly version {sequence}')
            if artifact['expired']:
                raise Exception('Nightly is too ancient')
            manifest_bytes = res.content
            return GTNHRelease.parse_raw(zipfile.ZipFile(io.BytesIO(manifest_bytes)).read('nightly.json'))
    else:
        res = requests.get(
            f'https://raw.githubusercontent.com/GTNewHorizons/DreamAssemblerXXL/master/releases/manifests/{version.strip()}.json')
        if res.status_code != 200:
            res = requests.get(
                f'https://raw.githubusercontent.com/GTNewHorizons/DreamAssemblerXXL/master/releases/manifests/old/{version.strip()}.json')
            res.raise_for_status()
        return GTNHRelease.parse_obj(res.json())


class Helper:
    def __init__(self, issue_form_data: dict, sections: list[str]):
        self._issue_form_data = issue_form_data
        self._sections = sections
        self._out = []

    @cached_property
    def crash_reports(self) -> list[CrashReport]:
        ret = []
        for section in self._sections:
            ret.extend(self._search_section(section))
        return ret

    def _search_section(self, section_key: str) -> list[CrashReport]:
        ret = []
        cr_data = self._issue_form_data[section_key]
        if '---- Minecraft Crash Report ----' in cr_data and 'Is Modded' in cr_data:
            istart = cr_data.find('---- Minecraft Crash Report ----')
            while istart != -1:
                moddedline = cr_data.find('Is Modded', istart)
                iend = cr_data.find('\n', moddedline)
                if iend == -1:
                    iend = len(cr_data)
                ret.append(CrashReport(f'inline {len(ret) + 1}', cr_data[istart:iend]))
                istart = cr_data.find('---- Minecraft Crash Report ----', istart)
        all_urls = set()
        for x in re.findall(r'(https://\S+)|\[[^]]+]\((https://\S+)\)', cr_data):
            url = x[0] or x[1]
            if url in all_urls:
                gha_utils.debug(f'Duplicate url: {url}')
                continue
            all_urls.add(url)
            download_url = None
            parsed = urlparse(url)
            if parsed.hostname == 'pastebin.com':
                if parsed.query or not parsed.path or '/' in parsed.path[1:]:
                    gha_utils.warning(f'Suspicious pastebin.com link: {url}. Not processing this file')
                    continue
                download_url = parsed.scheme + '://' + parsed.hostname + '/raw' + parsed.path
            elif parsed.hostname == 'github.com' and re.fullmatch('/[^/]+/GT-New-Horizons-Modpack/files/.+',
                                                                  parsed.path):
                if parsed.query:
                    gha_utils.warning(f'Suspicious github.com link: {url}. Not processing this file')
                    continue
                download_url = url
            elif parsed.hostname == 'gist.github.com':
                gha_utils.warning(f'Gist API not implemented for {url}. Not processing this file')
                continue
            elif parsed.hostname == 'paste.ee':
                if parsed.query or not re.fullmatch('/p/[^/]+/.+', parsed.path):
                    gha_utils.warning(f'Suspicious paste.ee link: {url}. Not processing this file')
                    continue
                download_url = parsed.scheme + '://' + parsed.hostname + '/d' + parsed.path[2:]
            elif parsed.hostname == 'mclo.gs':
                if parsed.query or not parsed.path or '/' in parsed.path[1:]:
                    gha_utils.warning(f'Suspicious mclo.gs link: {url}. Not processing this file')
                    continue
                download_url = 'https://api.mclo.gs/1/raw' + parsed.path
            elif parsed.hostname == 'paste.ubuntu.com':
                self._out.append(
                    'Please refrain from posting crash reports to paste.ubuntu.com. They might require login to be viewed and our automated analysis tool (like this) cannot read their content.')
                continue
            if not download_url:
                gha_utils.notice('Unknown url. Probably ')
                continue
            req = requests.get(download_url)
            if req.status_code != 200:
                gha_utils.warning(f'Failed to download url: {download_url}. Original file link: {url}')
                self._out.append(f'Failed to download url: {download_url}. Original file link: {url}')
                continue
            content = req.text
            if content.startswith('---- Minecraft Crash Report ----'):
                ret.append(CrashReport(download_url, content))
                continue
            if re.match(r'\[\d{2}:\d{2}:\d{2}] \[[^/]+/(INFO|DEBUG|TRACE|WARN|ERROR)] \[.+/.+]:', content):
                # probably a fml-client-latest.log
                gha_utils.notice(f'Found potential log in {url}. Parsing not implemented for now.')
                continue
        return list(ret)

    def get_mod_list(self, side: Side) -> list[Tuple[GTNHModInfo, GTNHVersion]]:
        try:
            return get_official_mods(self._issue_form_data['Your Pack Version'].split()[0].strip().lower(), side)
        except Exception:
            logging.error('error fetching cr mod list', exc_info=True)
            return []

    def get_mod_filename_set(self, side: Side) -> set[str]:
        return {v.filename for _, v in self.get_mod_list(side)}

    def analyze(self, cr: CrashReport):
        self._out.append(f'# Primitive Automated Analysis of Crash Report {cr.url}')

        # early checks. checks for uninteresting CRs and immediately return
        if cr.truncated:
            self._out.append('CRASH REPORT IS TRUNCATED. This will not help to get your problem fixed!!!')
        if cr.main_stack_trace[:2] == ['java.lang.NullPointerException',
                                       'at cpw.mods.fml.common.network.internal.FMLProxyPacket.func_148833_a(FMLProxyPacket.java:101)']:
            self._out.append(f'This crash report is near useless. Try post fml-client-latest.log instead.')
            return
        if any('ChunkIOProvider' in e for e in cr.main_stack_trace):
            self._out.append(f'This crash report suggests world corruption. Try restore from a backup.')
            return

        # Now some real diagnostics
        if cr.main_stack_trace[0] == 'java.lang.RuntimeException: Chunk build failed':
            self._out.append('Possibly an Angelica problem. Try remove this mod and see if this fixes your problem.')
        formatted_stack_trace = '\n'.join(cr.main_stack_trace)
        self._out.append(f'<details><summary>Stacktrace</summary>{formatted_stack_trace}</details>')

        if not self.get_mod_list(cr.side):
            return
        cr_mods = {mod.filename: mod for mod in cr.mod_list}
        cr_mods_set = set(cr_mods.keys())
        base_mods_set = self.get_mod_filename_set(cr.side)
        missing = base_mods_set - cr_mods_set
        added = cr_mods_set - base_mods_set
        if missing:
            self._out.append(f'<details><summary>Missing mods</summary>')
            for filename in missing:
                if 'healer' in filename.lower() or 'codechickenlib' in filename.lower():
                    # these are unfortunately invisible on the crash report
                    # healer did not provide a mod container
                    # CCL is a pure library without any entry point
                    continue
                if not cr.is_recent_java() and 'lwjgl3ify' in filename.lower():
                    # doesn't need it
                    continue
                self._out.append(f'* {filename}')
            self._out.append('</details>')
        if added:
            self._out.append(f'<details><summary>Added mods</summary>')
            for filename in added:
                self._out.append(f'* {filename} ({cr_mods[filename].modname})')
            self._out.append('</details>')

    def main(self):
        with gha_utils.group('Checking crash report'):
            if not self.crash_reports:
                return
            self._out.append(f'Found {len(self.crash_reports)} linked crash report(s)')
            for i, cr in enumerate(self.crash_reports):
                try:
                    self.analyze(cr)
                except Exception:
                    logging.error('error analyzing cr %s', i, exc_info=True)
                    continue
            if 'GITHUB_OUTPUT' in os.environ:
                gha_utils.set_output('comments', '\n'.join(self._out))
            else:
                for line in self._out:
                    print(line)


def _main():
    try:
        issue_form_data = json.loads(gha_utils.get_user_input("formdata"))
    except (json.JSONDecodeError, TypeError) as ex:
        gha_utils.error('Unable to parse formdata input: ' + str(ex))
        sys.exit(1)
    else:
        sections = gha_utils.get_user_input("sections") or 'Crash Report'
        Helper(issue_form_data, sections.split(',')).main()


if __name__ == '__main__':
    _main()
