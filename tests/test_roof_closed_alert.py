"""Closed observatory roof Slack alert: roof_closed_alert.sh as sourced by
autoprocess.sh, the autoprocess.sh run around it, and the ROOF CLOSED row of
the combine_reports.sh summary page.

Nothing here can reach Slack or any other host: every test that could post
puts a fake curl first in PATH (it records its arguments and answers "ok"),
and SLACK_WEBHOOK_URL_TEAM points at a loopback dummy that nothing listens on.
"""
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import types
import zipfile

import pytest


REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / 'roof_closed_alert.sh'
DUMMY_WEBHOOK = 'http://127.0.0.1:9/unmw-test-slack'
CAMERA = 'TTUQ2b1x1'
FIELD = 'Pup-02-Q2b1x1'
UPLOAD_NAME = '2026-11-12_CI_Pup-02-Q2b1x1_025654_TTUQ2b1x1.zip'
FITS_STUB = b'SIMPLE  = T' + b' ' * 2870
CURL_FAILURE = 'curl: (7) Failed to connect to 127.0.0.1 port 9 after 0 ms: Could not connect to server'


def jd_hours_ago(hours):
    return time.time() / 86400.0 + 2440587.5 - hours / 24.0


def vast_date_time(jd):
    """The date and time as VaST prints them in a 'Last  image:' line: the start
    of a 20 s exposure whose middle is the JD."""
    moment = datetime.datetime.fromtimestamp(round((jd - 2440587.5) * 86400.0 - 10.0, 3), tz=datetime.timezone.utc)
    return moment.strftime('%d.%m.%Y'), moment.strftime('%H:%M:%S.%f')[:12]


def report_page(processing_log, filtering_log, field=FIELD, camera=CAMERA):
    """A transient_report/index.html with the markers VaST writes, in VaST's order."""
    return ("<HTML>\n<HEAD><title>NMW</title></HEAD>\n<BODY>\n"
            "Processing fields {field} <br>\n"
            "<H2>Processing complete!</H2>\n"
            "<a href=\"javascript:toggleElement('processing_log')\"><H3>Processing log:</H3></a>\n"
            "<div id=\"processing_log\" style=\"display:none\">\n<pre class='folding-pre'>\n"
            "{processing_log}</pre>\n</div>\n"
            "<a href=\"javascript:toggleElement('filtering_log')\"><H3>Filtering log:</H3></a>\n"
            "<div id=\"filtering_log\">\n<pre class='folding-pre'>\n"
            "######## Processing parameters ########\n"
            "AAVSO_COMMENT_STRING= NMW-TexasTech Camera-2 135mm f/2.2 telephoto lens + QHY600M CMOS\n"
            "CAMERA_SETTINGS= {camera}\n"
            "#######################################\n"
            "{filtering_log}"
            "Found 0 unidentified candidates (excluding asteroids, hot pixels and known variable stars).\n"
            "</pre>\n</div>\n</BODY></HTML>\n").format(
                field=field, camera=camera, processing_log=processing_log, filtering_log=filtering_log)


def closed_roof_report(jd, field=FIELD, camera=CAMERA):
    date, clock = vast_date_time(jd)
    return report_page(
        '***** ACTION NEEDED - OBSERVATORY ROOF CLOSED? *****   IMAGE PROCESSING ERROR (no stars and no sky '
        'light on the latest new image - camera {camera} keeps imaging with the roof closed) \n'
        '###################################################################################\n'.format(camera=camera),
        'Last  image: {jd:.8f} {date} {clock} UTC\n'
        'ERROR: OBSERVATORY ROOF CLOSED? No stars and no sky light on the latest new image '
        '{field}_2026-11-12_02-48-08_20.00sec_-0.00C_LIGHT_0326.fits of the field {field} '
        '(camera {camera}, 2 of 2 new images are dark)\n'.format(jd=jd, date=date, clock=clock, field=field,
                                                                 camera=camera),
        field=field, camera=camera)


def ok_report(jd, field=FIELD, camera=CAMERA):
    """A successful run: 'Last  image' is the newest image of the run (vast_summary.log)."""
    date, clock = vast_date_time(jd)
    return report_page(
        'Images processed 4\nImages used for photometry 4\n'
        'Last  image: {jd:.5f} {date} {clock}   fd_{field}_2026-11-12_03-48-08_20.00sec_-0.00C_LIGHT_0330.fits\n'
        .format(jd=jd, date=date, clock=clock[:8], field=field),
        'Truncated list of TOCP transients from http://www.cbat.eps.harvard.edu/unconf/tocp.html :\n',
        field=field, camera=camera)


def write_executable(path, body):
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o700)


def install_fake_curl(bin_dir, log, fail_flag):
    """curl stand-in: appends its arguments to log as one JSON line, answers 'ok'
    like Slack, or fails like an unreachable host while fail_flag exists."""
    bin_dir.mkdir(exist_ok=True)
    write_executable(bin_dir / 'curl', (
        'python3 -c \'import json, sys; open(sys.argv[1], "a").write(json.dumps(sys.argv[2:]) + "\\n")\' {log} "$@"\n'
        'if [ -f {flag} ]; then\n echo {failure} >&2\n exit 7\nfi\n'
        "printf ok").format(log=shlex.quote(str(log)), flag=shlex.quote(str(fail_flag)),
                            failure=shlex.quote(CURL_FAILURE)))


def read_posts(log):
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def slack_text(post):
    return json.loads(post[post.index('--data') + 1])['text']


