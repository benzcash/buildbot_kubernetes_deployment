import os
import urlparse

from buildbot.plugins import steps, util

from zcash_helpers import (
    GoodRepo,
    read_or_generate_secret,
)
from zcash_steps import (
    CargoBenchRunner,
    ExpectedFailuresRunner,
    InitialBlockDownloadTimeRunner,
    MemoryTestRunner,
    MergeTestDriver,
    PerformanceTestRunner,
    TimingTestRunner,
    getPerfJson,
)

eu = os.path.expanduser

CODESPEED_URL = 'https://speed.z.cash/'
CODESPEED_CERT = eu('~/speed_z_cash.pem')
CODESPEED_PASS = read_or_generate_secret(eu('~/codespeed.password'), False)

REPOS = []
PROJECT = 'dev-ci-zcash'
for HOST in ['github.com']:
    for USER in ['Electric-Coin-Company', 'daira', 'ebfull', 'nathan-at-least', 'str4d']:
        PATH = '/'.join([USER, PROJECT])
        SSH_PATH = ':'.join([HOST, PATH])
        HTTPS_PATH = '/'.join([HOST, PATH])
        REPOS.append("git@" + SSH_PATH + ".git")
        REPOS.append("https://" + HTTPS_PATH)
        REPOS.append("https://" + HTTPS_PATH + ".git")
DEFAULT_REPOURL = "https://github.com/Electric-Coin-Company/dev-ci-zcash.git"

git_source = GoodRepo(REPOS, DEFAULT_REPOURL)

params_lock = util.WorkerLock("fetch-params",
                              maxCount=1)

def sh(*argv, **kw):
    name = kw.pop('name', os.path.basename(argv[0]))
    haltOnFailure = kw.pop('haltOnFailure', False)
    locks = kw.pop('locks', [])
    assert kw == {}, 'Unexpected keywords: {!r}'.format(kw)
    return steps.ShellCommand(
        name=name,
        description=name,
        command=argv,
        timeout=None,
        haltOnFailure=haltOnFailure,
        locks=locks,
    )

def test_stage(stage_name, **kw):
    name = kw.pop('name', stage_name)
    return sh('./qa/zcash/full_test_suite.py', stage_name, name=name, **kw)

def time(benchmark, *args, **kw):
    name = kw.pop('name', 'time %s' % benchmark)
    assert kw == {}, 'Unexpected keywords: {!r}'.format(kw)
    return TimingTestRunner(
        benchmark,
        args,
        name=name,
        description=name,
        timeout=None,
    )

def memory(benchmark, *args, **kw):
    name = kw.pop('name', 'memory %s' % benchmark)
    assert kw == {}, 'Unexpected keywords: {!r}'.format(kw)
    return MemoryTestRunner(
        benchmark,
        args,
        name=name,
        description=name,
        timeout=None,
    )

def valgrind(benchmark, *args, **kw):
    name = kw.pop('name', 'valgrind %s' % benchmark)
    assert kw == {}, 'Unexpected keywords: {!r}'.format(kw)
    return PerformanceTestRunner(
        'valgrind',
        benchmark,
        args,
        name=name,
        description=name,
        timeout=None,
    )

def asan(stage_name):
    return steps.ShellCommand(
        command=['./qa/zcash/full_test_suite.py', stage_name],
        name=stage_name,
        env={
            'ASAN_OPTIONS': 'symbolize=1:report_globals=1:check_initialization_order=true:detect_stack_use_after_return=true',
            'ASAN_SYMBOLIZER_PATH': util.Property('llvm-symbolizer', default='/usr/bin/llvm-symbolizer-3.5'),
        },
    )

@util.renderer
def nproc(props):
    name = props.getProperty('workername')
    if name in ['macos-0', 'macos-1']:
        return ['gnproc']
    else:
        return ['nproc']

def configure_flags(base_flags):
    @util.renderer
    def inner(props):
        configure_flags = props.getProperty('configure_flags', default=[])
        return ' '.join(base_flags + configure_flags)
    return inner

