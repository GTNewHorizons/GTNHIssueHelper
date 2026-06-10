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
from typing import Optional, Tuple, Generator, Iterable
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
        lines = self.content.splitlines()
        # The throwable starts right after the "Description:" line. Anchoring on it (instead
        # of a fixed line offset) keeps this working when the report is embedded in an fml log,
        # where the blank lines of an on-disk crash report are stripped. Fall back to the old
        # fixed offset if there is no Description line (e.g. an oddly truncated report).
        start = next((i + 1 for i, l in enumerate(lines) if l.startswith('Description:')), 6)
        ret = []
        for line in lines[start:]:
            line = line.strip()
            if not line:
                # a blank line ends the trace, but skip any blanks before it begins
                if ret:
                    break
                continue
            # the report body resumes after the trace; stop before it
            if line.startswith('A detailed walkthrough of the error') or line == '-- System Details --':
                break
            # whitespace is removed by strip(), making it rather hard to read
            if line.startswith('at '):
                ret.append('  ' + line)
            else:
                ret.append(line)
        return ret

    @cached_property
    def cause_stack_trace(self) -> list[str]:
        in_cause = False
        mst = self.main_stack_trace
        ret = []
        for line in mst:
            if in_cause is False and line.startswith('Caused by:'):
                in_cause = True
            if in_cause == True:
                ret.append(line)
        
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

    def has_optifine(self):
        return 'Optifine is installed' in self.content

    def has_angelica(self):
        return any(e.modid == 'angelica' for e in self.mod_list)

    def __hash__(self):
        return hash(self.url)

    def __eq__(self, other):
        return hasattr(other, 'url') and self.url == other.url

    @classmethod
    def from_url(cls, url: str):
        if re.fullmatch(r'https://github.com/[^/]+/GT-New-Horizons-Modpack/files/.+', url):
            return cls(url, requests.get(url).text)


CRASH_REPORT_MARKER = '---- Minecraft Crash Report ----'

# A single fml-*-latest.log line prefix, e.g. "[12:34:56] [Client thread/INFO] [STDOUT/]: "
_LOG_LINE_PREFIX = re.compile(r'^\[\d{2}:\d{2}:\d{2}] \[[^\]]+] \[[^\]]+]:\s?')


def _iter_embedded_crash_reports(content: str, url: str) -> Generator[CrashReport, None, None]:
    """Yield a CrashReport for every crash report embedded in an fml-*-latest.log.

    GTNH dumps the full crash report into the log as plain (unprefixed) lines, so a block
    runs from its '---- Minecraft Crash Report ----' marker up to the next ordinary log line
    (or the start of the next embedded report). A single log can hold more than one crash.
    """
    lines = content.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if CRASH_REPORT_MARKER not in lines[i]:
            i += 1
            continue
        start = i
        i += 1
        while i < len(lines) and CRASH_REPORT_MARKER not in lines[i] \
                and not _LOG_LINE_PREFIX.match(lines[i]):
            i += 1
        blocks.append('\n'.join(lines[start:i]))
    for n, block in enumerate(blocks, 1):
        label = url if len(blocks) == 1 else f'{url} (crash report {n})'
        yield CrashReport(label, block)