def expected_text(jd, results_url, field=FIELD, camera=CAMERA):
    date, clock = vast_date_time(jd)
    return (':warning: *Observatory roof appears to be CLOSED* - camera *{camera}*: no stars and no sky light '
            'on the latest new image of {field} taken {date} {clock} UTC, but the camera keeps imaging (NINA may not '
            'know that the roof is closed). Please check the observatory. Log: {url}').format(
                camera=camera, field=field, date=date, clock=clock[:8], url=results_url)


def run_helper(snippet, *args, env=None, cwd=None):
    """Run a bash snippet with roof_closed_alert.sh sourced; "$@" are args."""
    full_env = os.environ.copy()
    full_env.update(env or {})
    command = ['bash', '-c', 'source "$1"; shift; ' + snippet, 'test', str(HELPER)] + [str(a) for a in args]
    return subprocess.run(command, capture_output=True, text=True, env=full_env, cwd=cwd, timeout=60)


# ---------------------------------------------------------------------------
# What the alert reads from the report, and the message
# ---------------------------------------------------------------------------

class TestReportParsing:

    def test_camera_field_and_frame_of_a_closed_roof_report(self, tmp_path):
        report = tmp_path / 'index.html'
        report.write_text(closed_roof_report(2461356.86688461))
        result = run_helper('roof_closed_report_camera "$1" whatever; roof_closed_report_field "$1"; '
                            'roof_closed_report_frame_jd "$1"; roof_closed_report_frame_time "$1"', report)
        assert result.stdout.splitlines() == [CAMERA, FIELD, '2461356.86688461', '12.11.2026 08:48:08 UTC']

    def test_frame_jd_of_a_successful_run(self, tmp_path):
        report = tmp_path / 'index.html'
        report.write_text(ok_report(2461356.91000))
        result = run_helper('roof_closed_report_frame_jd "$1"', report)
        assert result.stdout.strip() == '2461356.91000'

    def test_camera_settings_are_sanitised(self, tmp_path):
        report = tmp_path / 'index.html'
        report.write_text('CAMERA_SETTINGS= TTU<b>&Q2;b1x1$(id)\n')
        result = run_helper('roof_closed_report_camera "$1" ignored', report)
        assert result.stdout.strip() == 'TTUbQ2b1x1id'

    @pytest.mark.parametrize('dataset,camera', [
        ('2026-11-12_CI_Pup-02-Q2b1x1_025654_TTUQ2b1x1', 'TTUQ2b1x1'),
        ('2026-11-12_CI_Gem-03-Q1b1x1_031200_TTUQ1b1x1', 'TTUQ1b1x1'),
        ('2025-01-07_Vul8_183150_Stas', 'Stas'),
        ('2026-11-12_CI_Pup-02-Q2b1x1_025654', 'unknown'),
    ])
    def test_camera_from_the_dataset_name_without_camera_settings(self, tmp_path, dataset, camera):
        report = tmp_path / 'index.html'
        report.write_text('CAMERA_SETTINGS= \nProcessing fields Pup-02-Q2b1x1 <br>\n')
        result = run_helper('roof_closed_report_camera "$1" "$2"', report, dataset)
        assert result.stdout.strip() == camera

    @pytest.mark.parametrize('line,jd,frame_time', [
        ('', '', ''),
        ('Last  image: 12.11.2026 08:48:08.830 UTC', '', ''),
        ('Last  image: 1234.5 12.11.2026 08:48:08.830 UTC', '', '12.11.2026 08:48:08 UTC'),
        ('Last  image: 2461356.86688461 2026-11-12 08:48:08.830 UTC', '2461356.86688461', ''),
        ('Last  image: 2461356.86688461 12.11.2026 08:48:08', '2461356.86688461', '12.11.2026 08:48:08 UTC'),
        ('Last  image: 2461356.86688461 12.11.2026 08:48:08 TT', '2461356.86688461', '12.11.2026 08:48:08 TT'),
    ])
    def test_frame_jd_and_time_are_validated_separately(self, tmp_path, line, jd, frame_time):
        report = tmp_path / 'index.html'
        report.write_text('Processing fields X <br>\n' + line + '\n')
        assert run_helper('roof_closed_report_frame_jd "$1"', report).stdout.strip() == jd
        assert run_helper('roof_closed_report_frame_time "$1"', report).stdout.strip() == frame_time

    def test_message_text_is_the_agreed_one(self):
        result = run_helper('roof_closed_alert_text "$@"', CAMERA, FIELD, '12.11.2026 08:48:08 UTC',
                            'http://example.test/uploads/results_x/')
        assert result.stdout.strip() == (
            ':warning: *Observatory roof appears to be CLOSED* - camera *TTUQ2b1x1*: no stars and no sky light on '
            'the latest new image of Pup-02-Q2b1x1 taken 12.11.2026 08:48:08 UTC, but the camera keeps imaging '
            '(NINA may not know that the roof is closed). Please check the observatory. '
            'Log: http://example.test/uploads/results_x/')

    def test_message_escapes_slack_specials_and_omits_unknown_parts(self):
        result = run_helper('roof_closed_alert_text "$@"', 'a&b', '<!channel>', '', 'http://x.test/?a=1&b=<2>')
        text = result.stdout.strip()
        assert 'camera *a&amp;b*' in text
        assert 'of &lt;!channel&gt;, but' in text
        assert 'taken' not in text
        assert text.endswith('Log: http://x.test/?a=1&amp;b=&lt;2&gt;')
        assert '<' not in text and '>' not in text


# ---------------------------------------------------------------------------
# The per-camera state: rate limit, re-arming by frame JD, file safety
# ---------------------------------------------------------------------------

