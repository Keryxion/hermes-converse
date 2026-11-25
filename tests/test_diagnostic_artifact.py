import io
from pathlib import Path
import runpy
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('debug', [False, True])
def test_artifact_uses_one_committed_revision_and_omits_release_recorder(tmp_path, monkeypatch, debug):
    build = runpy.run_path(str(ROOT / 'tools/build_artifact.py'))['build']
    commit = 'a' * 40
    archive = io.BytesIO()
    committed_integration = (ROOT / '_diagnostic_capability.py').read_bytes()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        entry = tarfile.TarInfo('_diagnostic_capability.py')
        entry.size = len(committed_integration)
        tar.addfile(entry, io.BytesIO(committed_integration))
    committed_recorder = b'# recording implementation from the committed revision\n'
    calls = []

    def git(command, *, cwd):
        assert cwd == ROOT
        calls.append(command)
        return {
            'rev-parse': (commit + '\n').encode(),
            'archive': archive.getvalue(),
            'show': committed_recorder,
        }[command[1]]

    monkeypatch.setattr(subprocess, 'check_output', git)
    output = tmp_path / 'artifact'
    assert build(ROOT, output, debug=debug, revision='test-ref') == commit
    assert calls[:2] == [
        ['git', 'rev-parse', '--verify', '--end-of-options', 'test-ref^{commit}'],
        ['git', 'archive', '--format=tar', commit],
    ]
    integration = (output / '_diagnostic_capability.py').read_bytes()
    implementation = output / '_server_recording.py'
    if debug:
        assert calls[2:] == [['git', 'show', f'{commit}:_server_recording.py']]
        assert implementation.read_bytes() == committed_recorder
        assert b'from ._server_recording import ServerRecording, maintenance, disarm' in integration
    else:
        assert len(calls) == 2
        assert not implementation.exists()
        assert integration == committed_integration
