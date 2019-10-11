from datetime import datetime
import json
import re

from twisted.internet import defer

from buildbot.plugins import steps, util
from buildbot.process import buildstep, logobserver, results

class MergeTestDriver(buildstep.ShellMixin, buildstep.BuildStep):
    driver = './qa/zcash/full_test_suite.py'

    def __init__(self, **kwargs):
        kwargs['command'] = [self.driver, '--list-stages']
        kwargs['name'] = 'list-stages'
        kwargs = self.setupShellMixin(kwargs)
        buildstep.BuildStep.__init__(self, **kwargs)
        self.observer = logobserver.BufferLogObserver()
        self.addLogObserver('stdio', self.observer)

    def extract_stages(self, stdout):
        stages = []
        for line in stdout.split('\n'):
            stage = str(line.strip())
            if stage:
                stages.append(stage)
        return stages

    @defer.inlineCallbacks
    def run(self):
        # generate the list of stages
        cmd = yield self.makeRemoteShellCommand()
        yield self.runCommand(cmd)

        # if the command passes extract the list of stages
        result = cmd.results()
        if result == util.SUCCESS:
            # create a ShellCommand for each stage and add them to the build
            self.build.addStepsAfterCurrentStep([
                steps.ShellCommand(
                    name=stage,
                    command=[self.driver, stage],
                    env={'PATH': ['${HOME}/venv/bin', '${PATH}']},
                    haltOnFailure=False,
                ) for stage in self.extract_stages(self.observer.getStdout())
            ])

        defer.returnValue(result)

class ExpectedFailuresParser(util.LogLineObserver):
    _passed_re = re.compile(r'^\[  PASSED  \] (\d+) test')
    finished = False

    def outLineReceived(self, line):
        if self.finished:
            return

        m = self._passed_re.search(line.strip())
        if m:
            self.step.passedTestsCount = int(m.group(1))
            self.finished = True

class ExpectedFailuresRunner(buildstep.ShellMixin, buildstep.BuildStep):
    passedTestsCount = -1

    def __init__(self, **kwargs):
        kwargs = self.setupShellMixin(kwargs, prohibitArgs=['command'])
        buildstep.BuildStep.__init__(self, **kwargs)
        self.addLogObserver('stdio', ExpectedFailuresParser())

    @defer.inlineCallbacks
    def run(self):
        cmd = yield self.makeRemoteShellCommand(
            command=[
                'make',
                '-C',
                'src',
                'zcash-gtest-expected-failures',
            ])
        yield self.runCommand(cmd)
        # The command should succeed, and all tests should fail
        if self.passedTestsCount == 0:
            defer.returnValue(results.SUCCESS)
        else:
            defer.returnValue(results.FAILURE)

class PerformanceTestRunner(buildstep.ShellMixin, buildstep.BuildStep):
    def __init__(self, metric, benchmark, args, **kwargs):
        self.metric = metric
        self.benchmark = benchmark
        self.args = list(args)
        kwargs = self.setupShellMixin(kwargs, prohibitArgs=['command'])
        buildstep.BuildStep.__init__(self, **kwargs)

    @defer.inlineCallbacks
    def run(self):
        cmd = yield self.makeRemoteShellCommand(
            command=[
                './qa/zcash/performance-measurements.sh',
                self.metric,
                self.benchmark,
            ]+self.args)
        yield self.runCommand(cmd)
        defer.returnValue(cmd.results())

    def get_base_fields(self):
        return {
            'project': 'Zcash',
            'environment': self.getProperty('workername'),
            'branch': 'master', #self.getProperty('branch'),
            'commitid': self.getProperty('got_revision'),
            'executable': 'zcash',
            'benchmark': self.name,
        }

    def setData(self, data):
        res = self.get_base_fields()
        res.update(self.parseData(data))
        if self.benchmark != 'sleep':
            results = self.getProperty('performance_results', [])
            results.append(res)
            self.setProperty('performance_results', results)

@util.renderer
def getPerfJson(props):
    return json.dumps(props.getProperty('performance_results'))

class TimingParser(util.LogLineObserver):
    content = ''
    parsing = False
    finished = False

    def outLineReceived(self, line):
        if self.finished:
            return

        if self.parsing:
            if ']' in line:
                self.content += line[:line.index(']')+1]
                self.finished = True
                self.parse_json()
            else:
                self.content += line
        elif '[' in line:
            self.content += line[line.index('['):]
            self.parsing = True

    def parse_json(self):
        try:
            time_data = json.loads(self.content)
        except ValueError:
            return
        time_data = [x['runningtime'] for x in time_data]
        if len(time_data) > 0:
            self.step.setData(time_data)

def mean(data):
    """Return the sample arithmetic mean of data."""
    n = len(data)
    if n < 1:
        raise ValueError('mean requires at least one data point')
    return sum(data)/float(n)

def _ss(data):
    """Return sum of square deviations of sequence data."""
    c = mean(data)
    ss = sum((x-c)**2 for x in data)
    return ss

def pstdev(data):
    """Calculates the population standard deviation."""
    n = len(data)
    if n < 2:
        raise ValueError('variance requires at least two data points')
    ss = _ss(data)
    pvar = ss/n # the population variance
    return pvar**0.5

def median(data):
    """Return the median of data."""
    n = len(data)
    index = (n - 1) // 2
    if (n % 2):
        return sorted(data)[index]
    else:
        return mean(sorted(data)[index:index+1])

def lower_quartile(data):
    """Return the weighted lower quartile of data."""
    n = len(data)
    srt = sorted(data)
    index = (n - 1) // 2
    if (n % 2):
        return mean([median(srt[:index-1]), median(srt[:index])])
    else:
        return median(srt[:index])