NOW = 1794473908
JD_CLOSED = 2461356.86688
JD_LATER = JD_CLOSED + 0.01
JD_EARLIER = JD_CLOSED - 0.01
# the JD of the unix time NOW
JD_NOW = NOW / 86400.0 + 2440587.5


def state(path, *args):
    return run_helper('roof_closed_alert_state "$@"', path, *args)


class TestAlertState:

    def test_first_closed_run_takes_the_slot(self, tmp_path):
        path = tmp_path / 'cam.state'
        result = state(path, 'closed', JD_CLOSED, NOW, 3)
        assert (result.returncode, result.stdout.strip()) == (0, 'send previous=0')
        assert path.read_text() == ('last_alert_unix=%d alerted_closed_jd=%.8f newest_closed_jd=%.8f '
                                    'newest_ok_jd=0.00000000\n' % (NOW, JD_CLOSED, JD_CLOSED))

    def test_repeat_window(self, tmp_path):
        path = tmp_path / 'cam.state'
        state(path, 'closed', JD_CLOSED, NOW, 3)
        result = state(path, 'closed', JD_LATER, NOW + 2 * 3600, 3)
        assert (result.returncode, result.stdout.strip()) == (1, 'recent hours_since_alert=2.0')
        # the suppressed run still records its newer closed frame
        assert 'newest_closed_jd=%.8f' % JD_LATER in path.read_text()
        result = state(path, 'closed', JD_LATER + 0.1, NOW + 3 * 3600, 3)
        assert (result.returncode, result.stdout.strip()) == (0, 'send previous=%d' % NOW)
        # 0 hours: every closed-roof run alerts
        assert state(path, 'closed', JD_LATER + 0.11, NOW + 3 * 3600 + 1, 0).returncode == 0

    def test_ok_run_with_a_newer_frame_rearms(self, tmp_path):
        path = tmp_path / 'cam.state'
        state(path, 'closed', JD_CLOSED, NOW, 3)
        result = state(path, 'ok', JD_LATER, NOW)
        assert (result.returncode, result.stdout.strip()) == (0, 'rearmed alerted_closed_jd=%.5f' % JD_CLOSED)
        assert path.read_text().startswith('last_alert_unix=0 ')
        # the roof closes again: alert at once, well within the repeat window
        assert state(path, 'closed', JD_LATER + 0.01, NOW + 1200, 3).stdout.strip() == 'send previous=0'

    def test_ok_run_with_an_older_frame_does_not_rearm(self, tmp_path):
        # processing order is not observation order: a good frame taken before
        # the roof closed may finish processing after the closed-roof run
        path = tmp_path / 'cam.state'
        state(path, 'closed', JD_CLOSED, NOW, 3)
        result = state(path, 'ok', JD_EARLIER, NOW)
        assert (result.returncode, result.stdout.strip()) == (0, 'unchanged')
        assert path.read_text().startswith('last_alert_unix=%d ' % NOW)
        assert state(path, 'closed', JD_LATER, NOW + 60, 3).returncode == 1

    def test_closed_frame_older_than_a_good_frame_is_not_alerted(self, tmp_path):
        path = tmp_path / 'cam.state'
        state(path, 'closed', JD_EARLIER, NOW, 3)
        state(path, 'ok', JD_LATER, NOW)
        # a late closed-roof run whose frame was taken before the good one
        result = state(path, 'closed', JD_CLOSED, NOW + 60, 3)
        assert (result.returncode, result.stdout.strip()) == (1, 'older_than_ok newest_ok_jd=%.5f' % JD_LATER)
        # unknown frame time: only the rate limit applies
        assert state(path, 'closed', 0, NOW + 120, 3).stdout.strip() == 'send previous=0'

    def test_release_gives_back_only_its_own_slot(self, tmp_path):
        path = tmp_path / 'cam.state'
        state(path, 'closed', JD_CLOSED, NOW, 3)
        assert state(path, 'release', NOW + 5, 0).stdout.strip() == 'kept'
        assert state(path, 'release', NOW, 0).stdout.strip() == 'released'
        assert path.read_text().startswith('last_alert_unix=0 ')
        assert state(path, 'closed', JD_LATER, NOW + 60, 3).returncode == 0

    def test_ok_and_release_never_create_the_file(self, tmp_path):
        path = tmp_path / 'cam.state'
        for args in (('ok', JD_LATER, NOW), ('release', NOW, 0)):
            result = state(path, *args)
            assert (result.returncode, result.stdout.strip()) == (0, 'no_state')
        assert not path.exists()

    def test_garbage_and_a_future_alert_time_do_not_block_alerts(self, tmp_path):
        path = tmp_path / 'cam.state'
        path.write_bytes(b'\xff\xfe last_alert_unix=nan newest_closed_jd=-5 junk\n')
        assert state(path, 'closed', JD_CLOSED, NOW, 3).returncode == 0
        path.write_text('last_alert_unix=%d newest_closed_jd=0 newest_ok_jd=0\n' % (NOW + 86400))
        assert state(path, 'closed', JD_CLOSED, NOW, 3).returncode == 0

    def test_future_frame_of_an_ok_run_is_not_recorded(self, tmp_path):
        # a camera clock running ahead must not make later closed-roof frames look
        # older than the newest good frame (that would block the alert)
        path = tmp_path / 'cam.state'
        state(path, 'closed', JD_CLOSED, NOW, 3)
        result = state(path, 'ok', JD_NOW + 2.0, NOW + 60)
        assert (result.returncode, result.stdout.strip()) == (0, 'unchanged')
        assert path.read_text().endswith('newest_ok_jd=0.00000000\n')
        # the clock is fixed and the roof closes again three hours later: alerted
        result = state(path, 'closed', JD_NOW + 0.13, NOW + 3 * 3600, 3)
        assert (result.returncode, result.stdout.strip()) == (0, 'send previous=%d' % NOW)

    def test_future_frame_jds_already_in_the_state_are_dropped(self, tmp_path):
        path = tmp_path / 'cam.state'
        path.write_text('last_alert_unix=0 newest_closed_jd=%.8f newest_ok_jd=%.8f\n'
                        % (JD_NOW + 30.0, JD_NOW + 30.0))
        result = state(path, 'closed', JD_CLOSED, NOW, 3)
        assert (result.returncode, result.stdout.strip()) == (0, 'send previous=0')
        assert path.read_text() == ('last_alert_unix=%d alerted_closed_jd=%.8f newest_closed_jd=%.8f '
                                    'newest_ok_jd=0.00000000\n' % (NOW, JD_CLOSED, JD_CLOSED))

    def test_future_frame_of_a_closed_run_counts_as_unknown_time(self, tmp_path):
        path = tmp_path / 'cam.state'
        result = state(path, 'closed', JD_NOW + 1.0, NOW, 3)
        assert (result.returncode, result.stdout.strip()) == (0, 'send previous=0')
        assert 'newest_closed_jd=0.00000000' in path.read_text()
        # so a correctly dated good frame still re-arms the alert
        result = state(path, 'ok', JD_NOW + 0.001, NOW + 120)
        assert result.stdout.strip() == 'rearmed alerted_closed_jd=0.00000'

    def test_rearm_follows_frame_times_not_processing_order(self, tmp_path):
        # the roof closes (alert), opens for a few minutes and closes again; the run of
        # the re-closure finishes before the run of the good frame taken in between
        path = tmp_path / 'cam.state'
        assert state(path, 'closed', JD_CLOSED, NOW, 3).stdout.strip() == 'send previous=0'
        reclosure = state(path, 'closed', JD_CLOSED + 0.042, NOW + 3700, 3)
        assert reclosure.stdout.strip().startswith('recent ')
        result = state(path, 'ok', JD_CLOSED + 0.040, NOW + 3800)
        assert result.stdout.strip() == 'rearmed alerted_closed_jd=%.5f' % JD_CLOSED
        assert state(path, 'closed', JD_CLOSED + 0.043, NOW + 3900, 3).stdout.strip() == 'send previous=0'

    def test_unsafe_state_files_are_refused_and_left_alone(self, tmp_path):
        victim = tmp_path / 'victim'
        victim.write_text('precious')
        link = tmp_path / 'link.state'
        link.symlink_to(victim)
        hardlink = tmp_path / 'hardlink.state'
        os.link(str(victim), str(hardlink))
        directory = tmp_path / 'directory.state'
        directory.mkdir()
        fifo = tmp_path / 'fifo.state'
        os.mkfifo(str(fifo))
        for path in (link, hardlink, directory, fifo):
            for args in (('closed', JD_CLOSED, NOW, 3), ('ok', JD_LATER, NOW), ('release', NOW, 0)):
                result = state(path, *args)
                assert result.returncode == 2, (path, args, result.stdout, result.stderr)
                assert result.stdout == ''
        assert victim.read_text() == 'precious'
        assert state(tmp_path / 'cam.state', 'bogus', 1).returncode == 2

    def test_concurrent_closed_runs_alert_once(self, tmp_path):
        path = tmp_path / 'cam.state'
        env = os.environ.copy()
        processes = [subprocess.Popen(['bash', '-c', 'source "$1"; shift; roof_closed_alert_state "$@"', 'test',
                                       str(HELPER), str(path), 'closed', str(JD_CLOSED + n * 1e-4), str(NOW), '3'],
                                      stdout=subprocess.PIPE, text=True, env=env) for n in range(8)]
        outputs = [process.communicate(timeout=60)[0].strip() for process in processes]
        assert len([output for output in outputs if output.startswith('send ')]) == 1, outputs
        assert len([output for output in outputs if output.startswith('recent ')]) == 7, outputs
        assert 'newest_closed_jd=%.8f' % (JD_CLOSED + 7e-4) in path.read_text()