def normalize_filename(filename: str) -> str:
    """Canonicalize a mod filename for set-comparison purposes.
    Log uploaders like Prism and mclogs seem to replace + with space
    """
    return filename.replace('+', ' ').strip()


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
        else:
            res['filename'] = cls.fix_curse_filename(res['filename'])
        status = res.pop('status')
        res['errored'] = 'E' in status
        res['disabled'] = 'D' in status
        return cls(**res)

    @classmethod
    def fix_curse_filename(cls, filename: str) -> str:
        # some mods have different filename between assets json and when downloaded by curse/deploader of curse release
        # replace it with the one in manifest
        new_name = filename
        if filename.startswith('Thaumic-Machina'):
            new_name = filename.replace('-', ' ', 1)
        if re.fullmatch(r'CraftPresence-.+\+1\.7\.10\.jar', filename):
            new_name = re.fullmatch(r'CraftPresence-(.+)\+1\.7\.10\.jar', filename).expand(r'CraftPresence-\1-1.7.10.jar')
        if new_name != filename:
            gha_utils.warning(f'Replaced curse file name {filename} with {new_name}')
        return new_name


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
            if miv.side and miv.side in side.valid_mod_sides():
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
        cr_data = self._issue_form_data.get(section_key, '')
        if '---- Minecraft Crash Report ----' in cr_data and 'Is Modded' in cr_data:
            istart = cr_data.find('---- Minecraft Crash Report ----')
            while istart != -1:
                moddedline = cr_data.find('Is Modded', istart)
                iend = cr_data.find('\n', moddedline)
                if iend == -1:
                    iend = len(cr_data)
                ret.append(CrashReport(f'inline {len(ret) + 1}', cr_data[istart:iend]))
                istart = cr_data.find('---- Minecraft Crash Report ----', iend)
        all_urls = set()
        for url in re.findall(r'https?://[^\s<>"\')]+', cr_data):
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
            elif parsed.hostname == 'github.com' and re.fullmatch('/user-attachments/files/[0-9]+/.+',
                                                                  parsed.path):
                if parsed.query:
                    gha_utils.warning(f'Suspicious github.com link: {url}. Not processing this file')
                    continue
                download_url = url
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
            if content.startswith(CRASH_REPORT_MARKER):
                ret.append(CrashReport(url, content))
                continue
            if CRASH_REPORT_MARKER in content:
                # A crash report embedded in a larger file, e.g. an fml-client-latest.log: FML
                # dumps the full report into the log. Detecting it by the marker (rather than by
                # the log line format) is robust to the many logger shapes like [FML/].
                embedded = list(_iter_embedded_crash_reports(content, url))
                if embedded:
                    gha_utils.notice(f'Found {len(embedded)} embedded crash report(s) in {url}.')
                    ret.extend(embedded)
                    continue
            if re.match(r'\[\d{2}:\d{2}:\d{2}] \[[^/]+/(INFO|DEBUG|TRACE|WARN|ERROR)] \[[^\]]+]:', content):
                # looks like an fml log, but with no crash report dumped into it
                gha_utils.notice(f'Found log in {url} but no embedded crash report to analyze.')
                continue
        return list(ret)

    def get_mod_list(self, side: Side) -> list[Tuple[GTNHModInfo, GTNHVersion]]:
        try:
            return get_official_mods(self.modpack_version, side)
        except Exception:
            logging.error('error fetching cr mod list', exc_info=True)
            return []

    @cached_property
    def modpack_version(self):
        v = self._issue_form_data['Your Pack Version'].split()[0].strip().lower()
        gha_utils.debug(f'Parsed modpack version: {v}')
        return v

    def get_mod_filename_set(self, side: Side) -> set[str]:
        return {v.filename for _, v in self.get_mod_list(side)}

    def _filter_missing(self, cr: CrashReport, missing: Iterable[str]) -> list[str]:
        ret = []
        for filename in missing:
            if 'healer' in filename.lower() or 'codechickenlib' in filename.lower():
                # these are unfortunately invisible on the crash report
                # healer did not provide a mod container
                # CCL is a pure library without any entry point
                continue
            if not cr.is_recent_java() and 'lwjgl3ify' in filename.lower():
                # doesn't need it
                continue
            ret.append(filename)
        return ret

    def _iter_likely_dev_jar_mods(self, cr: CrashReport) -> list[str]:
        for e in cr.mod_list:
            if e.filename.endswith('-dev.jar') or e.filename.endswith('-deobf.jar'):
                yield e.filename

    def analyze(self, cr: CrashReport):
        # Lead with a blank line: it closes any preceding HTML block (e.g. the previous
        # report's <details>) so this Markdown heading renders instead of being swallowed
        # as raw text inside that block.
        self._out.append('')
        self._out.append(f'## Primitive Automated Analysis of Crash Report')
        self._out.append(cr.url)

        # early checks. checks for uninteresting CRs and immediately return
        if cr.truncated:
            self._out.append('CRASH REPORT IS TRUNCATED. This will not help to get your problem fixed!!!')
        if cr.main_stack_trace[0] == 'java.lang.NullPointerException' and \
                'cpw.mods.fml.common.network.internal.FMLProxyPacket' in cr.main_stack_trace[1]:
            self._out.append(f'This crash report is near useless. Try post fml-client-latest.log instead.')
            return
        if any('ChunkIOProvider' in e for e in cr.main_stack_trace):
            self._out.append(f'This crash report suggests world corruption. Try restore from a backup.')
            return
        if any(self._iter_likely_dev_jar_mods(cr)):
            self._out.append('Dev jar detected. Use the jar without -dev or -deobf in filename.')
            self._out.extend(self._iter_likely_dev_jar_mods(cr))
            return
        if cr.has_angelica() and cr.has_optifine():
            self._out.append('Both optifine and angelica detected. They are not compatible with each other. Remove either to proceed.')
            return

        # Now some real diagnostics
        if cr.main_stack_trace[0] == 'java.lang.RuntimeException: Chunk build failed':
            self._out.append('Possibly an Angelica problem. Try remove this mod and see if this fixes your problem.')
            formatted_stack_trace = '\n'.join(cr.cause_stack_trace)
        else:
            formatted_stack_trace = '\n'.join(cr.main_stack_trace)
        self._out.append(f'<details><summary>Stacktrace</summary><pre>{formatted_stack_trace}</pre></details>')

        if not self.get_mod_list(cr.side):
            gha_utils.warning(f'Failed to get mod list for side {cr.side}')
            return
        cr_mods = {mod.filename: mod for mod in cr.mod_list}
        base_mods_set = self.get_mod_filename_set(cr.side)
        # Compare on normalized names (so '+' vs space and leading '+' don't create
        # spurious diffs) but keep the originals around for display.
        cr_by_norm = {normalize_filename(f): f for f in cr_mods}
        base_by_norm = {normalize_filename(f): f for f in base_mods_set}
        missing = self._filter_missing(
            cr, sorted(base_by_norm[n] for n in base_by_norm.keys() - cr_by_norm.keys()))
        added = sorted(cr_by_norm[n] for n in cr_by_norm.keys() - base_by_norm.keys())
        if missing:
            self._out.append(f'<details><summary>Missing {len(missing)} mods</summary><ul>')
            for filename in missing:
                self._out.append(f'<li>{filename}</li>')
            self._out.append('</ul></details>')
        if added:
            self._out.append(f'<details><summary>Added {len(added)} mods</summary><ul>')
            for filename in added:
                self._out.append(f'<li>{filename} ({cr_mods[filename].modname})</li>')
            self._out.append('</ul></details>')
        # force a newline between each section
        self._out.append('')

    # A file (e.g. a full fml log) can contain a crash dumped on every retry. Analyze the first
    # few and just tally the rest so the comment stays readable.
    MAX_ANALYZED_REPORTS = 5

    def main(self):
        with gha_utils.group('Checking crash report'):
            if not self.crash_reports:
                return
            self._out.append(f'Found {len(self.crash_reports)} linked crash report(s)')
            for i, cr in enumerate(self.crash_reports[:self.MAX_ANALYZED_REPORTS]):
                try:
                    self.analyze(cr)
                except Exception:
                    logging.error('error analyzing cr %s', i, exc_info=True)
                    continue
            extra = len(self.crash_reports) - self.MAX_ANALYZED_REPORTS
            if extra > 0:
                self._out.append('')
                self._out.append(f'Additional {extra} crash report(s) found in file, not analyzed.')
            if 'GITHUB_OUTPUT' in os.environ:
                with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                    f.write('comments<<__XSAJXAXJIO_EOF\n')
                    f.writelines(l + '\n' for l in self._out)
                    f.write('__XSAJXAXJIO_EOF\n')
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