def upper_quartile(data):
    """Return the weighted upper quartile of data."""
    n = len(data)
    srt = sorted(data)
    index = (n - 1) // 2
    if (n % 2):
        return mean([median(srt[index:]), median(srt[index+1:])])
    else:
        return median(srt[index+1:])

class TimingTestRunner(PerformanceTestRunner):
    def __init__(self, benchmark, args, **kwargs):
        PerformanceTestRunner.__init__(self, 'time', benchmark, args, **kwargs)
        self.addLogObserver('stdio', TimingParser())

    def parseData(self, data):
        return {
            'result_value': median(data),
            'min': min(data),
            'max': max(data),
            'q1': lower_quartile(data),
            'q3': upper_quartile(data),
        }

class MemoryParser(util.LogLineObserver):
    _info_re = re.compile(r'Detailed snapshots: \[(\d+, )*((\d+) \(peak\)).*\]')
    _snapshot_re = re.compile(r'^(\d+)\s+[\d,]+\s+([\d,]+)\s+([\d,]+)')
    peak = None
    finished = False

    def outLineReceived(self, line):
        if self.finished:
            return

        if not self.peak:
            m = self._info_re.search(line.strip())
            if m:
                self.peak = m.groups()[-1]
            return

        m = self._snapshot_re.search(line.strip())
        if m:
            n, total, useful_heap = m.groups()
            if n == self.peak:
                self.finished = True
                self.step.setData(int(total.replace(',', '')))

class MemoryTestRunner(PerformanceTestRunner):
    def __init__(self, benchmark, args, **kwargs):
        PerformanceTestRunner.__init__(self, 'memory', benchmark, args, **kwargs)
        self.addLogObserver('stdio', MemoryParser())

    def parseData(self, data):
        return {
            'units_title': 'Total memory',
            'units': 'Bytes',
            'result_value': data,
        }

class InitialBlockDownloadTimeParser(util.LogLineObserver):
    _start_re = re.compile(r'^([\d-]+ [\d:]+) Zcash version')
    _end_re = re.compile(r'^([\d-]+ [\d:]+) Leaving InitialBlockDownload')
    start = None
    finished = False

    def outLineReceived(self, line):
        if self.finished:
            return

        if not self.start:
            m = self._start_re.search(line.strip())
            if m:
                datestr = m.groups()[0]
                self.start = datetime.strptime(datestr, '%Y-%m-%d %H:%M:%S')
                return

        m = self._end_re.search(line.strip())
        if m:
            self.finished = True
            datestr = m.groups()[0]
            end = datetime.strptime(datestr, '%Y-%m-%d %H:%M:%S')
            self.step.addResult(
                'initialblockdownload-time',
                (end - self.start).total_seconds())
            self.step.stopZcash()

class InitialBlockDownloadTimeRunner(buildstep.ShellMixin, buildstep.BuildStep):
    def __init__(self, datadir, **kwargs):
        self.datadir = datadir
        kwargs['logfiles'] = {'debug': '%s/debug.log' % datadir}
        kwargs['sigtermTime'] = 30
        kwargs = self.setupShellMixin(kwargs, prohibitArgs=['command'])
        buildstep.BuildStep.__init__(self, **kwargs)
        self.addLogObserver('debug', InitialBlockDownloadTimeParser())
        self.stopZcashCalled = False

    @defer.inlineCallbacks
    def run(self):
        cmd = yield self.makeRemoteShellCommand(
            command=['./src/zcashd', '-datadir=%s' % self.datadir])
        yield self.runCommand(cmd)
        if self.stopZcashCalled:
            defer.returnValue(results.SUCCESS)
        else:
            defer.returnValue(cmd.results())

    def addResult(self, name, result):
        res = {
            'project': 'Zcash',
            'environment': self.getProperty('workername'),
            'branch': 'master', #self.getProperty('branch'),
            'commitid': self.getProperty('got_revision'),
            'executable': 'zcash',
            'benchmark': name,
            'result_value': result,
        }
        results = self.getProperty('performance_results', [])
        results.append(res)
        self.setProperty('performance_results', results)

    def stopZcash(self):
        self.stopZcashCalled = True
        self.cmd.interrupt('Finished timing')

class CargoBenchParser(util.LogLineObserver):
    _bench_re = re.compile(r'test\s([^\s]+).+bench:\s+([\d,]+).+\s([\d,]+)')

    def outLineReceived(self, line):
        m = self._bench_re.search(line.strip())
        if m:
            name, median, spread = m.groups()
            self.step.addResult(
                name,
                int(median.replace(',', '')),
                int(spread.replace(',', '')))

class CargoBenchRunner(buildstep.ShellMixin, buildstep.BuildStep):
    def __init__(self, project, executable, args, **kwargs):
        self.project = project
        self.executable = executable
        self.args = list(args)
        kwargs = self.setupShellMixin(kwargs, prohibitArgs=['command'])
        buildstep.BuildStep.__init__(self, **kwargs)
        self.addLogObserver('stdio', CargoBenchParser())

    @defer.inlineCallbacks
    def run(self):
        cmd = yield self.makeRemoteShellCommand(
            command=['cargo', 'bench']+self.args)
        yield self.runCommand(cmd)
        defer.returnValue(cmd.results())

    def addResult(self, name, median, spread):
        res = {
            'project': self.project,
            'environment': self.getProperty('workername'),
            'branch': self.getProperty('branch'),
            'commitid': self.getProperty('got_revision'),
            'executable': self.executable,
            'benchmark': name,
            'units': 'ns/iter',
            'result_value': median,
            'min': median - (spread/2),
            'max': median + (spread/2),
        }
        results = self.getProperty('performance_results', [])
        results.append(res)
        self.setProperty('performance_results', results)

