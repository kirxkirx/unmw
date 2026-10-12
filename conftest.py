"""Safety defaults and strict prerequisite/skip checks for the test suite."""
import os
import sys

if os.environ.get('GATEWAY_INTERFACE') or os.environ.get('REQUEST_METHOD'):
    sys.exit('Content-Type: text/plain\n\nERROR: not a CGI')

import importlib.util
import ipaddress
import socket

import pytest


def pytest_sessionstart(session):
    mode = os.environ.get('UNMW_TEST_RARFILE')
    available = importlib.util.find_spec('rarfile') is not None
    if mode == 'absent' and available:
        raise pytest.UsageError('This CI job must run without rarfile installed')
    if mode == 'present' and not available:
        raise pytest.UsageError('This CI job requires the optional rarfile test backend')


@pytest.fixture(autouse=True)
def isolated_test_environment(monkeypatch):
    """Never inherit production destinations, credentials or monitoring hooks.

    Python connections are loopback-only. CI also runs these tests in an
    offline network namespace; native children cannot bypass that boundary.
    Generating an AAVSO-format fixture is fine; submitting it is not.
    """
    exact = {'DATA_PROCESSING_ROOT', 'IMAGE_DATA_ROOT', 'IMAGE_ARCHIVE_DIR',
             'IMAGE_ARCHIVE_URL', 'VAST_REFERENCE_COPY', 'REFERENCE_IMAGES',
             'HTDOCS_DIR', 'URL_OF_DATA_PROCESSING_ROOT', 'UNMW_LOCAL_CONFIG_SOURCED',
             'SUSPICIOUS_FILENAMES_LOG', 'UNMW_CGITB_LOGDIR', 'QUERY_STRING',
             'GATEWAY_INTERFACE', 'REQUEST_METHOD', 'CONTENT_LENGTH', 'CONTENT_TYPE'}
    for key in list(os.environ):
        if key in exact or key.startswith(('AAVSO_', 'CURL_USERNAME_', 'MONITORING_', 'SLACK_')):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('AAVSO_OBSCODE', 'XXX')
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check(address, family):
        if family == socket.AF_UNIX:
            return
        host = address[0]
        if host == 'localhost':
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        raise AssertionError('Tests must not contact external services: ' + str(host))

    def connect(sock, address):
        check(address, sock.family)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address, sock.family)
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get('UNMW_TEST_STRICT') != '1':
        return
    reporter = session.config.pluginmanager.get_plugin('terminalreporter')
    unexpected = []
    for result in reporter.stats.get('skipped', []):
        reason = str(result.longrepr)
        if os.environ.get('UNMW_TEST_RARFILE') == 'absent' and 'rarfile' in reason:
            continue
        unexpected.append(result.nodeid + ': ' + reason)
    if unexpected:
        reporter.write_sep('=', 'Unexpected CI skips', red=True)
        for message in unexpected:
            reporter.write_line(message)
        session.exitstatus = 1