# ---------------------------------------------------------------------------
# roof_closed_alert: the conditions, the POST and the recorded decision
# ---------------------------------------------------------------------------

@pytest.fixture
def alert_sandbox(tmp_path):
    bin_dir = tmp_path / 'bin'
    log = tmp_path / 'curl_calls.jsonl'
    fail_flag = tmp_path / 'curl_fails'
    install_fake_curl(bin_dir, log, fail_flag)
    data = tmp_path / 'data'
    data.mkdir()
    report = tmp_path / 'index.html'
    report.write_text(closed_roof_report(jd_hours_ago(0.25)))
    env = {'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'], 'SLACK_WEBHOOK_URL_TEAM': DUMMY_WEBHOOK,
           'DATA_PROCESSING_ROOT': str(data), 'DATASET_NAME': UPLOAD_NAME[:-4], 'TEST_RUN': '0',
           'INPUT_DIR_NOT_ZIP_ARCHIVE': '0'}

    def alert(**overrides):
        merged = dict(env)
        merged.update(overrides)
        result = run_helper('roof_closed_alert "$@"', report, 'http://example.test/uploads/results_x/', env=merged)
        assert result.returncode == 0 and result.stderr == '', result.stdout + result.stderr
        return result.stdout.strip()

    return types.SimpleNamespace(alert=alert, posts=lambda: read_posts(log), report=report, data=data,
                                 fail_flag=fail_flag)