class ZcashBaseFactory(util.BuildFactory):
    configure_flags = ['--enable-werror']

    def __init__(self):
        util.BuildFactory.__init__(self, [
            steps.Git(
                repourl=git_source,
                mode='incremental',
            ),
            sh('git', 'clean', '-dfx', name='git clean'),
        ])

        self.addStep(steps.SetPropertyFromCommand(command=nproc, property="numcpus"))

        self._addPreBuildSteps()

        self._addBuildSteps()

        # Ensures the worker has the params; usually a no-op
        self.addStep(
            sh('./zcutil/fetch-params.sh', '--testnet',
                haltOnFailure=True,
                locks=[params_lock.access('exclusive')]))

    def _addPreBuildSteps(self):
        pass

    def _addBuildSteps(self):
        self.addStep(
            steps.ShellCommand(
                command=['./zcutil/build.sh', util.Interpolate('-j%(prop:numcpus)s')],
                name='build.sh',
                env={'CONFIGURE_FLAGS': configure_flags(self.configure_flags)},
                haltOnFailure=True,
            ))

class ZcashMergeTestFactory(ZcashBaseFactory):
    def __init__(self):
        ZcashBaseFactory.__init__(self)

        self.addStep(MergeTestDriver())

    def _addPreBuildSteps(self):
        self.addStep(
            steps.PyFlakes(
                command=['pyflakes', 'qa', 'src', 'zcutil'],
                env={'PATH': ['${HOME}/venv/bin', '${PATH}']},
                flunkOnWarnings=True,
                flunkOnFailure=True,
                alwaysRun=True,
            ))

class ZcashProtonMergeTestFactory(ZcashMergeTestFactory):
    def _addBuildSteps(self):
        self.addStep(
            steps.ShellCommand(
                command=['./zcutil/build.sh', '--enable-proton', util.Interpolate('-j%(prop:numcpus)s')],
                name='build.sh',
                env={'CONFIGURE_FLAGS': configure_flags(self.configure_flags)},
                haltOnFailure=True,
            ))

class ZcashExpectedFailuresFactory(ZcashBaseFactory):
    def __init__(self):
        ZcashBaseFactory.__init__(self)

        self.addSteps([
            ExpectedFailuresRunner(
                name='expected failures',
                description='expected failures',
                timeout=None,
            ),
        ])

class ZcashPerformanceFactory(ZcashBaseFactory):
    def __init__(self):
        ZcashBaseFactory.__init__(self)

        self.addSteps([
            steps.FileDownload(mastersrc="/home/bbmaster/block-107134.tar.xz", workerdest="block-107134.tar.xz"),
            sh('wget', '-N', 'https://z.cash/downloads/benchmarks/benchmark-200k-UTXOs.tar.xz', name='download benchmark-200k-UTXOs.tar.xz'),
            time('sleep'),
            time('parameterloading'),
            time('createjoinsplit'),
            time('verifyjoinsplit'),
            time('solveequihash'),
            time('solveequihash', 2, name='time solveequihash 2 threads'),
            time('verifyequihash'),
            time('validatelargetx'),
            time('connectblockslow'),
            time('sendtoaddress', '200k-recv', 0.0009, name='time-sendtoaddress-200k-recv-1'),
            time('sendtoaddress', '200k-recv', 0.0099, name='time-sendtoaddress-200k-recv-10'),
            time('sendtoaddress', '200k-recv', 0.0999, name='time-sendtoaddress-200k-recv-100'),
            time('sendtoaddress', '200k-send', 0.0009, name='time-sendtoaddress-200k-send-1'),
            time('sendtoaddress', '200k-send', 0.0099, name='time-sendtoaddress-200k-send-10'),
            time('sendtoaddress', '200k-send', 0.0999, name='time-sendtoaddress-200k-send-100'),
            time('loadwallet', '200k-recv', name='time-loadwallet-200k-recv'),
            time('listunspent', '200k-recv', name='time-listunspent-200k-recv'),
            memory('sleep'),
            memory('parameterloading'),
            memory('createjoinsplit'),
            memory('verifyjoinsplit'),
            memory('solveequihash'),
            memory('solveequihash', 2, name='memory solveequihash 2 threads'),
            memory('verifyequihash'),
            memory('validatelargetx'),
            memory('connectblockslow'),
            memory('sendtoaddress', '200k-recv', 0.0999, name='memory-sendtoaddress-200k-recv-100'),
            memory('loadwallet', '200k-recv', name='memory-loadwallet-200k-recv'),
            memory('listunspent', '200k-recv', name='memory-listunspent-200k-recv'),
            steps.POST(urlparse.urljoin(CODESPEED_URL, '/result/add/json/'),
                data={'json': getPerfJson},
                auth=('buildbot', CODESPEED_PASS),
                verify=CODESPEED_CERT,
                doStepIf=lambda s: s.getProperty('publish', False),
                hideStepIf=lambda results, s: results==util.SKIPPED,
            ),
        ])

    def _addPreBuildSteps(self):
        self.addSteps([
            steps.FileDownload(
                mastersrc="/home/bbmaster/zcash-librustzcash-alloc.diff",
                workerdest="zcash-librustzcash-alloc.diff"),
            steps.FileDownload(
                mastersrc="/home/bbmaster/librustzcash-alloc.diff",
                workerdest="depends/patches/librustzcash/librustzcash-alloc.diff"),
            steps.ShellCommand(
                command=['patch', '-p1', '-i', 'zcash-librustzcash-alloc.diff'],
                name='Apply librustzcash alloc patch',
                haltOnFailure=True,
            )
        ])