class TestAlertDecision:
    PREFIX = 'Closed-roof Slack alert (camera TTUQ2b1x1): '

    def test_sent_with_a_timeout_to_the_team_webhook(self, alert_sandbox):
        assert alert_sandbox.alert() == self.PREFIX + 'sent (curl exit code 0, Slack response: ok)'
        posts = alert_sandbox.posts()
        assert len(posts) == 1
        post = posts[0]
        assert post[-1] == DUMMY_WEBHOOK
        assert post[post.index('--max-time') + 1] == '30'
        assert '--silent' in post and '--show-error' in post
        assert post[post.index('-H') + 1] == 'Content-type: application/json'
        assert slack_text(post).startswith(':warning: *Observatory roof appears to be CLOSED* - camera *TTUQ2b1x1*: ')
        assert (alert_sandbox.data / 'roof_closed_alert_TTUQ2b1x1.state').read_text().startswith('last_alert_unix=')

    @pytest.mark.parametrize('overrides,reason', [
        ({'TEST_RUN': '1'}, "suppressed - test run (the dataset name contains 'test')"),
        ({'INPUT_DIR_NOT_ZIP_ARCHIVE': '1'},
         'suppressed - manual reprocessing of an image directory, not an uploaded archive'),
        ({'ROOF_CLOSED_SLACK_ALERT': 'no'}, 'suppressed - disabled with ROOF_CLOSED_SLACK_ALERT=no'),
        ({'ROOF_CLOSED_SLACK_ALERT': 'False'}, 'suppressed - disabled with ROOF_CLOSED_SLACK_ALERT=False'),
        ({'ROOF_CLOSED_SLACK_ALERT': 'OFF'}, 'suppressed - disabled with ROOF_CLOSED_SLACK_ALERT=OFF'),
        ({'SLACK_WEBHOOK_URL_TEAM': ''}, 'not sent - SLACK_WEBHOOK_URL_TEAM is not set'),
    ])
    def test_conditions(self, alert_sandbox, overrides, reason):
        assert alert_sandbox.alert(**overrides) == self.PREFIX + reason
        assert alert_sandbox.posts() == []
        assert not (alert_sandbox.data / 'roof_closed_alert_TTUQ2b1x1.state').exists()

    def test_stale_frames(self, alert_sandbox):
        alert_sandbox.report.write_text(closed_roof_report(jd_hours_ago(10)))
        stale = self.PREFIX + 'suppressed - the frame was taken 10.0 h ago (ROOF_CLOSED_ALERT_MAX_AGE_HOURS=6)'
        assert alert_sandbox.alert() == stale
        # an unusable setting means the default
        assert alert_sandbox.alert(ROOF_CLOSED_ALERT_MAX_AGE_HOURS='bogus') == stale
        assert alert_sandbox.posts() == []
        decision = alert_sandbox.alert(ROOF_CLOSED_ALERT_MAX_AGE_HOURS='12', ROOF_CLOSED_SLACK_ALERT='yes')
        assert decision == self.PREFIX + 'sent (curl exit code 0, Slack response: ok)'
        assert len(alert_sandbox.posts()) == 1

    def test_unknown_frame_time_is_alerted(self, alert_sandbox):
        alert_sandbox.report.write_text(re.sub('Last  image:.*\n', '', closed_roof_report(jd_hours_ago(10))))
        assert alert_sandbox.alert().endswith('sent (curl exit code 0, Slack response: ok)')
        assert 'Pup-02-Q2b1x1, but the camera keeps imaging' in slack_text(alert_sandbox.posts()[0])

    def test_rate_limit_is_per_camera(self, alert_sandbox):
        sent = 'sent (curl exit code 0, Slack response: ok)'
        assert alert_sandbox.alert() == self.PREFIX + sent
        assert alert_sandbox.alert() == self.PREFIX + (
            'suppressed - an alert for this camera was sent 0.0 h ago (ROOF_CLOSED_ALERT_REPEAT_HOURS=3)')
        alert_sandbox.report.write_text(closed_roof_report(jd_hours_ago(0.2), field='Gem-03-Q1b1x1',
                                                           camera='TTUQ1b1x1'))
        other_camera_sent = 'Closed-roof Slack alert (camera TTUQ1b1x1): ' + sent
        assert alert_sandbox.alert() == other_camera_sent
        # 0 hours: no rate limit
        assert alert_sandbox.alert(ROOF_CLOSED_ALERT_REPEAT_HOURS='0') == other_camera_sent
        assert len(alert_sandbox.posts()) == 3

    def test_failed_post_is_recorded_and_retried_by_the_next_run(self, alert_sandbox):
        alert_sandbox.fail_flag.write_text('')
        decision = alert_sandbox.alert()
        assert decision == self.PREFIX + ('NOT delivered (curl exit code 7, Slack response: ' + CURL_FAILURE
                                          + ') - the next closed-roof run of this camera will try again')
        alert_sandbox.fail_flag.unlink()
        assert alert_sandbox.alert().endswith(': sent (curl exit code 0, Slack response: ok)')
        assert len(alert_sandbox.posts()) == 2

    def test_webhook_url_never_reaches_the_record(self, alert_sandbox, tmp_path):
        bin_dir = tmp_path / 'bin'
        # a curl that names the (secret) webhook URL in its error message
        write_executable(bin_dir / 'curl',
                         'echo "curl: (28) Operation timed out for $(eval echo \\${$#})" >&2; exit 28')
        decision = alert_sandbox.alert()
        assert decision == self.PREFIX + ('NOT delivered (curl exit code 28, Slack response: curl: (28) Operation '
                                          'timed out for (webhook URL)) - the next closed-roof run of this camera '
                                          'will try again')

    @pytest.mark.parametrize('kind,problem', [
        ('directory', 'cannot open it: Is a directory'),
        ('hardlink', 'refused: not a regular file with a single link owned by this user'),
    ])
    def test_unusable_state_still_alerts(self, alert_sandbox, kind, problem):
        # better an alert too many than none
        path = alert_sandbox.data / 'roof_closed_alert_TTUQ2b1x1.state'
        if kind == 'directory':
            path.mkdir()
        else:
            path.write_text('')
            os.link(str(path), str(alert_sandbox.data / 'another_name'))
        for _ in range(2):
            assert alert_sandbox.alert() == self.PREFIX + (
                'sent (curl exit code 0, Slack response: ok); the rate-limit state {} is unusable, so the rate limit '
                'was not applied (roof_closed_alert_state: {})').format(path, problem)
        assert len(alert_sandbox.posts()) == 2