class ZcashInitialBlockDownloadTimeFactory(ZcashBaseFactory):
    def __init__(self):
        ZcashBaseFactory.__init__(self)

        self.addSteps([
            steps.MakeDirectory(
                dir='build/ibd-datadir',
                name='Create datadir',
            ),
            sh('touch', 'ibd-datadir/zcash.conf', name='Create zcash.conf'),
            InitialBlockDownloadTimeRunner(
                'ibd-datadir',
                name='time-InitialBlockDownload',
            ),
            steps.RemoveDirectory(
                dir='build/ibd-datadir',
                name='Remove datadir',
                alwaysRun=True,
            ),
            steps.POST(urlparse.urljoin(CODESPEED_URL, '/result/add/json/'),
                data={'json': getPerfJson},
                auth=('buildbot', CODESPEED_PASS),
                verify=CODESPEED_CERT,
                doStepIf=lambda s: s.getProperty('publish', False),
                hideStepIf=lambda results, s: results==util.SKIPPED,
            ),
        ])

class ZcashCoverageFactory(ZcashBaseFactory):
    configure_flags = [
        '--enable-werror',
        '--enable-lcov',
        '--disable-hardening',
    ]

    def __init__(self):
        ZcashBaseFactory.__init__(self)

        self.addSteps([
            sh('make', 'cov'),
            steps.DirectoryUpload(
                workersrc="./zcash-gtest.coverage",
                masterdest=util.Interpolate("/var/code-coverage/%(prop:buildnumber)s-zcash-gtest.coverage"),
                url=util.Interpolate("https://dev-ci.z.cash/code-coverage/%(prop:buildnumber)s-zcash-gtest.coverage")
            ),
            steps.DirectoryUpload(
                workersrc="./test_bitcoin.coverage",
                masterdest=util.Interpolate("/var/code-coverage/%(prop:buildnumber)s-test_zcash.coverage"),
                url=util.Interpolate("https://dev-ci.z.cash/code-coverage/%(prop:buildnumber)s-test_zcash.coverage")
            ),
            steps.DirectoryUpload(
                workersrc="./total.coverage",
                masterdest=util.Interpolate("/var/code-coverage/%(prop:buildnumber)s-total.coverage"),
                url=util.Interpolate("https://dev-ci.z.cash/code-coverage/%(prop:buildnumber)s-total.coverage")
            ),
            steps.MasterShellCommand("chmod -R 755 /var/code-coverage"),
        ])