# ---------------------------------------------------------------------------
# autoprocess.sh end to end, with a stand-in VaST factory
# ---------------------------------------------------------------------------

def _write_zip(path, names):
    with zipfile.ZipFile(str(path), 'w') as archive:
        for name in names:
            archive.writestr(name, FITS_STUB)


class AutoprocessSandbox:
    """A private DATA_PROCESSING_ROOT and VaST reference copy whose factory
    copies a prepared report into transient_report/index.html."""

    def __init__(self, root):
        self.root = root
        self.unmw = root / 'unmw'
        self.unmw.mkdir()
        for name in ('autoprocess.sh', 'roof_closed_alert.sh'):
            shutil.copyfile(str(REPO / name), str(self.unmw / name))
        self.data = root / 'data'
        self.data.mkdir()
        self.incoming = root / 'incoming'
        self.incoming.mkdir()
        # keep the input (and skip the pause before deleting a web_upload_* directory)
        (self.incoming / 'DO_NOT_DELETE_THIS_DIR').write_text('')
        vast = root / 'vast'
        (vast / 'util' / 'transients').mkdir(parents=True)
        (vast / 'lib').mkdir()
        write_executable(vast / 'vast', 'echo VaST')
        write_executable(vast / 'util' / 'transients' / 'transient_factory_test31.sh',
                         'cp "$UNMW_TEST_FAKE_REPORT" transient_report/index.html')
        self.report = root / 'fake_report.html'
        self.curl_log = root / 'curl_calls.jsonl'
        install_fake_curl(root / 'bin', self.curl_log, root / 'curl_fails')
        self.env = os.environ.copy()
        for key in ('GATEWAY_INTERFACE', 'REQUEST_METHOD'):
            self.env.pop(key, None)
        self.env.update(IMAGE_DATA_ROOT=str(self.data), DATA_PROCESSING_ROOT=str(self.data),
                        VAST_REFERENCE_COPY=str(vast), URL_OF_DATA_PROCESSING_ROOT='http://unused.test/uploads',
                        AUTOPROCESS_NO_WAIT='yes', WARN_ON_LOW_DISK_SPACE_SOFTLIMIT_KB='1',
                        WARN_ON_LOW_DISK_SPACE_HARDLIMIT_KB='1', SLACK_WEBHOOK_URL_TEAM=DUMMY_WEBHOOK,
                        UNMW_TEST_FAKE_REPORT=str(self.report),
                        PATH=str(root / 'bin') + os.pathsep + self.env['PATH'])

    def run(self, report_html, upload_name=UPLOAD_NAME, directory=False):
        self.report.write_text(report_html)
        if directory:
            images = self.root / upload_name
            images.mkdir()
            for name in ('a.fts', 'b.fts'):
                (images / name).write_bytes(FITS_STUB)
            target = images
        else:
            target = self.incoming / upload_name
            _write_zip(target, ['a.fits', 'b.fits'])
        before = set(self.data.glob('results_*'))
        result = subprocess.run(['bash', str(self.unmw / 'autoprocess.sh'), str(target)], cwd=str(self.root),
                                env=self.env, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        results, = set(self.data.glob('results_*')) - before
        assert (results / 'index.html').exists(), result.stdout + result.stderr
        return 'http://unused.test/uploads/' + results.name + '/'

    def posts(self):
        return read_posts(self.curl_log)

    def records(self):
        """The final message of every run in autoprocess.txt"""
        text = (self.data / 'autoprocess.txt').read_text()
        return re.split('^#{27} .* #{27}$', text, flags=re.MULTILINE)[1:]

    def decisions(self):
        text = (self.data / 'autoprocess.txt').read_text()
        return [line for line in text.splitlines() if line.startswith('Closed-roof Slack alert')]


@pytest.fixture
def autoprocess(tmp_path):
    for program in ('rsync', 'unzip', 'file'):
        if shutil.which(program) is None:
            pytest.fail(program + ' is required by autoprocess.sh')
    return AutoprocessSandbox(tmp_path)


SENT = 'Closed-roof Slack alert (camera TTUQ2b1x1): sent (curl exit code 0, Slack response: ok)'
SUPPRESSED_RECENT = ('Closed-roof Slack alert (camera TTUQ2b1x1): suppressed - an alert for this camera was sent '
                     '0.0 h ago (ROOF_CLOSED_ALERT_REPEAT_HOURS=3)')


def test_autoprocess_alerts_once_then_respects_the_repeat_window(autoprocess):
    jd = jd_hours_ago(0.5)
    results_url = autoprocess.run(closed_roof_report(jd))
    posts = autoprocess.posts()
    assert len(posts) == 1
    assert slack_text(posts[0]) == expected_text(jd, results_url)
    assert posts[0][-1] == DUMMY_WEBHOOK
    assert autoprocess.decisions() == [SENT]
    # the decision is a paragraph of the final record, after the exit code
    record = autoprocess.records()[-1]
    assert 'The script exit code is 0\n\n' + SENT + '\n\n' in record
    # the next closed-roof upload of the camera, 50 min later
    autoprocess.run(closed_roof_report(jd_hours_ago(0.1)))
    assert len(autoprocess.posts()) == 1
    assert autoprocess.decisions() == [SENT, SUPPRESSED_RECENT]
    # the verdict stays 'error', with the roof banner as the reason
    verdicts = (autoprocess.data / 'transient_search_verdicts.txt').read_text()
    assert ' error results_' in verdicts and 'OBSERVATORY ROOF CLOSED' in verdicts


def test_autoprocess_ok_run_with_a_newer_frame_rearms_the_alert(autoprocess):
    autoprocess.run(closed_roof_report(jd_hours_ago(0.5)))
    autoprocess.run(ok_report(jd_hours_ago(0.3)))
    assert len(autoprocess.decisions()) == 2
    assert autoprocess.decisions()[1].startswith('Closed-roof Slack alert (camera TTUQ2b1x1): re-armed - '
                                                 "this run's frame (JD ")
    jd = jd_hours_ago(0.1)
    results_url = autoprocess.run(closed_roof_report(jd))
    posts = autoprocess.posts()
    assert len(posts) == 2
    assert slack_text(posts[1]) == expected_text(jd, results_url)
    assert autoprocess.decisions()[2] == SENT


def test_autoprocess_ok_run_with_an_older_frame_does_not_rearm(autoprocess):
    autoprocess.run(closed_roof_report(jd_hours_ago(0.5)))
    # a good frame taken before the roof closed, processed after the closed-roof run
    autoprocess.run(ok_report(jd_hours_ago(0.8)))
    assert autoprocess.decisions() == [SENT]
    assert 'Closed-roof' not in autoprocess.records()[-1]
    autoprocess.run(closed_roof_report(jd_hours_ago(0.1)))
    assert len(autoprocess.posts()) == 1
    assert autoprocess.decisions() == [SENT, SUPPRESSED_RECENT]


def test_autoprocess_no_alert_for_a_test_upload(autoprocess):
    autoprocess.run(closed_roof_report(jd_hours_ago(0.5)), upload_name=UPLOAD_NAME.replace('.zip', '_test.zip'))
    assert autoprocess.posts() == []
    assert autoprocess.decisions() == [
        "Closed-roof Slack alert (camera TTUQ2b1x1): suppressed - test run (the dataset name contains 'test')"]


def test_autoprocess_no_alert_for_a_stale_frame(autoprocess):
    autoprocess.run(closed_roof_report(jd_hours_ago(10)))
    assert autoprocess.posts() == []
    assert autoprocess.decisions() == [
        'Closed-roof Slack alert (camera TTUQ2b1x1): suppressed - the frame was taken 10.0 h ago '
        '(ROOF_CLOSED_ALERT_MAX_AGE_HOURS=6)']
    assert not (autoprocess.data / 'roof_closed_alert_TTUQ2b1x1.state').exists()


def test_autoprocess_no_alert_for_manual_reprocessing_of_a_directory(autoprocess):
    autoprocess.run(closed_roof_report(jd_hours_ago(0.5)), upload_name='img_Pup-02-Q2b1x1_TTUQ2b1x1', directory=True)
    assert autoprocess.posts() == []
    assert autoprocess.decisions() == [
        'Closed-roof Slack alert (camera TTUQ2b1x1): suppressed - manual reprocessing of an image directory, '
        'not an uploaded archive']


def test_autoprocess_no_alert_without_the_marker(autoprocess):
    jd = jd_hours_ago(0.5)
    date, clock = vast_date_time(jd)
    autoprocess.run(report_page('***** IMAGE PROCESSING ERROR (less than 4 images processed) *****\n',
                                'Last  image: {:.8f} {} {} UTC\n'.format(jd, date, clock)))
    autoprocess.run(ok_report(jd_hours_ago(0.3)))
    assert autoprocess.posts() == []
    assert autoprocess.decisions() == []
    for record in autoprocess.records():
        assert 'Closed-roof' not in record
        # the record is laid out as before: a blank line between the exit code and the CPU temperature
        assert re.search('The script exit code is 0\n\n[^\n]*\n\nThe results should appear at', record), record
    assert not list(autoprocess.data.glob('roof_closed_alert_*'))


def test_autoprocess_without_the_helper_records_that(autoprocess):
    (autoprocess.unmw / 'roof_closed_alert.sh').unlink()
    autoprocess.run(closed_roof_report(jd_hours_ago(0.5)))
    autoprocess.run(ok_report(jd_hours_ago(0.3)))
    assert autoprocess.posts() == []
    assert autoprocess.decisions() == [
        'Closed-roof Slack alert: not sent - roof_closed_alert.sh is missing from ' + str(autoprocess.unmw)]


def test_autoprocess_stuck_camera_alert_is_unchanged(autoprocess):
    jd = jd_hours_ago(0.5)
    date, clock = vast_date_time(jd)
    results_url = autoprocess.run(report_page(
        '***** ACTION NEEDED - RESET CAMERA!!! *****   IMAGE PROCESSING ERROR (stuck camera repeatedly sending '
        'the same image) \n',
        'Last  image: {:.8f} {} {} UTC\nERROR the camera is stuck repeatedly sending the same image!\n'.format(
            jd, date, clock)))
    posts = autoprocess.posts()
    assert len(posts) == 1
    assert posts[0][:6] == ['--silent', '-X', 'POST', '-H', 'Content-type: application/json', '--data']
    assert posts[0][7:] == [DUMMY_WEBHOOK]
    text = slack_text(posts[0])
    assert text.startswith('[NMW ERROR] ')
    assert text.endswith('\nA camera error occured while processing fields {} \nThe cmaera seems to be repeatedly '
                         'writing the same image!!!\nThe detailed log output is at {}'.format(FIELD, results_url[:-1]))
    assert autoprocess.decisions() == []


# ---------------------------------------------------------------------------
# combine_reports.sh: summary rows
# ---------------------------------------------------------------------------

def _run_combine_reports(tmp_path, html):
    unmw = tmp_path / 'unmw'
    unmw.mkdir()
    for name in ('combine_reports.sh', 'report_helpers.sh'):
        shutil.copyfile(str(REPO / name), str(unmw / name))
    data = tmp_path / 'data'
    results = data / 'results_20261112_025753_2026-11-12_CI_Pup-02-Q2b1x1_025654_TTUQ2b1x1_1767138_7utQwWsM'
    results.mkdir(parents=True)
    (results / 'index.html').write_text(html)
    install_fake_curl(tmp_path / 'bin', tmp_path / 'curl_calls.jsonl', tmp_path / 'curl_fails')
    # The single-instance check counts every combine_reports.sh process on the
    # host ('ps ax'): on a server whose own cron runs the script, the copy here
    # would exit at once. An empty process list keeps the test self-contained.
    write_executable(tmp_path / 'bin' / 'ps', 'exit 0')
    env = os.environ.copy()
    for key in ('GATEWAY_INTERFACE', 'REQUEST_METHOD'):
        env.pop(key, None)
    env.update(DATA_PROCESSING_ROOT=str(data), URL_OF_DATA_PROCESSING_ROOT='http://unused.test/uploads',
               PATH=str(tmp_path / 'bin') + os.pathsep + env['PATH'])
    return unmw, data, env


def _summary_rows(summary):
    rows = [line for line in summary.read_text().splitlines() if line.startswith('<tr>')]
    header = [row for row in rows if '<th>' in row][-1]
    body = [row for row in rows if 'results_' in row]
    return re.findall(r'<th>(.*?)</th>', header), body


@pytest.mark.parametrize('existing_columns', [None, ('Altitude', 'Azimuth'), ('Altitude',), ()],
                         ids=['new', 'altitude_first', 'altitude_only', 'no_coordinates'])
@pytest.mark.parametrize('roof_closed', [True, False], ids=['roof_closed', 'ok'])
def test_combine_reports_summary_row(tmp_path, existing_columns, roof_closed):
    jd = jd_hours_ago(0.5)
    if roof_closed:
        html = closed_roof_report(jd)
    else:
        html = ok_report(jd).replace(
            'Images used for photometry 4\n',
            'Images used for photometry 4\n'
            'Field altitude during imaging: 34.2 deg (FITS keyword CENTALT) a.fits\n'
            'Field azimuth during imaging: 239.8 deg (FITS keyword CENTAZ) a.fits\n')
    unmw, data, env = _run_combine_reports(tmp_path, html)
    if existing_columns is not None:
        coordinate_headers = ''.join(f'<th>{name}(&deg;)</th>' for name in existing_columns)
        names = subprocess.run(['bash', '-c', 'D=$(date +%Y%m%d); echo ${D}_morning_summary.html '
                                '${D}_evening_summary.html'], capture_output=True, text=True, check=True)
        for name in names.stdout.split():
            (data / name).write_text(
                "<table>\n<tr><th>Camera</th><th>Obs.Time(UTC)</th><th>Field</th><th>Preview</th><th>Status</th>"
                "<th>Log</th><th>Pointing.Offset(&deg;)</th>" + coordinate_headers
                + "<th>mag.lim.</th><th>FWHM(pix)</th>"
                "<th>Candidates(new/total)</th><th>Comments</th></tr>\n")
    result = subprocess.run(['bash', str(unmw / 'combine_reports.sh')], cwd=str(tmp_path), env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    summaries = [path for path in data.glob('*_summary.html') if 'results_' in path.read_text()]
    assert len(summaries) == 1, result.stdout + result.stderr
    header_cells, rows = _summary_rows(summaries[0])
    expected_columns = ('Azimuth', 'Altitude') if existing_columns is None else existing_columns
    assert len(header_cells) == 11 + len(expected_columns)
    assert header_cells[7:-4] == [f'{name}(&deg;)' for name in expected_columns]
    assert len(rows) == 1
    row = rows[0]
    cells = re.findall(r'<td>(.*?)</td>', row)
    assert len(cells) == len(header_cells)
    if roof_closed:
        assert cells[7:-4] == [''] * len(expected_columns)
        assert "<td><span class='status-error'>ROOF CLOSED</span></td><td><a href='./results_20261112_" in row
        assert row.endswith('<td>no stars and no sky light on the latest new image'
                            ' - is the observatory roof closed?</td></tr>')
    else:
        assert cells[4] == "<span class='status-ok'>OK</span>"
        values = {'Azimuth': '239.8', 'Altitude': '34.2'}
        assert cells[7:-4] == [values[name] for name in expected_columns]
    assert row.startswith('<tr><td>TTUQ2b1x1</td>')
    assert not (tmp_path / 'curl_calls.jsonl').exists()