class ZcashValgrindFactory(ZcashBaseFactory):
    def __init__(self):
        ZcashBaseFactory.__init__(self)

        self.addSteps([
            steps.FileDownload(mastersrc="/home/bbmaster/block-107134.tar.xz", workerdest="block-107134.tar.xz"),
            valgrind('sleep'),
            valgrind('parameterloading'),
            valgrind('createjoinsplit'),
            valgrind('verifyjoinsplit'),
            valgrind('solveequihash'),
            valgrind('solveequihash', 2, name='valgrind solveequihash 2 threads'),
            valgrind('verifyequihash'),
            valgrind('connectblockslow'),
        ])

class ZcashASanFactory(ZcashBaseFactory):
    configure_flags = [
        '--enable-werror',
        '--enable-asan',
    ]

    def __init__(self):
        ZcashBaseFactory.__init__(self)

        self.addSteps([
            steps.SetPropertyFromCommand(
                command=['find', '/usr', '-name', 'llvm-symbolizer*', '-type', 'f', '-executable'],
                property='llvm-symbolizer',
                name='Find llvm-symbolizer',
            ),
            asan('btest'),
            asan('gtest'),
        ])

class ZcashTSanFactory(ZcashBaseFactory):
    configure_flags = [
        '--enable-werror',
        '--enable-tsan',
    ]

    def __init__(self):
        ZcashBaseFactory.__init__(self)

        self.addSteps([
            test_stage('btest'),
            test_stage('gtest'),
        ])

class ZcashCheckDependsFactory(ZcashBaseFactory):
    def __init__(self):
        ZcashBaseFactory.__init__(self)
        self.addStep(sh('./qa/zcash/test-depends-sources-mirror.py'))

class PairingPerformanceFactory(util.BuildFactory):
    def __init__(self):
        util.BuildFactory.__init__(self, [
            steps.Git(
                repourl='https://github.com/ebfull/pairing.git',
                mode='incremental',
            ),
            sh('git', 'clean', '-dfx', name='git clean'),
            sh('wget', '-N', 'https://static.rust-lang.org/dist/rust-nightly-x86_64-unknown-linux-gnu.tar.gz', name='download latest nightly Rust'),
            sh('tar', 'xzf', 'rust-nightly-x86_64-unknown-linux-gnu.tar.gz', name='extract Rust'),
            sh('./rust-nightly-x86_64-unknown-linux-gnu/install.sh', '--prefix=./rust-nightly', name='install Rust'),
            steps.ShellCommand(
                command=['rustc', '--version'],
                env={'PATH': ['./rust-nightly/bin', '${PATH}']},
                name='rustc version',
            ),
            CargoBenchRunner(
                'Pairing', 'pairing', ['--features', 'u128-support'],
                env={'PATH': ['./rust-nightly/bin', '${PATH}']},
                name='cargo bench',
            ),
            steps.POST(urlparse.urljoin(CODESPEED_URL, '/result/add/json/'),
                data={'json': getPerfJson},
                auth=('buildbot', CODESPEED_PASS),
                verify=CODESPEED_CERT,
                doStepIf=lambda s: s.getProperty('publish', False),
                hideStepIf=lambda results, s: results==util.SKIPPED,
            ),
        ])

class SaplingTestFactory(util.BuildFactory):
    def __init__(self, arch):
        nightly_name = 'rust-nightly-%s' % arch
        nightly_file = '%s.tar.gz' % nightly_name
        util.BuildFactory.__init__(self, [
            steps.Git(
                repourl='https://github.com/zcash-hackworks/sapling.git',
                mode='incremental',
            ),
            sh('git', 'clean', '-dfx', name='git clean'),
            sh('wget', '-N', 'https://static.rust-lang.org/dist/%s' % nightly_file, name='download latest nightly Rust'),
            sh('tar', 'xzf', nightly_file, name='extract Rust'),
            sh('./%s/install.sh' % nightly_name, '--prefix=./rust-nightly', name='install Rust'),
            steps.ShellCommand(
                command=['rustc', '--version'],
                env={'PATH': ['./rust-nightly/bin', '${PATH}']},
                name='rustc version',
            ),
            steps.ShellCommand(
                command=['cargo', 'test', '--release'],
                env={'PATH': ['./rust-nightly/bin', '${PATH}']},
                name='cargo test',
            ),
        ])

